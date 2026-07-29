from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import aliased, selectinload

from app.clients import (
    CapabilityGrant,
    CommunityClient,
    CreatorClient,
    ModerationClient,
)
from app.config import Settings
from app.database import (
    Conversation,
    Database,
    Message,
    Approval,
    Run,
    RunStep,
    ScheduledAction,
    ScheduledActionAttempt,
    SideEffect,
    UserMemory,
    append_event,
    utc_now,
)
from app.domain import AgentPlan, AgentPlanStep
from app.graph_runtime import graph_descriptor
from app.llm import DeepSeekClient
from app.mcp_gateway import McpGateway
from app.memory import AssistantMemory
from app.rate_limit import DistributedLimitExceeded, DistributedRateLimiter
from app.token_vault import DelegatedTokenVault
from app.tools import RiskLevel, ToolRegistry
from app.untrusted_content import guard_post_payload


class ApprovalRequired(Exception):
    def __init__(self, arguments: dict[str, Any]) -> None:
        super().__init__("该操作需要用户批准")
        self.arguments = arguments


class TransientToolError(RuntimeError):
    """A retryable infrastructure failure with an idempotent replay boundary."""


class DependencyPending(Exception):
    def __init__(
        self,
        *,
        task_id: str,
        status: str,
        state: dict[str, Any],
        dependency_type: str = "CREATOR_TASK",
    ) -> None:
        super().__init__(f"{dependency_type} {task_id} is {status}")
        self.task_id = task_id
        self.status = status
        self.state = state
        self.dependency_type = dependency_type


logger = logging.getLogger(__name__)


class AgentWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        llm: DeepSeekClient,
        community: CommunityClient,
        creator: CreatorClient,
        moderation: ModerationClient,
        mcp: McpGateway,
        registry: ToolRegistry,
        rate_limiter: DistributedRateLimiter | None = None,
        memory: AssistantMemory | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.llm = llm
        self.community = community
        self.creator = creator
        self.moderation = moderation
        self.mcp = mcp
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.memory = memory
        self.token_vault = DelegatedTokenVault(settings.service_shared_secret)
        self.worker_id = f"assistant-{uuid.uuid4()}"
        self._stopping = asyncio.Event()
        self._active_runs: set[asyncio.Task[None]] = set()
        self._active_schedules: set[asyncio.Task[None]] = set()
        self._dependency_watchers: dict[str, asyncio.Task[None]] = {}

    def stop(self) -> None:
        self._stopping.set()
        for task in self._dependency_watchers.values():
            task.cancel()

    async def run_forever(self) -> None:
        await asyncio.gather(
            self.run_jobs_forever(),
            self.schedule_jobs_forever(),
        )

    async def run_jobs_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                self._active_runs = {
                    task for task in self._active_runs if not task.done()
                }
                did_work = False
                try:
                    while len(self._active_runs) < self.settings.run_concurrency:
                        run_id = await self._claim_run()
                        if not run_id:
                            break
                        did_work = True
                        task = asyncio.create_task(
                            self._execute_run(run_id),
                            name=f"assistant-run:{run_id}",
                        )
                        self._active_runs.add(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Assistant run claim loop failed")
                await self._idle_when_needed(did_work)
        finally:
            if self._active_runs:
                await asyncio.gather(*self._active_runs, return_exceptions=True)
            if self._dependency_watchers:
                await asyncio.gather(
                    *self._dependency_watchers.values(),
                    return_exceptions=True,
                )

    async def schedule_jobs_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                self._active_schedules = {
                    task for task in self._active_schedules if not task.done()
                }
                did_work = False
                try:
                    while (
                        len(self._active_schedules)
                        < self.settings.scheduler_concurrency
                    ):
                        action_id = await self._claim_scheduled_action()
                        if not action_id:
                            break
                        did_work = True
                        task = asyncio.create_task(
                            self._execute_scheduled_action(action_id),
                            name=f"assistant-schedule:{action_id}",
                        )
                        self._active_schedules.add(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Assistant scheduler claim loop failed")
                await self._idle_when_needed(did_work)
        finally:
            if self._active_schedules:
                await asyncio.gather(
                    *self._active_schedules, return_exceptions=True
                )

    async def _idle_when_needed(self, did_work: bool) -> None:
        if did_work:
            return
        try:
            await asyncio.wait_for(
                self._stopping.wait(), timeout=self.settings.worker_poll_seconds
            )
        except TimeoutError:
            pass

    async def _claim_run(self) -> str | None:
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                acquired = await session.scalar(
                    select(
                        func.pg_try_advisory_xact_lock(
                            func.hashtext("assistant:run:claim")
                        )
                    )
                )
                if not acquired:
                    return None
            active = aliased(Run)
            active_for_user = (
                select(func.count(active.id))
                .where(
                    active.user_id == Run.user_id,
                    active.id != Run.id,
                    active.status == "RUNNING",
                    active.lease_expires_at.is_not(None),
                    active.lease_expires_at >= now,
                )
                .correlate(Run)
                .scalar_subquery()
            )
            active_globally = (
                select(func.count(active.id))
                .where(
                    active.status == "RUNNING",
                    active.lease_expires_at.is_not(None),
                    active.lease_expires_at >= now,
                )
                .scalar_subquery()
            )
            candidate = await session.scalar(
                select(Run)
                .where(
                    Run.status.in_(
                        ["QUEUED", "RETRYING", "RUNNING", "WAITING_DEPENDENCY"]
                    ),
                    (Run.retry_after.is_(None)) | (Run.retry_after <= now),
                    (Run.lease_expires_at.is_(None)) | (Run.lease_expires_at < now),
                    active_for_user < self.settings.max_concurrent_runs_per_user,
                    active_globally < self.settings.run_concurrency,
                )
                .order_by(Run.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            resumed_dependency = candidate.status == "WAITING_DEPENDENCY"
            candidate.status = "RUNNING"
            if candidate.started_at is None:
                candidate.started_at = now
            if resumed_dependency and candidate.dependency_wait_started_at is not None:
                candidate.dependency_wait_ms += max(
                    0,
                    int(
                        (now - candidate.dependency_wait_started_at).total_seconds()
                        * 1000
                    ),
                )
                candidate.dependency_wait_started_at = None
            if not resumed_dependency:
                candidate.attempts += 1
            candidate.retry_after = None
            candidate.version += 1
            candidate.lease_owner = self.worker_id
            candidate.lease_expires_at = now + timedelta(
                seconds=self.settings.lease_seconds
            )
            candidate.updated_at = now
            await append_event(
                session,
                candidate.id,
                "RUN_STARTED",
                {"status": "RUNNING", "attempt": candidate.attempts},
            )
            return candidate.id

    async def _execute_run(self, run_id: str) -> None:
        lease_task = asyncio.create_task(self._renew_run_lease(run_id))
        try:
            run, history, memories, tenant_id = await self._load_run_and_history(run_id)
            run = await self._ensure_runtime_identity(run)
            recalled_memories = await self._recall_task_memory(
                run=run,
                tenant_id=tenant_id,
            )
            if run.plan:
                plan = AgentPlan.model_validate(run.plan)
            else:
                await self._consume_budget(run_id, "model")
                intent = await self._track_duration(
                    run_id,
                    "model_duration_ms",
                    self.llm.understand_intent(
                        prompt=run.prompt,
                        context_post_id=run.context_post_id,
                        context_comment_id=run.context_comment_id,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        on_structured_retry=lambda: self._structured_output_retry(
                            run_id, "Intent"
                        ),
                    ),
                )
                await self._save_intent(run_id, intent.model_dump(mode="json"))
                run.intent_detail = intent.model_dump(mode="json")
                await self._consume_budget(run_id, "model")
                plan = await self._track_duration(
                    run_id,
                    "model_duration_ms",
                    self.llm.plan(
                        prompt=run.prompt,
                        context_post_id=run.context_post_id,
                        context_comment_id=run.context_comment_id,
                        client_timezone=run.client_timezone,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        structured_intent=intent,
                        on_structured_retry=lambda: self._structured_output_retry(
                            run_id, "Planner"
                        ),
                    ),
                )
                await self._save_plan(run_id, plan)
            outputs: list[dict[str, Any]] = []
            while True:
                ordinals = {
                    str(step.task_id): ordinal
                    for ordinal, step in enumerate(plan.steps, start=1)
                }
                for layer_index, layer in enumerate(plan.execution_layers(), start=1):
                    output_by_task = {
                        str(item.get("task_id")): item.get("result", {})
                        for item in outputs
                        if item.get("task_id")
                    }
                    pending: list[AgentPlanStep] = []
                    for planned_step in layer:
                        task_id = str(planned_step.task_id)
                        ordinal = ordinals[task_id]
                        completed = await self._completed_step(run_id, ordinal)
                        if completed is not None:
                            if not any(
                                item.get("ordinal") == ordinal for item in outputs
                            ):
                                outputs.append(
                                    self._output_record(
                                        ordinal, planned_step, completed
                                    )
                                )
                            continue
                        condition_result = self._condition_result(
                            planned_step, output_by_task
                        )
                        if condition_result is False:
                            if (
                                planned_step.condition
                                and planned_step.condition.on_false == "fail"
                            ):
                                raise RuntimeError(
                                    f"任务 {task_id} 的执行条件不满足"
                                )
                            step = await self._start_step(
                                run_id, ordinal, planned_step
                            )
                            skipped = {
                                "skipped": True,
                                "reason": "condition evaluated to false",
                            }
                            await self._complete_step(step.id, skipped)
                            outputs.append(
                                self._output_record(
                                    ordinal, planned_step, skipped
                                )
                            )
                            continue
                        pending.append(planned_step)

                    read_steps = [
                        planned_step
                        for planned_step in pending
                        if self.registry.get(planned_step.tool).risk
                        == RiskLevel.READ
                        and not self.registry.get(planned_step.tool).side_effecting
                    ]
                    if read_steps:
                        snapshot = sorted(
                            outputs, key=lambda item: int(item["ordinal"])
                        )
                        results = await asyncio.gather(
                            *[
                                self._execute_read_step(
                                    run=run,
                                    run_id=run_id,
                                    ordinal=ordinals[str(planned_step.task_id)],
                                    planned_step=planned_step,
                                    previous_outputs=snapshot,
                                )
                                for planned_step in read_steps
                            ],
                            return_exceptions=True,
                        )
                        for planned_step, result in zip(read_steps, results):
                            if isinstance(result, BaseException):
                                raise result
                            outputs.append(
                                self._output_record(
                                    ordinals[str(planned_step.task_id)],
                                    planned_step,
                                    result,
                                )
                            )

                    for planned_step in pending:
                        if planned_step in read_steps:
                            continue
                        ordinal = ordinals[str(planned_step.task_id)]
                        step = await self._start_step(
                            run_id, ordinal, planned_step
                        )
                        try:
                            output = await self._track_duration(
                                run_id,
                                "tool_duration_ms",
                                self._execute_tool(
                                    run=run,
                                    plan_step=planned_step,
                                    previous_outputs=sorted(
                                        outputs,
                                        key=lambda item: int(item["ordinal"]),
                                    ),
                                    ordinal=ordinal,
                                ),
                            )
                            outputs.append(
                                self._output_record(
                                    ordinal, planned_step, output
                                )
                            )
                            await self._complete_step(step.id, output)
                        except ApprovalRequired as required:
                            await self._wait_for_approval(
                                run_id=run_id,
                                step_id=step.id,
                                ordinal=ordinal,
                                planned=planned_step,
                                arguments=required.arguments,
                            )
                            return
                        except DependencyPending as pending_dependency:
                            await self._wait_for_dependency(
                                run_id=run_id,
                                step_id=step.id,
                                dependency=pending_dependency,
                            )
                            return
                        except Exception as exc:
                            await self._fail_step(step.id, str(exc))
                            raise
                    await self._save_progress_ledger(
                        run_id,
                        plan,
                        outputs,
                        active_layer=layer_index,
                    )

                await self._consume_budget(run_id, "model")
                verification = await self._track_duration(
                    run_id,
                    "model_duration_ms",
                    self.llm.verify(
                        prompt=run.prompt,
                        plan=plan,
                        tool_outputs=outputs,
                        on_structured_retry=lambda: self._structured_output_retry(
                            run_id, "Verifier"
                        ),
                    ),
                )
                await self._save_checkpoint(run_id, verification.model_dump(mode="json"))
                if verification.decision == "COMPLETE":
                    break
                if verification.decision == "FAILED":
                    raise RuntimeError(verification.reason)
                await self._consume_budget(run_id, "replan")
                await self._consume_budget(run_id, "model")
                next_plan = await self._track_duration(
                    run_id,
                    "model_duration_ms",
                    self.llm.plan(
                        prompt=run.prompt,
                        context_post_id=run.context_post_id,
                        context_comment_id=run.context_comment_id,
                        client_timezone=run.client_timezone,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        previous_execution={"outputs": outputs},
                        next_focus=verification.next_focus,
                        structured_intent=plan.intent_detail,
                        on_structured_retry=lambda: self._structured_output_retry(
                            run_id, "Planner"
                        ),
                    ),
                )
                plan = self._merge_replan(plan, next_plan)
                await self._save_plan(run_id, plan, replanned=True)

            pending_response = (run.checkpoint or {}).get("pending_final_response")
            if isinstance(pending_response, str) and pending_response.strip():
                final_response = pending_response
            else:
                await self._consume_budget(run_id, "model")
                final_response = await self._track_duration(
                    run_id,
                    "model_duration_ms",
                    self.llm.answer(
                        prompt=run.prompt,
                        plan=plan,
                        tool_outputs=outputs,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                    ),
                )
                await self._save_pending_final_response(run_id, final_response)
            if run.context_comment_id and run.context_post_id:
                await self._consume_budget(run_id, "tool")
                reply_tool = "community.reply_comment"
                reply_definition = self.registry.get(reply_tool)
                reply_args = self.registry.validate(
                    reply_tool,
                    {
                        "post_id": run.context_post_id,
                        "parent_comment_id": run.context_comment_id,
                        "content": final_response[:2_000],
                    },
                )
                reply = await self._execute_side_effect(
                    run=run,
                    tool=reply_tool,
                    args=reply_args,
                    ordinal=len(plan.steps) + 1,
                    timeout_seconds=reply_definition.timeout_seconds,
                )
                outputs.append(
                    {
                        "ordinal": len(plan.steps) + 1,
                        "tool": "community.reply_comment",
                        "label": "回复 @助手 的评论",
                        "result": reply,
                    }
                )
            await self._complete_run(run_id, final_response, outputs)
        except Exception as exc:
            await self._fail_run(run_id, exc)
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)

    async def _execute_read_step(
        self,
        *,
        run: Run,
        run_id: str,
        ordinal: int,
        planned_step: AgentPlanStep,
        previous_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, planned_step.max_attempts + 1):
            step = await self._start_step(run_id, ordinal, planned_step)
            try:
                output = await self._track_duration(
                    run_id,
                    "tool_duration_ms",
                    self._execute_tool(
                        run=run,
                        plan_step=planned_step,
                        previous_outputs=previous_outputs,
                        ordinal=ordinal,
                    ),
                )
                await self._complete_step(step.id, output)
                return output
            except Exception as exc:
                last_error = exc
                await self._fail_step(step.id, str(exc))
                if attempt >= planned_step.max_attempts or not _is_transient_exception(
                    exc
                ):
                    raise
                await append_retry_delay(attempt)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _output_record(
        ordinal: int,
        planned_step: AgentPlanStep,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "task_id": str(planned_step.task_id),
            "agent": planned_step.agent,
            "tool": planned_step.tool,
            "label": planned_step.label,
            "result": output,
        }

    async def _renew_run_lease(self, run_id: str) -> None:
        interval = max(5.0, self.settings.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with self.database.sessions() as session, session.begin():
                run = await session.get(Run, run_id, with_for_update=True)
                if (
                    run is None
                    or run.lease_owner != self.worker_id
                    or run.status != "RUNNING"
                ):
                    return
                run.lease_expires_at = utc_now() + timedelta(
                    seconds=self.settings.lease_seconds
                )
                await append_event(
                    session,
                    run_id,
                    "LEASE_RENEWED",
                    {"worker_id": self.worker_id},
                )

    async def _track_duration(
        self, run_id: str, field: str, operation: Any
    ) -> Any:
        started = time.perf_counter()
        try:
            return await operation
        finally:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            async with self.database.sessions() as session, session.begin():
                run = await session.get(Run, run_id, with_for_update=True)
                if run is not None:
                    setattr(run, field, int(getattr(run, field, 0) or 0) + elapsed_ms)

    async def _load_run_and_history(
        self, run_id: str
    ) -> tuple[Run, list[dict[str, str]], list[dict[str, str]], str]:
        async with self.database.sessions() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Run disappeared after claim")
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == run.conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(30)
                )
            ).all()
            memories = (
                await session.scalars(
                    select(UserMemory)
                    .where(UserMemory.user_id == run.user_id)
                    .order_by(UserMemory.updated_at.desc())
                    .limit(20)
                )
            ).all()
            conversation = await session.get(Conversation, run.conversation_id)
            return (
                run,
                [
                    {"role": item.role, "content": item.content}
                    for item in reversed(messages)
                ],
                [{"key": item.key, "value": item.value} for item in memories],
                conversation.tenant_id if conversation is not None else "zhiguang",
            )

    async def _recall_task_memory(
        self,
        *,
        run: Run,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        if self.memory is None:
            return []
        checkpoint = dict(run.checkpoint or {})
        if checkpoint.get("memory_context_frozen") is True:
            saved = checkpoint.get("memory_context")
            return list(saved) if isinstance(saved, list) else []
        try:
            recalled = await self.memory.recall(
                user_id=run.user_id,
                tenant_id=tenant_id,
                query=run.prompt,
            )
        except Exception:
            logger.exception("Assistant memory recall failed for run %s", run.id)
            return []
        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run.id, with_for_update=True)
            if current is not None and current.lease_owner == self.worker_id:
                current_checkpoint = dict(current.checkpoint or {})
                current_checkpoint["memory_context_frozen"] = True
                current_checkpoint["memory_context"] = recalled
                current_checkpoint["memory_recalled_at"] = utc_now().isoformat()
                current.checkpoint = current_checkpoint
                if recalled:
                    await append_event(
                        session,
                        run.id,
                        "MEMORY_RECALLED",
                        {
                            "count": len(recalled),
                            "memory_ids": [
                                item["memory_id"]
                                for item in recalled
                                if item.get("memory_id")
                            ],
                        },
                    )
        return recalled

    async def _ensure_runtime_identity(self, run: Run) -> Run:
        current = self.llm.runtime_identity()
        saved = dict(run.runtime_identity or {})
        if saved == current:
            return run
        reject_resume = False
        async with self.database.sessions() as session, session.begin():
            locked = await session.get(Run, run.id, with_for_update=True)
            if locked is None or locked.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能校验运行协议")
            completed_steps = await session.scalar(
                select(func.count(RunStep.id)).where(
                    RunStep.run_id == run.id,
                    RunStep.status == "COMPLETED",
                )
            )
            durable_effects = await session.scalar(
                select(func.count(SideEffect.id)).where(
                    SideEffect.run_id == run.id,
                    SideEffect.status.in_(["IN_FLIGHT", "UNKNOWN", "COMPLETED"]),
                )
            )
            mismatch_fields = sorted(
                key
                for key in set(saved) | set(current)
                if saved.get(key) != current.get(key)
            )
            if (completed_steps or 0) > 0 or (durable_effects or 0) > 0:
                await append_event(
                    session,
                    run.id,
                    "RUNTIME_IDENTITY_MISMATCH",
                    {
                        "fields": mismatch_fields,
                        "saved_fingerprint": _stable_hash(saved),
                        "current_fingerprint": _stable_hash(current),
                        "resume_rejected": True,
                    },
                )
                reject_resume = True
            else:
                locked.runtime_identity = current
                locked.plan = None
                locked.plan_hash = None
                locked.checkpoint = {}
                await append_event(
                    session,
                    run.id,
                    "RUNTIME_IDENTITY_UPDATED",
                    {
                        "fields": mismatch_fields,
                        "current_fingerprint": _stable_hash(current),
                    },
                )
        if reject_resume:
            raise RuntimeError(
                "Agent 运行协议已升级，且旧任务已有完成步骤；为避免混用旧计划，"
                "请重新发起请求"
            )
        run.runtime_identity = current
        run.plan = None
        run.plan_hash = None
        run.checkpoint = {}
        return run

    async def _save_plan(
        self, run_id: str, plan: AgentPlan, *, replanned: bool = False
    ) -> None:
        graph = graph_descriptor(plan)
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker cannot save plan")
            run.intent = plan.intent
            run.intent_detail = (
                plan.intent_detail.model_dump(mode="json")
                if plan.intent_detail
                else run.intent_detail
            )
            run.summary = plan.summary
            run.plan = plan.model_dump(mode="json")
            run.plan_hash = _stable_hash(run.plan)
            run.task_ledger = {
                "goal": (
                    plan.intent_detail.goal if plan.intent_detail else plan.summary
                ),
                "intent": plan.intent,
                "constraints": (
                    plan.intent_detail.constraints if plan.intent_detail else []
                ),
                "tasks": [
                    {
                        "task_id": str(step.task_id),
                        "agent": step.agent,
                        "capabilities": step.capabilities,
                        "tool": step.tool,
                        "label": step.label,
                        "depends_on": step.depends_on,
                        "condition": (
                            step.condition.model_dump(mode="json")
                            if step.condition
                            else None
                        ),
                        "max_attempts": step.max_attempts,
                    }
                    for step in plan.steps
                ],
                "graph": graph,
                "revision": run.replan_count,
            }
            run.progress_ledger = {
                "completed": [],
                "pending": [str(step.task_id) for step in plan.steps],
                "failed": [],
                "active_layer": None,
                "updated_at": utc_now().isoformat(),
            }
            run.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                "PLAN_REVISED" if replanned else "PLAN_CREATED",
                {
                    "intent": plan.intent,
                    "summary": plan.summary,
                    "layers": graph["layers"],
                },
            )

    async def _save_intent(
        self, run_id: str, intent_detail: dict[str, Any]
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能保存意图")
            run.intent_detail = intent_detail
            goal = str(intent_detail.get("goal") or "").strip()
            run.summary = (
                f"已理解：{goal[:180]}，正在生成执行计划"
                if goal
                else "已理解需求，正在生成执行计划"
            )
            run.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                "INTENT_UNDERSTOOD",
                intent_detail,
            )

    async def _structured_output_retry(
        self, run_id: str, phase: str
    ) -> None:
        await self._consume_budget(run_id, "model")
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("任务不存在或租约已失效")
            run.summary = f"{phase} 输出格式异常，正在自动修复"
            run.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                "STRUCTURED_OUTPUT_RETRY",
                {"phase": phase, "model_calls": run.model_calls},
            )

    async def _save_progress_ledger(
        self,
        run_id: str,
        plan: AgentPlan,
        outputs: list[dict[str, Any]],
        *,
        active_layer: int | None,
    ) -> None:
        completed = {
            str(item["task_id"])
            for item in outputs
            if item.get("task_id")
        }
        all_tasks = [str(step.task_id) for step in plan.steps]
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能保存 Progress Ledger")
            run.progress_ledger = {
                "completed": [task for task in all_tasks if task in completed],
                "pending": [task for task in all_tasks if task not in completed],
                "failed": [],
                "active_layer": active_layer,
                "updated_at": utc_now().isoformat(),
            }
            run.checkpoint = {
                **dict(run.checkpoint or {}),
                "completed_task_ids": [
                    task for task in all_tasks if task in completed
                ],
                "saved_at": utc_now().isoformat(),
            }
            await append_event(
                session,
                run_id,
                "PROGRESS_CHECKPOINTED",
                run.progress_ledger,
            )

    @staticmethod
    def _condition_result(
        step: AgentPlanStep,
        outputs_by_task: dict[str, Any],
    ) -> bool | None:
        condition = step.condition
        if condition is None:
            return None
        current: Any = outputs_by_task.get(condition.source_task)
        for segment in condition.path.split("."):
            if segment in {"", "$"}:
                continue
            if isinstance(current, dict):
                current = current.get(segment)
            elif isinstance(current, list) and segment.isdigit():
                index = int(segment)
                current = current[index] if 0 <= index < len(current) else None
            else:
                current = None
                break
        expected = condition.value
        operator = condition.operator
        if operator == "exists":
            return current is not None
        if operator == "eq":
            return current == expected
        if operator == "ne":
            return current != expected
        if operator == "contains":
            try:
                return expected in current
            except TypeError:
                return False
        try:
            if operator == "gt":
                return current > expected
            if operator == "gte":
                return current >= expected
            if operator == "lt":
                return current < expected
            if operator == "lte":
                return current <= expected
        except TypeError:
            return False
        raise ValueError(f"未知条件操作符：{operator}")

    @staticmethod
    def _merge_replan(current: AgentPlan, revision: AgentPlan) -> AgentPlan:
        prefix = f"replan-{len(current.steps) + 1}"
        id_map = {
            str(step.task_id): f"{prefix}-{index}"
            for index, step in enumerate(revision.steps, start=1)
        }
        revised_steps = [
            step.model_copy(
                update={
                    "task_id": id_map[str(step.task_id)],
                    "depends_on": [
                        id_map[dependency] for dependency in step.depends_on
                    ],
                    "condition": (
                        step.condition.model_copy(
                            update={
                                "source_task": id_map[
                                    step.condition.source_task
                                ]
                            }
                        )
                        if step.condition
                        else None
                    ),
                }
            )
            for step in revision.steps
        ]
        return AgentPlan.model_validate(
            {
                "intent": current.intent,
                "summary": revision.summary,
                "response_guidance": revision.response_guidance,
                "intent_detail": (
                    current.intent_detail.model_dump(mode="json")
                    if current.intent_detail
                    else (
                        revision.intent_detail.model_dump(mode="json")
                        if revision.intent_detail
                        else None
                    )
                ),
                "steps": [
                    step.model_dump(mode="json")
                    for step in [*current.steps, *revised_steps]
                ],
            }
        )

    async def _start_step(
        self, run_id: str, ordinal: int, planned: AgentPlanStep
    ) -> RunStep:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能启动步骤")
            step = await session.scalar(
                select(RunStep)
                .where(RunStep.run_id == run_id, RunStep.ordinal == ordinal)
                .with_for_update()
            )
            if step is None:
                step = RunStep(
                    run_id=run_id,
                    ordinal=ordinal,
                    kind="TOOL",
                    task_key=str(planned.task_id),
                    agent_name=planned.agent,
                    capabilities=planned.capabilities,
                    depends_on=planned.depends_on,
                    condition=(
                        planned.condition.model_dump(mode="json")
                        if planned.condition
                        else None
                    ),
                    tool_name=planned.tool,
                    label=planned.label,
                    status="RUNNING",
                    input=planned.arguments,
                    attempts=1,
                    max_attempts=planned.max_attempts,
                    started_at=utc_now(),
                )
                session.add(step)
            else:
                step.status = "RUNNING"
                step.task_key = str(planned.task_id)
                step.agent_name = planned.agent
                step.capabilities = planned.capabilities
                step.depends_on = planned.depends_on
                step.condition = (
                    planned.condition.model_dump(mode="json")
                    if planned.condition
                    else None
                )
                step.tool_name = planned.tool
                step.label = planned.label
                step.input = planned.arguments
                step.output = None
                step.error = None
                step.started_at = utc_now()
                step.completed_at = None
                step.attempts += 1
                step.max_attempts = planned.max_attempts
            await session.flush()
            await append_event(
                session,
                run_id,
                "STEP_STARTED",
                {
                    "step_id": step.id,
                    "ordinal": ordinal,
                    "tool": planned.tool,
                    "task_id": str(planned.task_id),
                    "agent": planned.agent,
                    "attempt": step.attempts,
                    "label": planned.label,
                },
            )
            return step

    async def _completed_step(
        self, run_id: str, ordinal: int
    ) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            step = await session.scalar(
                select(RunStep).where(
                    RunStep.run_id == run_id,
                    RunStep.ordinal == ordinal,
                    RunStep.status == "COMPLETED",
                )
            )
        return dict(step.output or {}) if step is not None else None

    async def _complete_step(self, step_id: str, output: dict[str, Any]) -> None:
        async with self.database.sessions() as session, session.begin():
            step = await session.get(RunStep, step_id, with_for_update=True)
            if step is None:
                return
            run = await session.get(Run, step.run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 的步骤结果已拒绝")
            step.status = "COMPLETED"
            step.output = output
            step.completed_at = utc_now()
            await append_event(
                session,
                step.run_id,
                "STEP_COMPLETED",
                {
                    "step_id": step.id,
                    "ordinal": step.ordinal,
                    "tool": step.tool_name,
                    "output": output,
                },
            )

    async def _fail_step(self, step_id: str, error: str) -> None:
        async with self.database.sessions() as session, session.begin():
            step = await session.get(RunStep, step_id, with_for_update=True)
            if step is None:
                return
            run = await session.get(Run, step.run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                return
            step.status = "FAILED"
            step.error = error[:4_000]
            step.completed_at = utc_now()
            await append_event(
                session,
                step.run_id,
                "STEP_FAILED",
                {"step_id": step.id, "ordinal": step.ordinal, "error": step.error},
            )

    async def _wait_for_dependency(
        self,
        *,
        run_id: str,
        step_id: str,
        dependency: DependencyPending,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            step = await session.get(RunStep, step_id, with_for_update=True)
            if run is None or step is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能挂起依赖任务")
            now = utc_now()
            step.status = "WAITING_DEPENDENCY"
            step.output = {
                "dependency_type": dependency.dependency_type,
                "task_id": dependency.task_id,
                "status": dependency.status,
            }
            run.status = "WAITING_DEPENDENCY"
            if run.dependency_wait_started_at is None:
                run.dependency_wait_started_at = now
            run.retry_after = now + timedelta(
                seconds=self.settings.creator_dependency_poll_seconds
            )
            run.lease_owner = None
            run.lease_expires_at = None
            run.version += 1
            run.updated_at = now
            await append_event(
                session,
                run_id,
                "DEPENDENCY_WAITING",
                {
                    "step_id": step.id,
                    "dependency_type": dependency.dependency_type,
                    "task_id": dependency.task_id,
                    "status": dependency.status,
                    "retry_after": run.retry_after.isoformat(),
                },
            )
        if dependency.dependency_type == "CREATOR_TASK":
            self._start_dependency_watcher(
                run_id=run_id,
                task_id=dependency.task_id,
            )

    def _start_dependency_watcher(self, *, run_id: str, task_id: str) -> None:
        existing = self._dependency_watchers.get(task_id)
        if existing is not None and not existing.done():
            return
        watcher = asyncio.create_task(
            self._watch_creator_dependency(run_id=run_id, task_id=task_id),
            name=f"assistant-creator-watch:{task_id}",
        )
        self._dependency_watchers[task_id] = watcher
        watcher.add_done_callback(
            lambda completed: self._remove_dependency_watcher(
                task_id=task_id,
                completed=completed,
            )
        )

    def _remove_dependency_watcher(
        self,
        *,
        task_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        if self._dependency_watchers.get(task_id) is completed:
            self._dependency_watchers.pop(task_id, None)

    async def _watch_creator_dependency(
        self, *, run_id: str, task_id: str
    ) -> None:
        try:
            async with self.database.sessions() as session:
                run = await session.get(Run, run_id)
            if (
                run is None
                or run.status != "WAITING_DEPENDENCY"
                or not run.delegated_token
            ):
                return
            snapshot = await self.creator.wait_for_terminal_event(
                task_id,
                access_token=self.token_vault.decrypt(run.delegated_token),
                trace_id=run.trace_id,
                timeout_seconds=self.settings.creator_timeout_seconds,
            )
            if snapshot is None:
                return
            async with self.database.sessions() as session, session.begin():
                current = await session.get(Run, run_id, with_for_update=True)
                if current is None or current.status != "WAITING_DEPENDENCY":
                    return
                current.retry_after = utc_now()
                current.updated_at = utc_now()
                await append_event(
                    session,
                    run_id,
                    "DEPENDENCY_SIGNALED",
                    {
                        "dependency_type": "CREATOR_TASK",
                        "task_id": task_id,
                        "status": snapshot.get("status"),
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info(
                "Creator SSE watcher ended; durable polling fallback remains active",
                exc_info=True,
            )

    async def _execute_tool(
        self,
        *,
        run: Run,
        plan_step: AgentPlanStep,
        previous_outputs: list[dict[str, Any]],
        ordinal: int,
    ) -> dict[str, Any]:
        tool = plan_step.tool
        args = self._resolve_arguments(
            run=run,
            tool=tool,
            arguments=dict(plan_step.arguments),
            previous_outputs=previous_outputs,
        )
        definition = self.registry.get(tool)
        if definition.risk == RiskLevel.EXTERNAL_WRITE:
            if not await self._has_approval(run.id, ordinal, args):
                raise ApprovalRequired(args)
        args = self.registry.validate(tool, args)
        if definition.side_effecting:
            return await self._execute_side_effect(
                run=run,
                tool=tool,
                args=args,
                ordinal=ordinal,
                timeout_seconds=definition.timeout_seconds,
            )
        await self._consume_budget(run.id, "tool")
        raw_output = await self._dispatch_tool(
            run=run,
            tool=tool,
            args=args,
            ordinal=ordinal,
            timeout_seconds=definition.timeout_seconds,
            operation_key=f"assistant-read-{run.id}-{ordinal}",
            continuation=None,
        )
        return self.registry.validate_output(
            tool, raw_output, args, run_id=run.id
        )

    async def _dispatch_tool(
        self,
        *,
        run: Run,
        tool: str,
        args: dict[str, Any],
        ordinal: int,
        timeout_seconds: int,
        operation_key: str,
        continuation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if tool == "community.search_posts":
            query = str(args["query"])
            capability = await self._issue_capability(
                run,
                action="community.search_posts",
                resources=[],
            )
            results = await asyncio.wait_for(
                self.community.search_posts(
                    query,
                    int(args["limit"]),
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
            return {"query": query, "results": results}
        if tool == "community.analyze_engagement":
            capability = await self._issue_capability(
                run,
                action="community.analyze_engagement",
                resources=[],
            )
            return await asyncio.wait_for(
                self.community.analyze_engagement(
                    topic=args.get("topic"),
                    days=int(args["days"]),
                    limit=int(args["limit"]),
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.list_own_posts":
            max_items = int(args["max_items"])
            collected: list[dict[str, Any]] = []
            offset = 0
            while len(collected) <= max_items:
                page_limit = min(100, max_items + 1 - len(collected))
                capability = await self._issue_capability(
                    run,
                    action="community.list_own_posts",
                    resources=[],
                )
                page = await asyncio.wait_for(
                    self.community.list_own_posts(
                        limit=page_limit,
                        offset=offset,
                        capability_token=capability.token,
                        trace_id=run.trace_id,
                    ),
                    timeout=timeout_seconds,
                )
                collected.extend(page)
                offset += len(page)
                if len(page) < page_limit or not page:
                    break
            truncated = len(collected) > max_items
            posts = collected[:max_items]
            return {
                "posts": posts,
                "count": len(posts),
                "truncated": truncated,
            }
        if tool in {"community.get_post", "community.summarize_post"}:
            post_id = str(args.get("post_id") or run.context_post_id or "")
            if not post_id:
                raise ValueError("当前对话没有可用的帖子上下文")
            capability = await self._issue_capability(
                run,
                action="community.get_post",
                resources=[f"post:{post_id}"],
            )
            post = await asyncio.wait_for(
                self.community.get_post(
                    post_id,
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
            post = guard_post_payload(post)
            if tool == "community.get_post":
                return post
            await self._consume_budget(run.id, "model")
            summary = await self._track_duration(
                run.id,
                "model_duration_ms",
                asyncio.wait_for(
                    self.llm.summarize(post, args.get("focus")),
                    timeout=timeout_seconds,
                ),
            )
            return {
                "post_id": post_id,
                "title": post.get("title"),
                "summary": summary,
                "source_content_sha256": post.get(
                    "contentSha256", post.get("content_sha256")
                ),
            }
        if tool == "creator.create_draft":
            access_token = self.token_vault.decrypt(run.delegated_token)
            state = dict(continuation or {})
            task_id = str(state.get("task_id") or "")
            if not task_id:
                submitted = await asyncio.wait_for(
                    self.creator.submit_draft(
                        instruction=str(args["instruction"]),
                        references=list(args.get("references") or []),
                        access_token=access_token,
                        idempotency_key=operation_key,
                        trace_id=run.trace_id,
                    ),
                    timeout=timeout_seconds,
                )
                task_id = str(submitted["task_id"])
                state = {
                    "task_id": task_id,
                    "submitted_at": utc_now().isoformat(),
                    "status": str(submitted.get("status") or "QUEUED"),
                }
                raise DependencyPending(
                    task_id=task_id,
                    status=state["status"],
                    state=state,
                )
            submitted_at = datetime.fromisoformat(
                str(state["submitted_at"]).replace("Z", "+00:00")
            )
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            if (utc_now() - submitted_at).total_seconds() > (
                self.settings.creator_timeout_seconds
            ):
                raise TimeoutError(f"Creator task {task_id} did not finish in time")
            snapshot = await asyncio.wait_for(
                self.creator.get_task(
                    task_id,
                    access_token=access_token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
            creator_status = str(snapshot.get("status") or "UNKNOWN")
            if creator_status in {"FAILED", "CANCELLED"}:
                raise RuntimeError(
                    f"Creator task {task_id} ended with {creator_status}: "
                    f"{snapshot.get('error_message') or snapshot.get('error_code') or ''}"
                )
            if creator_status != "COMPLETED":
                state["status"] = creator_status
                raise DependencyPending(
                    task_id=task_id,
                    status=creator_status,
                    state=state,
                )
            return await asyncio.wait_for(
                self.creator.create_handoff(
                    task_id=task_id,
                    snapshot=snapshot,
                    access_token=access_token,
                    idempotency_key=operation_key,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "moderation.check_draft":
            state = dict(continuation or {})
            task_id = str(state.get("task_id") or "")
            draft_id = str(args["draft_id"])
            expected_sha = str(args["expected_content_sha256"]).lower()
            if not task_id:
                capability = await self._issue_capability(
                    run,
                    action="community.get_own_draft",
                    resources=[f"post:{draft_id}"],
                )
                draft = await asyncio.wait_for(
                    self.community.get_own_draft(
                        draft_id,
                        capability_token=capability.token,
                        trace_id=run.trace_id,
                    ),
                    timeout=min(timeout_seconds, 30),
                )
                actual_sha = str(
                    draft.get("contentSha256")
                    or draft.get("content_sha256")
                    or ""
                ).lower()
                if actual_sha != expected_sha:
                    raise ValueError("审核前草稿内容版本已变化，请重新创作或重新规划")
                content = "\n\n".join(
                    part.strip()
                    for part in [
                        str(draft.get("title") or ""),
                        str(draft.get("description") or ""),
                        str(
                            draft.get("bodyMarkdown")
                            or draft.get("body_markdown")
                            or ""
                        ),
                    ]
                    if part and part.strip()
                )
                submitted = await asyncio.wait_for(
                    self.moderation.submit_task(
                        content=content,
                        content_id=draft_id,
                        creator_id=run.user_id,
                        idempotency_key=operation_key,
                        trace_id=run.trace_id,
                    ),
                    timeout=min(timeout_seconds, 30),
                )
                task_id = str(submitted["id"])
                status = str(submitted.get("status") or "PENDING")
                state = {
                    "task_id": task_id,
                    "draft_id": draft_id,
                    "content_sha256": expected_sha,
                    "submitted_at": utc_now().isoformat(),
                    "status": status,
                }
                if status not in {"COMPLETED", "WAITING_REVIEW", "FAILED"}:
                    raise DependencyPending(
                        task_id=task_id,
                        status=status,
                        state=state,
                        dependency_type="MODERATION_TASK",
                    )
                snapshot = submitted
            else:
                if (
                    str(state.get("draft_id")) != draft_id
                    or str(state.get("content_sha256")).lower() != expected_sha
                ):
                    raise ValueError("审核依赖绑定的草稿版本已变化")
                snapshot = await asyncio.wait_for(
                    self.moderation.get_task(task_id),
                    timeout=min(timeout_seconds, 30),
                )
            moderation_status = str(snapshot.get("status") or "UNKNOWN")
            if moderation_status in {"PENDING", "RUNNING"}:
                state["status"] = moderation_status
                raise DependencyPending(
                    task_id=task_id,
                    status=moderation_status,
                    state=state,
                    dependency_type="MODERATION_TASK",
                )
            if moderation_status == "FAILED":
                raise RuntimeError(
                    f"Moderation task {task_id} failed: "
                    f"{snapshot.get('error_message') or 'unknown error'}"
                )
            decision = dict(snapshot.get("agent_decision") or {})
            final_action = str(snapshot.get("final_action") or "")
            return {
                "task_id": task_id,
                "draft_id": draft_id,
                "status": moderation_status,
                "final_action": final_action or None,
                "risk_type": snapshot.get("final_risk_type")
                or decision.get("risk_type"),
                "risk_score": decision.get("risk_score"),
                "requires_human_review": moderation_status == "WAITING_REVIEW"
                or final_action == "HUMAN_REVIEW",
                "reason": decision.get("reason"),
                "content_sha256": expected_sha,
            }
        if tool == "publication.schedule":
            run_at = _parse_run_at(args["run_at"])
            if run_at <= utc_now() + timedelta(seconds=15):
                raise ValueError("定时发布时间必须至少晚于当前时间 15 秒")
            ttl_seconds = int((run_at - utc_now()).total_seconds()) + 3_600
            if ttl_seconds > 604_800:
                raise ValueError("定时发布目前最多可提前约 6 天安排")
            capability = await self._issue_capability(
                run,
                action="publication.publish_now",
                resources=[f"post:{args['draft_id']}"],
                ttl_seconds=ttl_seconds,
                max_uses=5,
            )
            async with self.database.sessions() as session, session.begin():
                action = await session.scalar(
                    select(ScheduledAction).where(
                        ScheduledAction.idempotency_key == operation_key
                    )
                )
                if action is None:
                    action = ScheduledAction(
                        run_id=run.id,
                        user_id=run.user_id,
                        draft_id=args["draft_id"],
                        expected_content_sha256=args["expected_content_sha256"],
                        creator_task_id=None,
                        instruction=run.prompt,
                        run_at=run_at,
                        status="SCHEDULED",
                        idempotency_key=operation_key,
                        capability_id=capability.capability_id,
                        capability_token=self.token_vault.encrypt(capability.token),
                    )
                    session.add(action)
                    await session.flush()
            return {
                "action_id": action.id,
                "draft_id": action.draft_id,
                "run_at": action.run_at.isoformat(),
                "status": action.status,
            }
        if tool == "publication.schedule_batch":
            base_run_at = _parse_run_at(args["run_at"])
            interval = int(args["interval_minutes"])
            items = list(args["items"])
            final_run_at = base_run_at + timedelta(
                minutes=interval * (len(items) - 1)
            )
            if base_run_at <= utc_now() + timedelta(seconds=15):
                raise ValueError("批量定时发布时间必须至少晚于当前时间 15 秒")
            if final_run_at > utc_now() + timedelta(days=6):
                raise ValueError("批量定时发布目前最多可提前约 6 天安排")
            actions: list[dict[str, Any]] = []
            for index, item in enumerate(items):
                item_key = f"{operation_key}-{index + 1}"
                async with self.database.sessions() as session:
                    action = await session.scalar(
                        select(ScheduledAction).where(
                            ScheduledAction.idempotency_key == item_key
                        )
                    )
                if action is None:
                    item_run_at = base_run_at + timedelta(
                        minutes=interval * index
                    )
                    ttl_seconds = (
                        int((item_run_at - utc_now()).total_seconds()) + 3_600
                    )
                    capability = await self._issue_capability(
                        run,
                        action="publication.publish_now",
                        resources=[f"post:{item['draft_id']}"],
                        ttl_seconds=ttl_seconds,
                        max_uses=5,
                    )
                    async with self.database.sessions() as session, session.begin():
                        action = await session.scalar(
                            select(ScheduledAction).where(
                                ScheduledAction.idempotency_key == item_key
                            )
                        )
                        if action is None:
                            action = ScheduledAction(
                                run_id=run.id,
                                user_id=run.user_id,
                                draft_id=item["draft_id"],
                                expected_content_sha256=item[
                                    "expected_content_sha256"
                                ],
                                creator_task_id=None,
                                instruction=run.prompt,
                                run_at=item_run_at,
                                status="SCHEDULED",
                                idempotency_key=item_key,
                                capability_id=capability.capability_id,
                                capability_token=self.token_vault.encrypt(
                                    capability.token
                                ),
                            )
                            session.add(action)
                            await session.flush()
                actions.append(
                    {
                        "action_id": action.id,
                        "draft_id": action.draft_id,
                        "run_at": action.run_at.isoformat(),
                        "status": action.status,
                    }
                )
            return {"status": "SCHEDULED", "actions": actions}
        if tool == "publication.publish_now":
            capability = await self._issue_capability(
                run,
                action="publication.publish_now",
                resources=[f"post:{args['draft_id']}"],
            )
            return await asyncio.wait_for(
                self.community.publish_ai_draft(
                    post_id=args["draft_id"],
                    creator_id=run.user_id,
                    idempotency_key=operation_key,
                    capability_token=capability.token,
                    expected_content_sha256=args["expected_content_sha256"],
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.reply_comment":
            capability = await self._issue_capability(
                run,
                action="community.reply_comment",
                resources=[
                    f"post:{args['post_id']}",
                    f"comment:{args['parent_comment_id']}",
                ],
            )
            return await asyncio.wait_for(
                self.community.reply_comment(
                    post_id=args["post_id"],
                    parent_comment_id=args["parent_comment_id"],
                    content=args["content"],
                    assistant_run_id=run.id,
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.delete_post":
            capability = await self._issue_capability(
                run,
                action="community.delete_post",
                resources=[f"post:{args['post_id']}"],
            )
            return await asyncio.wait_for(
                self.community.delete_post(
                    post_id=args["post_id"],
                    idempotency_key=operation_key,
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.delete_own_posts_batch":
            post_ids = [str(value) for value in args["post_ids"]]
            deleted = 0
            already_deleted = 0
            completed_ids: list[str] = []
            for chunk_index, start in enumerate(range(0, len(post_ids), 20)):
                chunk = post_ids[start : start + 20]
                capability = await self._issue_capability(
                    run,
                    action="community.delete_own_posts_batch",
                    resources=[f"post:{post_id}" for post_id in chunk],
                )
                result = await asyncio.wait_for(
                    self.community.delete_posts_batch(
                        post_ids=chunk,
                        idempotency_key=f"{operation_key}-{chunk_index + 1}",
                        capability_token=capability.token,
                        trace_id=run.trace_id,
                    ),
                    timeout=timeout_seconds,
                )
                completed_ids.extend(
                    str(value)
                    for value in (
                        result.get("postIds") or result.get("post_ids") or []
                    )
                )
                deleted += int(
                    result.get("deletedCount")
                    or result.get("deleted_count")
                    or 0
                )
                already_deleted += int(
                    result.get("alreadyDeletedCount")
                    or result.get("already_deleted_count")
                    or 0
                )
            return {
                "post_ids": completed_ids,
                "deleted_count": deleted,
                "already_deleted_count": already_deleted,
                "status": "deleted",
            }
        if tool.startswith("mcp."):
            return await asyncio.wait_for(
                self.mcp.call(tool, args),
                timeout=timeout_seconds,
            )
        raise ValueError(f"Unsupported tool: {tool}")

    async def _issue_capability(
        self,
        run: Run,
        *,
        action: str,
        resources: list[str],
        ttl_seconds: int = 120,
        max_uses: int = 1,
    ) -> CapabilityGrant:
        access_token = self.token_vault.decrypt(run.delegated_token)
        return await self.community.issue_capability(
            access_token=access_token,
            run_id=run.id,
            actions=[action],
            resources=resources,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            trace_id=run.trace_id,
        )

    async def _execute_side_effect(
        self,
        *,
        run: Run,
        tool: str,
        args: dict[str, Any],
        ordinal: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        cached, operation_key, continuation, first_execution = (
            await self._prepare_side_effect(
                run.id, ordinal, tool, args
            )
        )
        if cached is not None:
            return self.registry.validate_output(
                tool, cached, args, run_id=run.id
            )
        if first_execution:
            await self._consume_budget(run.id, "tool")
        try:
            raw_output = await self._dispatch_tool(
                run=run,
                tool=tool,
                args=args,
                ordinal=ordinal,
                timeout_seconds=timeout_seconds,
                operation_key=operation_key,
                continuation=continuation,
            )
            output = self.registry.validate_output(
                tool, raw_output, args, run_id=run.id
            )
            await self._finish_side_effect(
                run.id,
                ordinal,
                status="COMPLETED",
                result=output,
                remote_operation_id=_remote_operation_id(output),
            )
            return output
        except DependencyPending as pending:
            await self._finish_side_effect(
                run.id,
                ordinal,
                status="WAITING_DEPENDENCY",
                result=pending.state,
                remote_operation_id=pending.task_id,
            )
            raise
        except Exception as exc:
            transient = _is_transient_exception(exc)
            await self._finish_side_effect(
                run.id,
                ordinal,
                status="UNKNOWN" if transient else "FAILED",
                error=str(exc),
            )
            if transient:
                raise TransientToolError(
                    f"{tool} 暂时不可用，将按幂等键重试：{exc}"
                ) from exc
            raise

    async def _prepare_side_effect(
        self,
        run_id: str,
        ordinal: int,
        tool: str,
        args: dict[str, Any],
    ) -> tuple[
        dict[str, Any] | None,
        str,
        dict[str, Any] | None,
        bool,
    ]:
        request_hash = _stable_hash({"tool": tool, "arguments": args})
        operation_key = f"assistant-effect-{run_id}-{ordinal}"
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能准备副作用")
            effect = await session.scalar(
                select(SideEffect)
                .where(
                    SideEffect.run_id == run_id,
                    SideEffect.step_ordinal == ordinal,
                )
                .with_for_update()
            )
            if effect is None:
                first_execution = True
                continuation = None
                effect = SideEffect(
                    run_id=run_id,
                    step_ordinal=ordinal,
                    tool_name=tool,
                    operation_key=operation_key,
                    request_hash=request_hash,
                    resource_id=_effect_resource_id(tool, args),
                    status="PREPARED",
                )
                session.add(effect)
                await session.flush()
                event_type = "SIDE_EFFECT_PREPARED"
            else:
                first_execution = False
                continuation = (
                    dict(effect.result or {})
                    if effect.status == "WAITING_DEPENDENCY"
                    else None
                )
                operation_key = effect.operation_key
                if effect.tool_name != tool or effect.request_hash != request_hash:
                    raise RuntimeError(
                        "同一步骤的副作用参数已变化，拒绝复用旧幂等边界"
                    )
                if effect.status == "COMPLETED" and effect.result is not None:
                    return dict(effect.result), operation_key, None, False
                event_type = (
                    "SIDE_EFFECT_DEPENDENCY_RESUMED"
                    if continuation is not None
                    else "SIDE_EFFECT_RECONCILING"
                )
            effect.status = "IN_FLIGHT"
            effect.attempts += 1
            effect.error = None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                event_type,
                {
                    "tool": tool,
                    "ordinal": ordinal,
                    "operation_key": operation_key,
                    "attempt": effect.attempts,
                },
            )
        return None, operation_key, continuation, first_execution

    async def _finish_side_effect(
        self,
        run_id: str,
        ordinal: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        remote_operation_id: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            effect = await session.scalar(
                select(SideEffect)
                .where(
                    SideEffect.run_id == run_id,
                    SideEffect.step_ordinal == ordinal,
                )
                .with_for_update()
            )
            if effect is None:
                return
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 的副作用结果已拒绝")
            effect.status = status
            effect.result = result
            effect.remote_operation_id = remote_operation_id
            effect.error = error[:4_000] if error else None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                f"SIDE_EFFECT_{status}",
                {
                    "tool": effect.tool_name,
                    "ordinal": ordinal,
                    "operation_key": effect.operation_key,
                    "remote_operation_id": remote_operation_id,
                    "error": effect.error,
                },
            )

    def _resolve_arguments(
        self,
        *,
        run: Run,
        tool: str,
        arguments: dict[str, Any],
        previous_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        args = dict(arguments)
        if tool == "community.search_posts":
            args["query"] = str(args.get("query") or run.prompt)
            try:
                requested_limit = int(args.get("limit", 5))
            except (TypeError, ValueError):
                requested_limit = 5
            args["limit"] = max(1, min(requested_limit, 10))
        elif tool == "community.analyze_engagement":
            topic = args.get("topic")
            args["topic"] = str(topic).strip() if topic else None
            args.setdefault("days", 7)
            args.setdefault("limit", 10)
        elif tool == "community.list_own_posts":
            try:
                requested_max = int(args.get("max_items", 1_000))
            except (TypeError, ValueError):
                requested_max = 1_000
            args["max_items"] = max(1, min(requested_max, 1_000))
        elif tool in {"community.get_post", "community.summarize_post"}:
            args["post_id"] = str(args.get("post_id") or run.context_post_id or "")
        elif tool == "creator.create_draft":
            args["instruction"] = str(args.get("instruction") or run.prompt)
            args["references"] = self._reference_results(args, previous_outputs)
        elif tool == "moderation.check_draft":
            draft = self._resolve_draft(args, previous_outputs)
            args["draft_id"] = draft["draft_id"]
            args["expected_content_sha256"] = str(
                draft.get("content_sha256") or ""
            ).lower()
        elif tool in {"publication.schedule", "publication.publish_now"}:
            draft = self._resolve_draft(args, previous_outputs)
            args["draft_id"] = draft["draft_id"]
            resolved_sha = str(draft.get("content_sha256") or "").lower()
            supplied_sha = str(args.get("expected_content_sha256") or "").lower()
            if supplied_sha and resolved_sha and supplied_sha != resolved_sha:
                raise ValueError("计划中的草稿版本与 Creator 实际产物不一致")
            # The model cannot invent or replace the freshness boundary. Only the
            # typed Creator handoff from this run is authoritative.
            args["expected_content_sha256"] = resolved_sha
            moderation = next(
                (
                    item.get("result", {})
                    for item in reversed(previous_outputs)
                    if item.get("tool") == "moderation.check_draft"
                    and str(item.get("result", {}).get("draft_id")) == args["draft_id"]
                ),
                None,
            )
            if (
                isinstance(getattr(run, "intent_detail", None), dict)
                and getattr(run, "intent_detail").get("domain")
                == "community_operation"
                and moderation is None
            ):
                raise ValueError("社区运营草稿必须先经过审核 Agent 才能发布")
            if moderation and moderation.get("final_action") != "PASS":
                raise ValueError("草稿未通过审核，发布步骤已阻止")
        elif tool == "publication.schedule_batch":
            approved_drafts: dict[str, dict[str, str]] = {}
            for item in previous_outputs:
                if item.get("tool") != "moderation.check_draft":
                    continue
                result = item.get("result", {})
                if result.get("final_action") != "PASS":
                    continue
                draft_id = str(result.get("draft_id") or "")
                content_sha = str(result.get("content_sha256") or "").lower()
                if draft_id and content_sha:
                    approved_drafts[draft_id] = {
                        "draft_id": draft_id,
                        "expected_content_sha256": content_sha,
                    }
            if not 2 <= len(approved_drafts) <= 10:
                raise ValueError("批量定时发布需要 2—10 篇已通过审核的草稿")
            args["items"] = list(approved_drafts.values())
            args.setdefault("interval_minutes", 30)
        elif tool == "community.reply_comment":
            args["post_id"] = str(args.get("post_id") or run.context_post_id or "")
            args["parent_comment_id"] = str(
                args.get("parent_comment_id") or run.context_comment_id or ""
            )
        elif tool == "community.delete_post":
            args["post_id"] = str(args.get("post_id") or run.context_post_id or "")
        elif tool == "community.delete_own_posts_batch":
            inventory = next(
                (
                    item.get("result", {})
                    for item in reversed(previous_outputs)
                    if item.get("tool") == "community.list_own_posts"
                ),
                None,
            )
            if not isinstance(inventory, dict):
                raise ValueError("批量删除前必须先读取当前用户的完整帖子清单")
            if inventory.get("truncated"):
                raise ValueError(
                    "当前用户帖子超过单次安全上限，未执行不完整的批量删除"
                )
            post_ids = [
                str(item.get("id"))
                for item in list(inventory.get("posts") or [])
                if isinstance(item, dict) and item.get("id")
            ]
            if not post_ids:
                raise ValueError("当前用户没有可删除的帖子")
            args["post_ids"] = list(dict.fromkeys(post_ids))
        return args

    @staticmethod
    def _reference_results(
        args: dict[str, Any], previous: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        supplied = args.get("references")
        if isinstance(supplied, list):
            explicit = [item for item in supplied if isinstance(item, dict)]
            if explicit:
                return explicit[:10]

        references: list[dict[str, Any]] = []
        for item in previous:
            result = item.get("result", {})
            if item.get("tool") == "community.search_posts":
                references.extend(
                    entry
                    for entry in list(result.get("results") or [])
                    if isinstance(entry, dict)
                )
            elif (
                item.get("tool") == "community.analyze_engagement"
                and isinstance(result, dict)
            ):
                topic = result.get("topic") or "全站"
                references.append(
                    {
                        "id": f"analytics:{topic}",
                        "title": f"{topic}社区活跃度分析",
                        "description": (
                            f"发帖 {result.get('published_post_count', 0)}，"
                            f"评论 {result.get('comment_count', 0)}，"
                            f"活跃创作者 {result.get('active_creator_count', 0)}，"
                            f"互动用户 {result.get('interacting_user_count', 0)}。"
                            f"数据限制：{result.get('limitations', [])}"
                        ),
                        "body_markdown": json.dumps(
                            {
                                "top_posts": result.get("top_posts", []),
                                "top_contributors": result.get(
                                    "top_contributors", []
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            elif item.get("tool") == "community.get_post" and isinstance(result, dict):
                references.append(dict(result))
            elif item.get("tool") == "community.summarize_post" and isinstance(result, dict):
                references.append(
                    {
                        "id": result.get("post_id"),
                        "title": result.get("title"),
                        "description": result.get("summary"),
                        "content_sha256": result.get("source_content_sha256"),
                    }
                )
        return references[-10:]

    @staticmethod
    def _resolve_draft(
        args: dict[str, Any], previous: list[dict[str, Any]]
    ) -> dict[str, Any]:
        draft_id = str(args.get("draft_id") or "").strip()
        if draft_id and not AgentWorker._is_draft_placeholder(draft_id):
            for item in reversed(previous):
                result = item.get("result", {})
                if str(result.get("draft_id") or "") == str(draft_id):
                    return dict(result)
            raise ValueError("发布只能使用当前任务中 Creator 生成并绑定版本的草稿")
        for item in reversed(previous):
            result = item.get("result", {})
            if result.get("draft_id"):
                return dict(result)
        raise ValueError("发布步骤缺少 Creator 生成的草稿")

    @staticmethod
    def _is_draft_placeholder(value: str) -> bool:
        text = value.strip()
        if text.startswith("$"):
            return True
        normalized = text.upper().replace("-", "_").replace(" ", "_")
        return normalized in {
            "AUTO",
            "LAST_DRAFT",
            "PREVIOUS_DRAFT",
            "PREVIOUS_STEP",
            "FROM_PREVIOUS_STEP",
            "DRAFT_FROM_PREVIOUS_STEP",
            "DRAFT_ID_FROM_PREVIOUS_STEP",
        }

    async def _has_approval(
        self, run_id: str, ordinal: int, arguments: dict[str, Any]
    ) -> bool:
        async with self.database.sessions() as session:
            approval = await session.scalar(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.step_ordinal == ordinal,
                    Approval.status == "APPROVED",
                )
            )
            run = await session.get(Run, run_id)
        return bool(
            approval
            and run
            and approval.plan_hash == run.plan_hash
            and approval.input_hash == _stable_hash(arguments)
            and approval.expires_at > utc_now()
        )

    async def _wait_for_approval(
        self,
        *,
        run_id: str,
        step_id: str,
        ordinal: int,
        planned: AgentPlanStep,
        arguments: dict[str, Any],
    ) -> None:
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            step = await session.get(RunStep, step_id, with_for_update=True)
            if run is None or step is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 无法创建审批")
            existing = await session.scalar(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.step_ordinal == ordinal,
                )
            )
            run.status = "WAITING_APPROVAL"
            run.version += 1
            run.lease_owner = None
            run.lease_expires_at = None
            step.status = "WAITING_APPROVAL"
            step.input = arguments
            step.error = None
            if existing is None:
                existing = Approval(
                    run_id=run_id,
                    step_ordinal=ordinal,
                    user_id=run.user_id,
                    action=planned.tool,
                    description=planned.label,
                    status="PENDING",
                    plan_hash=run.plan_hash or _stable_hash(run.plan or {}),
                    input_hash=_stable_hash(arguments),
                    preview=arguments,
                    expected_run_version=run.version,
                    expires_at=now
                    + timedelta(minutes=self.settings.approval_ttl_minutes),
                )
                session.add(existing)
            await session.flush()
            await append_event(
                session,
                run_id,
                "APPROVAL_REQUIRED",
                {
                    "approval_id": existing.id,
                    "action": existing.action,
                    "description": existing.description,
                    "preview": existing.preview,
                    "expected_run_version": existing.expected_run_version,
                },
            )

    async def _consume_budget(self, run_id: str, kind: str) -> None:
        if kind == "model" and self.rate_limiter is not None:
            async with self.database.sessions() as session:
                snapshot = await session.get(Run, run_id)
                if snapshot is None or snapshot.lease_owner != self.worker_id:
                    raise RuntimeError("任务不存在或租约已失效")
                user_id = snapshot.user_id
            await self.rate_limiter.consume_model_call(user_id=user_id)
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("任务不存在或租约已失效")
            if run.deadline_at and run.deadline_at <= utc_now():
                raise RuntimeError("任务超过最大执行时间")
            if kind == "model":
                if run.model_calls >= run.max_model_calls:
                    raise RuntimeError("模型调用预算已耗尽")
                run.model_calls += 1
            elif kind == "tool":
                if run.tool_calls >= run.max_tool_calls:
                    raise RuntimeError("工具调用预算已耗尽")
                run.tool_calls += 1
            elif kind == "replan":
                if run.replan_count >= run.max_replans:
                    raise RuntimeError("重规划预算已耗尽")
                run.replan_count += 1
            else:
                raise ValueError(f"未知预算类型：{kind}")
            await append_event(
                session,
                run_id,
                "BUDGET_UPDATED",
                {
                    "model_calls": run.model_calls,
                    "tool_calls": run.tool_calls,
                    "replan_count": run.replan_count,
                },
            )

    async def _save_checkpoint(
        self, run_id: str, verification: dict[str, Any]
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 无法保存 Checkpoint")
            checkpoint = dict(run.checkpoint or {})
            checkpoint["verification"] = verification
            checkpoint["saved_at"] = utc_now().isoformat()
            run.checkpoint = checkpoint
            await append_event(session, run_id, "RUN_VERIFIED", verification)

    async def _save_pending_final_response(
        self, run_id: str, final_response: str
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 无法保存最终回复 Checkpoint")
            checkpoint = dict(run.checkpoint or {})
            checkpoint["pending_final_response"] = final_response
            checkpoint["response_saved_at"] = utc_now().isoformat()
            run.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "FINAL_RESPONSE_CHECKPOINTED",
                {"length": len(final_response)},
            )

    async def _complete_run(
        self, run_id: str, final_response: str, outputs: list[dict[str, Any]]
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker completion rejected")
            run.status = "COMPLETED"
            run.completed_at = utc_now()
            run.final_response = final_response
            run.delegated_token = None
            run.lease_owner = None
            run.lease_expires_at = None
            run.version += 1
            run.updated_at = utc_now()
            session.add(
                Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=final_response,
                    parts=outputs,
                    run_id=run.id,
                )
            )
            await append_event(
                session,
                run.id,
                "RUN_COMPLETED",
                {"status": "COMPLETED", "response": final_response},
            )
        if self.memory is not None:
            try:
                episode = await self.memory.record_completed_run(run_id, outputs)
                if episode is not None:
                    async with self.database.sessions() as session, session.begin():
                        await append_event(
                            session,
                            run_id,
                            "MEMORY_CONSOLIDATED",
                            {
                                "episode_id": episode.id,
                                "expires_at": episode.expires_at.isoformat(),
                            },
                        )
            except Exception:
                logger.exception(
                    "Run %s completed, but automatic memory consolidation failed",
                    run_id,
                )

    async def _fail_run(self, run_id: str, error: Exception) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                return
            error_text = str(error)[:4_000]
            can_retry = (
                _is_transient_exception(error)
                and run.attempts < run.max_attempts
                and (run.deadline_at is None or run.deadline_at > utc_now())
            )
            retry_delay = (
                error.retry_after_seconds
                if isinstance(error, DistributedLimitExceeded)
                else min(60, 5 * (2 ** max(0, run.attempts - 1)))
            )
            run.status = "RETRYING" if can_retry else "FAILED"
            run.error = error_text
            run.retry_after = (
                utc_now() + timedelta(seconds=max(1, min(60, retry_delay)))
                if can_retry
                else None
            )
            if not can_retry:
                run.delegated_token = None
                run.completed_at = utc_now()
            run.lease_owner = None
            run.lease_expires_at = None
            run.version += 1
            run.updated_at = utc_now()
            await append_event(
                session,
                run.id,
                "RUN_RETRYING" if can_retry else "RUN_FAILED",
                {
                    "status": run.status,
                    "error": run.error,
                    "attempt": run.attempts,
                    "retry_after": (
                        run.retry_after.isoformat() if run.retry_after else None
                    ),
                },
            )

    async def _claim_scheduled_action(self) -> str | None:
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            action = await session.scalar(
                select(ScheduledAction)
                .where(
                    ScheduledAction.status.in_(["SCHEDULED", "RETRYING"]),
                    ScheduledAction.run_at <= now,
                    (ScheduledAction.lease_expires_at.is_(None))
                    | (ScheduledAction.lease_expires_at < now),
                )
                .order_by(ScheduledAction.run_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if action is None:
                return None
            action.status = "RUNNING"
            action.attempts += 1
            action.lease_owner = self.worker_id
            action.lease_expires_at = now + timedelta(
                seconds=self.settings.lease_seconds
            )
            previous = (
                await session.scalars(
                    select(ScheduledActionAttempt).where(
                        ScheduledActionAttempt.action_id == action.id,
                        ScheduledActionAttempt.status == "RUNNING",
                    )
                )
            ).all()
            for attempt in previous:
                attempt.status = "UNKNOWN"
                attempt.error = "Worker lease expired before the attempt was finalized"
                attempt.completed_at = now
            session.add(
                ScheduledActionAttempt(
                    action_id=action.id,
                    attempt=action.attempts,
                    status="RUNNING",
                    worker_id=self.worker_id,
                    started_at=now,
                )
            )
            return action.id

    async def _execute_scheduled_action(self, action_id: str) -> None:
        async with self.database.sessions() as session:
            action = await session.get(ScheduledAction, action_id)
        if action is None:
            return
        try:
            if not action.capability_token:
                raise RuntimeError("定时发布缺少委托能力令牌")
            result = await self.community.publish_ai_draft(
                post_id=action.draft_id,
                creator_id=action.user_id,
                idempotency_key=action.idempotency_key,
                capability_token=self.token_vault.decrypt(action.capability_token),
                expected_content_sha256=action.expected_content_sha256,
                trace_id=action.run_id,
            )
            result = self.registry.validate_output(
                "publication.publish_now",
                result,
                {"draft_id": action.draft_id},
                run_id=action.run_id,
            )
            async with self.database.sessions() as session, session.begin():
                current = await session.get(
                    ScheduledAction, action_id, with_for_update=True
                )
                if current is None or current.lease_owner != self.worker_id:
                    return
                current.status = "COMPLETED"
                current.result = result
                current.error = None
                current.capability_token = None
                current.lease_owner = None
                current.lease_expires_at = None
                await self._finish_scheduled_attempt(
                    session,
                    current,
                    status="COMPLETED",
                    result=result,
                )
        except Exception as exc:
            async with self.database.sessions() as session, session.begin():
                current = await session.get(
                    ScheduledAction, action_id, with_for_update=True
                )
                if current is None or current.lease_owner != self.worker_id:
                    return
                retryable = _is_transient_exception(exc)
                current.status = (
                    "RETRYING"
                    if retryable and current.attempts < 5
                    else "FAILED"
                )
                current.error = str(exc)[:4_000]
                if current.status == "FAILED":
                    current.capability_token = None
                if current.status == "RETRYING":
                    current.run_at = utc_now() + timedelta(
                        seconds=min(300, 10 * (2 ** max(0, current.attempts - 1)))
                    )
                current.lease_owner = None
                current.lease_expires_at = None
                await self._finish_scheduled_attempt(
                    session,
                    current,
                    status="RETRYABLE_ERROR" if current.status == "RETRYING" else "FAILED",
                    error=str(exc),
                )

    async def _finish_scheduled_attempt(
        self,
        session: Any,
        action: ScheduledAction,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        attempt = await session.scalar(
            select(ScheduledActionAttempt)
            .where(
                ScheduledActionAttempt.action_id == action.id,
                ScheduledActionAttempt.attempt == action.attempts,
            )
            .with_for_update()
        )
        if attempt is None:
            return
        attempt.status = status
        attempt.result = result
        attempt.error = error[:4_000] if error else None
        attempt.completed_at = utc_now()


def _parse_run_at(value: Any) -> datetime:
    if not value:
        raise ValueError("定时发布缺少 run_at")
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


async def append_retry_delay(attempt: int) -> None:
    await asyncio.sleep(min(4.0, 0.5 * (2 ** max(0, attempt - 1))))


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_transient_exception(error: BaseException) -> bool:
    if isinstance(error, (TransientToolError, DistributedLimitExceeded)):
        return True
    if isinstance(
        error,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status in {408, 425, 429} or status >= 500
    return False


def _effect_resource_id(tool: str, arguments: dict[str, Any]) -> str | None:
    if tool in {
        "publication.schedule",
        "publication.publish_now",
        "moderation.check_draft",
    }:
        return f"post:{arguments.get('draft_id')}"
    if tool == "publication.schedule_batch":
        ids = ",".join(
            str(item.get("draft_id"))
            for item in list(arguments.get("items") or [])
        )
        return f"posts:{ids}"
    if tool == "community.reply_comment":
        return (
            f"post:{arguments.get('post_id')}/"
            f"comment:{arguments.get('parent_comment_id')}"
        )
    if tool == "community.delete_post":
        return f"post:{arguments.get('post_id')}"
    if tool == "community.delete_own_posts_batch":
        return "posts:" + ",".join(
            str(value) for value in list(arguments.get("post_ids") or [])
        )
    return None


def _remote_operation_id(output: dict[str, Any]) -> str | None:
    for key in ("action_id", "post_id", "draft_id", "id", "task_id"):
        value = output.get(key)
        if value is not None:
            return str(value)
    return None
