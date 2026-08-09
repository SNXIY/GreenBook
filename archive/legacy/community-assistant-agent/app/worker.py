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
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from app.clients import (
    CapabilityGrant,
    CommunityClient,
    CreatorClient,
)
from app.config import Settings
from app.database import (
    Artifact,
    ArtifactRelation,
    Conversation,
    ConversationGoal as ConversationGoalRecord,
    Database,
    IntentDelta as IntentDeltaRecord,
    Message,
    Approval,
    PolicyAudit,
    Run,
    RunStep,
    ScheduledAction,
    ScheduledActionAttempt,
    SideEffect,
    ToolExecutionReceipt,
    ToolJob,
    TargetBinding as TargetBindingRecord,
    UserMemory,
    append_event,
    utc_now,
)
from app.artifacts import publish_final_artifact, publish_step_artifact
from app.conversation_workspace import ConversationWorkspace
from app.domain import (
    AdaptiveExecutionDecision,
    AgentPlan,
    AgentPlanStep,
    CommunityIntent,
    ConversationGoal,
    GoalMatch,
    GoalResolution,
    IntentDelta,
    PendingClarification,
    ProgressDecision,
    ResolvedTargetView,
    TargetBinding,
    TargetCandidate,
    TargetContext,
    TurnIntent,
)
from app.agent_registry import agent_registry
from app.execution import (
    deterministic_verification,
    ExecutionPath,
    normalize_execution_decision,
    normalize_compiled_path,
    render_continuation_publish_result,
    render_creator_result,
    render_goal_delta_result,
    requires_verification,
    workload_lane,
)
from app.graph_runtime import graph_descriptor
from app.llm import DeepSeekClient
from app.intent_delta import IntentDeltaBinder, TurnIntentParser
from app.goal_resolver import GoalResolver
from app.goal_workspace import goals_for_resolution, materialize_goal_workspace
from app.intent_delta_plan_compiler import IntentDeltaPlanCompiler
from app.operation_contracts import OperationPlanGuard
from app.target_resolver import TargetResolver
from app.temporal_resolver import (
    TemporalResolution,
    format_beijing_time,
    normalize_run_at_for_tool,
    resolve_schedule_time,
)
from app.turn_pipeline import TurnPipeline
from app.turn_plan import (
    TurnPlan,
    TurnPlanBuilder,
    changes_from_operation,
    primary_operation_from_changes,
)
from app.mcp_gateway import McpGateway
from app.memory import AssistantMemory
from app.policy import (
    CommunityPolicyEngine,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    community_policy,
)
from app.plan_compiler import PlanCompiler
from app.rate_limit import DistributedLimitExceeded, DistributedRateLimiter
from app.router import RouteDecision, control_plane_router
from app.query_agent import QueryAgent, query_agent
from app.task_manager import task_manager
from app.target_resolver import EntityTargetResolution
from app.token_vault import DelegatedTokenVault
from app.tools import (
    ExecutionMode,
    RiskLevel,
    ToolDefinition,
    ToolRegistry,
    tool_registry,
)
from app.tool_runtime import (
    MIGRATED_READ_TOOLS,
    MIGRATED_WRITE_TOOLS,
    ToolAdapterRuntime,
    ToolCredentials,
    ToolErrorCode,
    ToolInvocationContext,
    ToolInvocationStatus,
    ToolRuntime,
    ToolRuntimeContext,
    ToolRuntimeError,
    UnknownSideEffectError,
    tool_adapter_runtime,
)
from app.read_tools import (
    CommunityCapabilityProvider,
    register_migrated_read_handlers,
)
from app.schedule_repository import ScheduleRepository
from app.side_effect_ledger import SideEffectLedger
from app.write_tools import UpdateScheduleServices, register_update_schedule_handler
from app.schedule_commands import (
    ScheduleCommandServices,
    register_schedule_command_handlers,
)
from app.publication_commands import (
    PublishNowRequest,
    PublishNowServices,
    execute_publish_now,
    register_publish_now_handler,
)
from app.creator_tools import CreatorToolServices, register_creator_tool_handlers
from app.tool_dependency import DependencyPending
from app.untrusted_content import guard_post_payload


class ApprovalRequired(Exception):
    def __init__(self, arguments: dict[str, Any]) -> None:
        super().__init__("该操作需要用户批准")
        self.arguments = arguments


class TransientToolError(RuntimeError):
    """A retryable infrastructure failure with an idempotent replay boundary."""


class PermanentToolError(RuntimeError):
    """A non-retryable tool failure that should fail the run with a clear reason."""


# DependencyPending lives in app.tool_dependency (canonical).


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
        mcp: McpGateway,
        registry: ToolRegistry,
        rate_limiter: DistributedRateLimiter | None = None,
        memory: AssistantMemory | None = None,
        policy: CommunityPolicyEngine = community_policy,
    ) -> None:
        self.settings = settings
        self.database = database
        self.llm = llm
        self.community = community
        self.creator = creator
        self.mcp = mcp
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.memory = memory
        self.policy = policy
        self.plan_compiler = PlanCompiler(
            tools=registry,
            agents=agent_registry,
        )
        self.intent_delta_binder = IntentDeltaBinder()
        self.turn_intent_parser = TurnIntentParser()
        self.goal_resolver = GoalResolver()
        self.turn_pipeline = TurnPipeline()
        self.turn_plan_builder = TurnPlanBuilder()
        self.intent_delta_plan_compiler = IntentDeltaPlanCompiler()
        self.operation_plan_guard = OperationPlanGuard()
        self.target_resolver = TargetResolver()
        self.tool_runtime = ToolAdapterRuntime()
        self.execution_runtime = ToolRuntime(
            definitions=registry,
            legacy_dispatch=self._dispatch_builtin_tool,
        )
        self.execution_runtime.set_legacy_executor(self._legacy_tool_executor)
        self.execution_runtime.set_capability_provider(
            CommunityCapabilityProvider(community)
        )
        self.execution_runtime.set_policy_gate(self._runtime_policy_gate)
        self.control_router = control_plane_router
        self.task_manager = task_manager
        self.query_agent = query_agent
        self.token_vault = DelegatedTokenVault(settings.service_shared_secret)
        self.schedule_repository = ScheduleRepository(
            database,
            encrypt_token=self.token_vault.encrypt,
        )
        self.side_effect_ledger = SideEffectLedger(
            database,
            worker_id="",  # filled after worker_id assigned
        )
        self.worker_id = f"assistant-{uuid.uuid4()}"
        self.side_effect_ledger.worker_id = self.worker_id
        self._stopping = asyncio.Event()
        self._active_runs: set[asyncio.Task[None]] = set()
        self._active_schedules: set[asyncio.Task[None]] = set()
        self._active_tool_jobs: set[asyncio.Task[None]] = set()
        self._dependency_watchers: dict[str, asyncio.Task[None]] = {}
        self._register_builtin_tool_handlers()

    def _register_builtin_tool_handlers(self) -> None:
        """Install transport adapters on this Worker's ToolRuntime instance."""
        self.execution_runtime.adopt_staged_handlers(self.registry)
        register_migrated_read_handlers(
            self.execution_runtime,
            community=self.community,
            schedule_lookup=self.schedule_repository,
        )
        register_update_schedule_handler(
            self.execution_runtime,
            services=UpdateScheduleServices(
                schedules=self.schedule_repository,
                ledger=self.side_effect_ledger,
                community=self.community,
                publication_min_lead_seconds=self.settings.publication_min_lead_seconds,
                publication_max_schedule_days=self.settings.publication_max_schedule_days,
                consume_budget=self._consume_budget,
            ),
        )
        register_schedule_command_handlers(
            self.execution_runtime,
            services=ScheduleCommandServices(
                schedules=self.schedule_repository,
                ledger=self.side_effect_ledger,
                community=self.community,
                publication_min_lead_seconds=self.settings.publication_min_lead_seconds,
                publication_max_schedule_days=self.settings.publication_max_schedule_days,
                consume_budget=self._consume_budget,
                run_prompt_loader=self._load_run_prompt,
            ),
        )
        register_publish_now_handler(
            self.execution_runtime,
            services=PublishNowServices(
                community=self.community,
                ledger=self.side_effect_ledger,
                issue_capability=self._runtime_issue_capability,
                registry=self.registry,
                consume_budget=self._consume_budget,
            ),
        )
        register_creator_tool_handlers(
            self.execution_runtime,
            services=CreatorToolServices(
                creator=self.creator,
                community=self.community,
                ledger=self.side_effect_ledger,
                issue_capability=self._runtime_issue_capability,
                consume_budget=self._consume_budget,
                load_content_target=self._load_active_content_target_for_runtime,
            ),
        )
        for name in self.registry.names():
            if name in MIGRATED_READ_TOOLS or name in MIGRATED_WRITE_TOOLS:
                continue
            if self.execution_runtime.handler_for(name) is None:
                self.execution_runtime.register_handler(
                    name, self._dispatch_builtin_tool
                )

    async def get_own_schedule(
        self, *, action_id: str, user_id: str
    ) -> dict[str, Any]:
        return await self.schedule_repository.get_own_schedule(
            action_id=action_id, user_id=user_id
        )

    async def _load_run_prompt(self, run_id: str) -> str | None:
        async with self.database.sessions() as session:
            run = await session.get(Run, run_id)
        if run is None:
            return None
        return str(run.prompt or "") or None

    async def _runtime_issue_capability(
        self,
        *,
        action: str,
        resources: list[str],
        max_uses: int = 1,
        ttl_seconds: int = 120,
        context: ToolInvocationContext,
        credentials: ToolCredentials,
    ) -> CapabilityGrant:
        del credentials
        async with self.database.sessions() as session:
            run = await session.get(Run, context.run_id)
        if run is None:
            raise RuntimeError("Run 不存在，无法签发 Capability")
        return await self._issue_capability(
            run,
            action=action,
            resources=resources,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
        )

    async def _load_active_content_target_for_runtime(
        self, context: ToolInvocationContext
    ) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            run = await session.get(Run, context.run_id)
        if run is None:
            return None
        target_context = await self._load_target_context(run)
        content = getattr(target_context, "content_target", None)
        if content is None:
            return None
        return {
            "draft_id": str(
                getattr(content, "target_id", None)
                or getattr(content, "draft_id", None)
                or ""
            ),
            "content_sha256": str(getattr(content, "content_sha256", None) or "").lower(),
            "goal_id": run.goal_id,
        }

    async def _runtime_policy_gate(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        run: Run | None,
        definition: ToolDefinition,
    ) -> None:
        if run is None:
            return
        approval_granted = (
            await self._has_approval(
                run.id, int(context.trace_metadata.get("ordinal") or 0), arguments
            )
            if definition.risk == RiskLevel.EXTERNAL_WRITE
            else False
        )
        decision = self.policy.evaluate(
            context=PolicyContext(
                run_id=run.id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                principal_role=run.principal_role,
                action=tool_name,
                resource=_policy_resource(tool_name, arguments, definition),
                approval_granted=approval_granted,
            ),
            definition=definition,
            registry=self.registry,
        )
        await self._record_policy_decision(
            run=run,
            tool=tool_name,
            resource=_policy_resource(tool_name, arguments, definition),
            decision=decision,
        )
        if decision.decision == PolicyDecisionType.DENY:
            raise PermissionError(decision.reason)
        if decision.decision == PolicyDecisionType.REQUIRE_APPROVAL:
            raise ApprovalRequired(arguments)

    def stop(self) -> None:
        self._stopping.set()
        for task in self._dependency_watchers.values():
            task.cancel()
# asyncio.gather(...) 的含义：

# 同时启动这三个协程（并行，不是一个接一个）
# 一直等，直到三个都返回或某个抛错
# 但这三个都是 *_forever() 死循环，正常情况 不会“执行完”，会一直跑：

# run_jobs_forever：抢 Run
# schedule_jobs_forever：定时任务
# tool_jobs_forever：工具任务
# 所以实际效果是：服务运行期间三路后台循环并行工作；只有关服务、stop() / 取消任务时，它们才结束，gather 才返回。
    async def run_forever(self) -> None:
        await asyncio.gather(
            self.run_jobs_forever(), # 抢 Run
            self.schedule_jobs_forever(), # 定时任务
            self.tool_jobs_forever(), # 工具任务
        )

    async def run_and_tool_jobs_forever(self) -> None:
        await asyncio.gather(
            self.run_jobs_forever(),
            self.tool_jobs_forever(),
        )

    async def run_jobs_forever(self) -> None:
        try:
            # private final AtomicBoolean stopping = new AtomicBoolean(false);
            while not self._stopping.is_set():
                # _active_runs 里是 asyncio.create_task(_execute_run(...)) 那些任务
                # task.done() 为真：已经跑完/失败/取消
                # 只保留 not task.done()：还在执行的
                # 下一行会用 len(self._active_runs) < run_concurrency 判断还能不能再抢新任务。
                # 不清理的话，已完成的 task 一直占着名额，并发数会算错，新任务抢不进来。
                self._active_runs = {
                    task for task in self._active_runs if not task.done()
                }
                did_work = False
                try:
                    # 当前正在跑的 Run 数量还没到配置上限时，才继续抢新任务。
                    while len(self._active_runs) < self.settings.run_concurrency:
                        # 查 assistant_runs 里 QUEUED（以及可重试等状态），改成 RUNNING 再执行。
                        run_id = await self._claim_run()
                        if not run_id:
                            break
                        # 标记为有工作。
                        did_work = True
                        task = asyncio.create_task(
                            # 执行 Run。
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
# schedule_jobs_forever() 在 all 模式下服务启动后就在后台转。它做的是：

# 去库里看有没有到期的 ScheduledAction（比如之前安排的定时发布）
# 有 → 抢走并执行
# 没有 → 空转一会再查（idle），几乎不干活
# 和前端这次消息的关系：

# 情况	会不会走定时任务
# 用户只是普通聊天
# 不会创建定时任务；scheduler 循环空转
# 某次对话里助手调用了 publication.schedule 等
# 才会写入 ScheduledAction，到点才被 scheduler 执行
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

    async def tool_jobs_forever(self) -> None:
        try:
            while not self._stopping.is_set():
                self._active_tool_jobs = {
                    task for task in self._active_tool_jobs if not task.done()
                }
                did_work = False
                try:
                    while (
                        len(self._active_tool_jobs)
                        < self.settings.tool_job_concurrency
                    ):
                        job_id = await self._claim_tool_job()
                        if not job_id:
                            break
                        did_work = True
                        task = asyncio.create_task(
                            self._execute_tool_job(job_id),
                            name=f"assistant-tool-job:{job_id}",
                        )
                        self._active_tool_jobs.add(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Assistant tool queue claim loop failed")
                if did_work:
                    continue
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.settings.tool_job_poll_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            if self._active_tool_jobs:
                await asyncio.gather(
                    *self._active_tool_jobs,
                    return_exceptions=True,
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
#从数据库“抢”一条可执行任务，抢到就改成 RUNNING 并加上租约；没抢到返回 None。
    async def _claim_run(self) -> str | None:
        now = utc_now()# 当前时间
        async with self.database.sessions() as session, session.begin():
            # 获取数据库连接，并开始一个事务。
            if session.get_bind().dialect.name == "postgresql": # 如果数据库是 PostgreSQL，则使用 PostgreSQL 的 advisory lock 机制来确保只有一个 worker 能抢到 Run。
                # 如果数据库是 PostgreSQL，则使用 PostgreSQL 的 advisory lock 机制来确保只有一个 worker 能抢到 Run。
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
                    active.workload_lane == Run.workload_lane,
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
                        [
                            "QUEUED",
                            "RETRYING",
                            "RUNNING",
                            "WAITING_DEPENDENCY",
                            "WAITING_LANE",
                        ]
                    ),
                    (Run.retry_after.is_(None)) | (Run.retry_after <= now),
                    (Run.lease_expires_at.is_(None)) | (Run.lease_expires_at < now),
                    or_(
                        and_(
                            Run.workload_lane == "READ",
                            active_for_user
                            < self.settings.max_concurrent_read_runs_per_user,
                        ),
                        and_(
                            Run.workload_lane != "READ",
                            active_for_user
                            < self.settings.max_concurrent_runs_per_user,
                        ),
                    ),
                    active_globally < self.settings.run_concurrency,
                )
                .order_by(Run.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            resumed_dependency = candidate.status == "WAITING_DEPENDENCY"
            resumed_lane = candidate.status == "WAITING_LANE"
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
            if not resumed_dependency and not resumed_lane:
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

    async def _claim_tool_job(self) -> str | None:
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            candidate = await session.scalar(
                select(ToolJob)
                .where(
                    ToolJob.status.in_(["PENDING", "RETRYING", "RUNNING"]),
                    (ToolJob.next_attempt_at.is_(None))
                    | (ToolJob.next_attempt_at <= now),
                    (ToolJob.lease_expires_at.is_(None))
                    | (ToolJob.lease_expires_at < now),
                )
                .order_by(ToolJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            run = await session.get(Run, candidate.run_id)
            if run is None or run.status in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            }:
                candidate.status = "CANCELLED"
                candidate.error = "所属任务已结束"
                candidate.lease_owner = None
                candidate.lease_expires_at = None
                candidate.updated_at = now
                return None
            candidate.status = "RUNNING"
            candidate.attempts += 1
            candidate.next_attempt_at = None
            candidate.lease_owner = self.worker_id
            candidate.lease_expires_at = now + timedelta(
                seconds=self.settings.tool_job_lease_seconds
            )
            candidate.updated_at = now
            await append_event(
                session,
                candidate.run_id,
                "TOOL_JOB_STARTED",
                {
                    "job_id": candidate.id,
                    "tool": candidate.tool_name,
                    "attempt": candidate.attempts,
                },
            )
            return candidate.id

    async def _execute_tool_job(self, job_id: str) -> None:
        started = time.perf_counter()
        async with self.database.sessions() as session:
            job = await session.get(ToolJob, job_id)
            if (
                job is None
                or job.status != "RUNNING"
                or job.lease_owner != self.worker_id
            ):
                return
            run = await session.get(Run, job.run_id)
            if run is None:
                return
            tool_name = job.tool_name
            arguments = dict(job.arguments)
            ordinal = job.step_ordinal
            operation_key = job.idempotency_key
            attempt = job.attempts
            max_attempts = job.max_attempts
        definition = self.registry.get(tool_name)
        try:
            raw_output = await self._dispatch_tool(
                run=run,
                tool=tool_name,
                args=arguments,
                ordinal=ordinal,
                timeout_seconds=definition.timeout_seconds,
                operation_key=operation_key,
                continuation=None,
            )
            output = self.registry.validate_output(
                tool_name,
                raw_output,
                arguments,
                run_id=run.id,
            )
        except Exception as exc:
            transient = _is_transient_exception(exc)
            now = utc_now()
            async with self.database.sessions() as session, session.begin():
                current = await session.get(ToolJob, job_id, with_for_update=True)
                if (
                    current is None
                    or current.lease_owner != self.worker_id
                    or current.status != "RUNNING"
                ):
                    return
                terminal = not transient or attempt >= max_attempts
                current.status = "DEAD_LETTER" if terminal else "RETRYING"
                current.error = str(exc)[:4_000]
                current.lease_owner = None
                current.lease_expires_at = None
                current.next_attempt_at = (
                    None
                    if terminal
                    else now
                    + timedelta(seconds=min(60, 2 ** max(0, attempt - 1)))
                )
                current.dead_lettered_at = now if terminal else None
                current.updated_at = now
                owning_run = await session.get(Run, current.run_id)
                if owning_run is not None and owning_run.status == "WAITING_DEPENDENCY":
                    owning_run.retry_after = (
                        now if terminal else current.next_attempt_at
                    )
                    owning_run.updated_at = now
                await append_event(
                    session,
                    current.run_id,
                    "TOOL_JOB_DEAD_LETTERED"
                    if terminal
                    else "TOOL_JOB_RETRYING",
                    {
                        "job_id": current.id,
                        "tool": current.tool_name,
                        "attempt": current.attempts,
                        "max_attempts": current.max_attempts,
                        "error": current.error,
                        "next_attempt_at": (
                            current.next_attempt_at.isoformat()
                            if current.next_attempt_at
                            else None
                        ),
                    },
                )
            return

        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            current = await session.get(ToolJob, job_id, with_for_update=True)
            if (
                current is None
                or current.lease_owner != self.worker_id
                or current.status != "RUNNING"
            ):
                return
            owning_run = await session.get(Run, current.run_id, with_for_update=True)
            if owning_run is None or owning_run.status == "CANCELLED":
                current.status = "CANCELLED"
                current.error = "所属任务已取消，迟到结果被拒绝"
            else:
                current.status = "COMPLETED"
                current.result = output
                current.error = None
                owning_run.tool_duration_ms = int(
                    owning_run.tool_duration_ms or 0
                ) + elapsed_ms
                if owning_run.status == "WAITING_DEPENDENCY":
                    owning_run.retry_after = now
                    owning_run.updated_at = now
            current.lease_owner = None
            current.lease_expires_at = None
            current.next_attempt_at = None
            current.updated_at = now
            await append_event(
                session,
                current.run_id,
                "TOOL_JOB_COMPLETED"
                if current.status == "COMPLETED"
                else "TOOL_JOB_CANCELLED",
                {
                    "job_id": current.id,
                    "tool": current.tool_name,
                    "attempt": current.attempts,
                    "duration_ms": elapsed_ms,
                },
            )

    async def _execute_run(self, run_id: str) -> None:
        # 这是在跑任务的同时，后台另开一个“续租”协程。防止长任务租约过期被别人重复执行
        lease_task = asyncio.create_task(self._renew_run_lease(run_id))
        try:
            # 加载 Run 和历史记录。
            run, history, memories, tenant_id = await self._load_run_and_history(run_id)
            # 确保 Run 的运行时身份。
            run = await self._ensure_runtime_identity(run)
            conversation_workspace = await self._load_conversation_workspace(run)
            # Defer Run↔Goal attach until TurnPlan / GoalResolver decide.
            # Prematurely binding the latest ACTIVE goal is the main reason
            # interleaved multi-goal dialogue mutates the wrong draft.
            goal = await self._load_bound_goal(run)
            if goal is None:
                goal = await self._find_goal_with_pending_clarification(run)
            planning_prompt = run.prompt
            intent_delta = None
            target_resolved_from_clarification = False
            # ── goal-level clarification resolution ──────────────────────
            # When the previous run asked "你指的是哪一个任务？" and the
            # user replies with a selection, resolve it before entering the
            # normal intent pipeline.
            goal_resolved_from_clarification = False
            if (goal is None or not goal.pending_clarification) and not run.plan:
                goal_resolution = await self._load_pending_goal_resolution(run)
                if goal_resolution is not None:
                    selected_goal_id = self.goal_resolver.resolve_selection(
                        message=run.prompt,
                        candidates=goal_resolution.candidates,
                    )
                    if selected_goal_id is not None:
                        goal = await self._attach_run_to_goal(
                            run=run,
                            goal_id=selected_goal_id,
                        )
                        conversation_workspace = conversation_workspace.model_copy(
                            update={
                                "active_goal_ref": f"goal:{goal.goal_id}",
                                "target_context": goal.target_context,
                            }
                        )
                        await self._clear_goal_resolution(run)
                        goal_resolved_from_clarification = True
            if goal is not None:
                conversation_workspace = conversation_workspace.model_copy(
                    update={
                        "active_goal_ref": f"goal:{goal.goal_id}",
                        "active_target": None,
                        "target_context": goal.target_context,
                    }
                )
            # ── temporal clarification resume ───────────────────────────
            # A prior UPDATE_SCHEDULE turn asked for a concrete time. The
            # follow-up carries only the time expression; restore the bound
            # Goal + IntentDelta from PostgreSQL (not process memory).
            if (
                goal is not None
                and goal.pending_clarification is not None
                and goal.pending_clarification.kind == "TEMPORAL_SCHEDULE"
            ):
                intent_delta = await self._load_intent_delta_by_id(
                    goal.pending_delta_id or goal.pending_clarification.delta_id
                )
                if intent_delta is None:
                    raise RuntimeError("无法恢复待澄清的 IntentDelta（发布时间）")
                follow_up = run.prompt.strip()
                planning_prompt = follow_up
                refreshed = dict(intent_delta.delta or {})
                refreshed["message"] = follow_up
                refreshed["schedule_request"] = follow_up
                changes = list(refreshed.get("changes") or [])
                updated_changes = []
                for item in changes:
                    if not isinstance(item, dict):
                        updated_changes.append(item)
                        continue
                    payload = dict(item.get("payload") or {})
                    if item.get("role") == "SCHEDULE" and item.get("op") == "UPDATE":
                        payload["schedule_request"] = follow_up
                        payload["message"] = follow_up
                        payload.pop("run_at", None)
                    updated_changes.append({**item, "payload": payload})
                if updated_changes:
                    refreshed["changes"] = updated_changes
                intent_delta = intent_delta.model_copy(update={"delta": refreshed})
                goal = await self._attach_run_to_goal(run=run, goal_id=goal.goal_id)
                await self._clear_pending_clarification(run.id, goal.goal_id)
                goal = goal.model_copy(
                    update={
                        "status": "ACTIVE",
                        "pending_clarification": None,
                        "pending_delta_id": None,
                    }
                )
                target_resolved_from_clarification = True
            # ── target-level clarification resolution ────────────────────
            elif (
                goal is not None
                and goal.pending_clarification is not None
                and self.turn_intent_parser.read_operation(run.prompt) is None
            ):
                selected = self.target_resolver.resolve_selection(
                    message=run.prompt,
                    clarification=goal.pending_clarification,
                )
                if selected is None:
                    # The user is allowed to abandon a clarification and issue
                    # a fresh request.  A PendingClarification is not a global
                    # conversation lock; only a resolvable selection resumes
                    # the suspended delta.
                    await self._supersede_pending_clarification(
                        run_id=run.id,
                        goal_id=goal.goal_id,
                    )
                    goal = goal.model_copy(
                        update={
                            "status": "ACTIVE",
                            "pending_clarification": None,
                            "pending_delta_id": None,
                        }
                    )
                    intent_delta = None
                else:
                    selected_binding = await self._bind_selected_target(
                        run=run,
                        candidate=selected,
                    )
                    goal = goal.model_copy(
                        update={
                            "target_context": self._merge_target_context(
                                goal.target_context,
                                selected_binding,
                            ),
                        }
                    )
                    intent_delta = await self._load_intent_delta_by_id(
                        goal.pending_delta_id or goal.pending_clarification.delta_id
                    )
                    if intent_delta is None:
                        raise RuntimeError("无法恢复待澄清的 IntentDelta")
                    planning_prompt = str(
                        intent_delta.delta.get("message") or run.prompt
                    )
                    target_resolved_from_clarification = True
                    await self._clear_pending_clarification(run.id, goal.goal_id)
            else:
                intent_delta = await self._load_intent_delta(run)
            if goal is not None:
                conversation_workspace = conversation_workspace.model_copy(
                    update={
                        "active_target": None,
                        "target_context": goal.target_context,
                    }
                )
            workspace_context = conversation_workspace.model_context()
            continuation_draft = self.llm._workspace_draft(workspace_context)
            referenced_entities = list(
                dict(run.checkpoint or {}).get("referenced_entities") or []
            )
            # 先“想起来以前相关做过什么”，再决定怎么回答/规划
            recalled_memories = await self._recall_task_memory(
                run=run,
                tenant_id=tenant_id,
            )
            # Phase 2 control-plane router: classify QUERY/ACTION/CHAT before
            # Adaptive Router / GoalResolver. Resume and clarification paths skip
            # reclassification so mid-flight ACTION state is preserved.
            if (
                not run.plan
                and intent_delta is None
                and not target_resolved_from_clarification
                and not goal_resolved_from_clarification
            ):
                frozen_for_route = (
                    CommunityIntent.model_validate(run.intent_detail)
                    if run.intent_detail
                    else None
                )
                rejected_for_route = dict(run.checkpoint or {}).get("invalid_plan")
                if not (
                    frozen_for_route is not None and isinstance(rejected_for_route, dict)
                ):
                    route = self.control_router.classify(planning_prompt)
                    await self._record_control_plane_route(run_id, route)
                    if route.mode == "QUERY":
                        await self._enter_query_path(
                            run_id=run_id,
                            run=run,
                            history=history,
                            memories=memories,
                            recalled_memories=recalled_memories,
                            route=route,
                        )
                        return
                    if route.mode == "CHAT":
                        await self._enter_chat_path(
                            run_id=run_id,
                            run=run,
                            history=history,
                            memories=memories,
                            recalled_memories=recalled_memories,
                            route=route,
                        )
                        return
                    # ACTION: fall through to the legacy Adaptive Router path.
            execution_path: ExecutionPath # 执行路径
            if run.plan:
                plan = AgentPlan.model_validate(run.plan) # 解析 Run 的计划。
                execution_path = (
                    run.execution_path
                    if run.execution_path
                    in {"DIRECT", "TOOL", "CREATOR", "ORCHESTRATED"}
                    else "ORCHESTRATED"
                )
            else: # 没有计划，需要决策。
                # If planning was interrupted, the already understood intent is
                # part of the checkpoint. Do not ask the router to reinterpret
                # the same prompt against newer conversation state: that can
                # silently turn a new goal into an operation on an old draft.
                frozen_intent = (
                    CommunityIntent.model_validate(run.intent_detail)
                    if run.intent_detail
                    else None
                )
                rejected_plan = dict(run.checkpoint or {}).get("invalid_plan")
                if (
                    intent_delta is not None
                    and target_resolved_from_clarification
                ):
                    # Clarification resume: restore IntentDelta-bound intent
                    # without re-asking the Adaptive Router / LLM.
                    intent = CommunityIntent(
                        domain=str(
                            intent_delta.delta.get("intent_domain") or "content_publish"
                        ),
                        goal=str(
                            intent_delta.delta.get("intent_goal") or planning_prompt
                        ),
                        required_capabilities=["schedule_publish"]
                        if intent_delta.operation
                        in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "PUBLISH_NOW"}
                        else ["generation"],
                        risk="high",
                        confidence=float(intent_delta.confidence or 1.0),
                    )
                    decision = AdaptiveExecutionDecision(
                        execution_path="ORCHESTRATED",
                        classification_summary="恢复澄清后的目标增量并继续执行",
                        intent=intent,
                        turn_relation="MODIFY",
                        primary_operation=intent_delta.operation,
                        referenced_entities=referenced_entities,
                    )
                elif frozen_intent is not None and isinstance(rejected_plan, dict):
                    plan = AgentPlan.model_validate(rejected_plan).model_copy(
                        update={"intent_detail": frozen_intent}
                    )
                    decision = AdaptiveExecutionDecision(
                        execution_path="ORCHESTRATED",
                        classification_summary=(
                            run.summary or "恢复已冻结的用户目标并继续编译执行计划"
                        ),
                        intent=frozen_intent,
                        turn_relation="NEW_GOAL",
                        referenced_entities=referenced_entities,
                    )
                    intent = frozen_intent
                else:
                    decision = self.llm.deterministic_execution(
                        prompt=planning_prompt,
                        client_timezone=run.client_timezone,
                        continuation_draft=continuation_draft,
                        conversation_workspace=workspace_context,
                    )
                    if decision is None:
                        await self._consume_budget(run_id, "model")
                        # 调用 LLM 决策。
                        decision = await self._track_duration(
                            run_id,
                            "model_duration_ms",
                            self.llm.decide_execution(
                                 prompt=planning_prompt,
                                context_post_id=run.context_post_id,
                                context_comment_id=run.context_comment_id,
                                client_timezone=run.client_timezone,
                                history=history,
                                memories=memories,
                                recalled_memories=recalled_memories,
                                continuation_draft=continuation_draft,
                                conversation_workspace=workspace_context,
                                on_structured_retry=lambda: self._structured_output_retry(
                                    run_id, "Adaptive Router"
                                ),
                            ),
                        )
                    else:
                        await self._record_deterministic_route(
                            run_id,
                            decision.classification_summary,
                        )
                    intent = decision.intent
                    referenced_entities = list(decision.referenced_entities)

                # ── PREVIEW mode ──────────────────────────────────────────
                # LLM recognised the user wants a preview before executing.
                # Route to DIRECT: generate a plain-text plan, no tools run.
                if getattr(decision, "interaction_mode", "EXECUTE") == "PREVIEW":
                    await self._enter_chat_path(
                        run_id=run_id,
                        run=run,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        route=RouteDecision(
                            mode="CHAT",
                            domain="general",
                            confidence=0.9,
                            summary="用户要求先预览再执行",
                        ),
                    )
                    return

                turn_intent = None
                turn_plan = None
                if intent_delta is None:
                    focus_goal_refs = list(
                        conversation_workspace.focus_goal_refs
                        or (
                            [conversation_workspace.active_goal_ref]
                            if conversation_workspace.active_goal_ref
                            else []
                        )
                    )
                    # Phase 3 continuity:
                    # Router(ACTION) → TaskManager(lifecycle) → TargetResolver(Layer A)
                    # → Goal bind → TargetResolver.resolve(Layer B) → Planner
                    preflight_decision, preflight_task_turn = (
                        self.task_manager.prepare_action(
                            message=planning_prompt,
                            decision=decision,
                            goals=[],
                            focus_goal_refs=[],
                        )
                    )
                    if preflight_task_turn.action == "CREATE":
                        # CREATE must not resolve against historical goals.
                        conversation_goals = []
                        decision, task_turn = (
                            preflight_decision,
                            preflight_task_turn,
                        )
                    else:
                        conversation_goals = await self._load_goals_for_resolution(run)
                        decision, task_turn = self.task_manager.prepare_action(
                            message=planning_prompt,
                            decision=decision,
                            goals=conversation_goals,
                            focus_goal_refs=focus_goal_refs,
                        )
                    if task_turn.action in {"UPDATE", "CANCEL"}:
                        entity_resolution = self.task_manager.resolve_target(
                            message=planning_prompt,
                            goals=conversation_goals,
                            focus_goal_refs=focus_goal_refs,
                            conversation_context=workspace_context,
                            candidate_targets=list(conversation_workspace.entities),
                            artifacts=[
                                item.model_dump(mode="json")
                                for item in conversation_workspace.entities
                            ],
                        )
                        await self._record_entity_target_resolution(
                            run_id,
                            entity_resolution,
                        )
                        if entity_resolution.resolution_method == "AMBIGUOUS":
                            await self._wait_for_goal_resolution(
                                run=run,
                                resolution=self._goal_resolution_from_entity(
                                    entity_resolution
                                ),
                            )
                            return
                        selected_task = next(
                            (
                                item
                                for item in self.task_manager.list_active_tasks(
                                    conversation_goals,
                                    focus_goal_refs,
                                )
                                if item.task_id == entity_resolution.task_id
                            ),
                            None,
                        )
                        if selected_task is None and entity_resolution.task_id:
                            matched_goal = next(
                                (
                                    item
                                    for item in conversation_goals
                                    if item.goal_id == entity_resolution.task_id
                                ),
                                None,
                            )
                            if matched_goal is not None:
                                selected_task = self.task_manager._to_task(matched_goal)
                        if selected_task is None:
                            await self._wait_for_goal_resolution(
                                run=run,
                                resolution=self._goal_resolution_from_entity(
                                    entity_resolution
                                ),
                            )
                            return
                        task_turn = self.task_manager.bind_resolved_target(
                            message=planning_prompt,
                            action=task_turn.action,
                            task=selected_task,
                        )
                        if task_turn.turn_relation_override is not None:
                            decision = decision.model_copy(
                                update={
                                    "turn_relation": task_turn.turn_relation_override
                                }
                            )
                        focus_goal_refs = self.task_manager.focus_refs_for_active(
                            selected_task,
                            focus_goal_refs,
                        )
                    active_task = task_turn.task or self.task_manager.resolve_active_task(
                        conversation_goals,
                        focus_goal_refs,
                    )
                    focus_goal_refs = self.task_manager.focus_refs_for_active(
                        active_task,
                        focus_goal_refs,
                    )
                    has_established_goals = (
                        True
                        if task_turn.force_has_target is True
                        else (
                            False
                            if task_turn.force_has_target is False
                            else any(
                                self._goal_is_established(item)
                                for item in conversation_goals
                            )
                        )
                    )
                    turn_intent, turn_plan, goal_resolution = self.turn_pipeline.interpret(
                        message=planning_prompt,
                        decision=decision,
                        conversation_goals=conversation_goals,
                        has_established_goals=has_established_goals,
                        focus_goal_refs=focus_goal_refs,
                    )
                    turn_intent, goal_resolution, task_turn = (
                        self.task_manager.adapt_goal_resolution(
                            message=planning_prompt,
                            turn_intent=turn_intent,
                            goal_resolution=goal_resolution,
                            goals=conversation_goals,
                            focus_goal_refs=focus_goal_refs,
                            prior=task_turn,
                        )
                    )
                    # TaskManager lifecycle (esp. CANCEL) may override the
                    # operation while TurnPlanBuilder still emitted OPEN_PLAN
                    # for underspecified utterances like "把这个任务取消".
                    if (
                        task_turn.operation_override
                        and turn_plan is not None
                        and task_turn.operation_override
                        != primary_operation_from_changes(
                            turn_plan.changes,
                            open_plan=turn_plan.open_plan,
                        )
                    ):
                        turn_plan = turn_plan.model_copy(
                            update={
                                "changes": changes_from_operation(
                                    task_turn.operation_override,
                                    message=planning_prompt,
                                ),
                                "open_plan": False,
                            }
                        )
                        turn_intent = turn_intent.model_copy(
                            update={
                                "operation": task_turn.operation_override,  # type: ignore[arg-type]
                                "operation_class": (
                                    "SIDE_EFFECT"
                                    if task_turn.operation_override
                                    in {
                                        "UPDATE_SCHEDULE",
                                        "PUBLISH_NOW",
                                        "CANCEL_SCHEDULE",
                                    }
                                    else turn_intent.operation_class
                                ),
                            }
                        )
                    if task_turn.action != "PASS":
                        await self._record_task_manager_decision(
                            run_id,
                            task_turn,
                        )
                    if turn_plan.tasks:
                        await self._checkpoint_task_bag(
                            run_id=run.id,
                            primary_summary=turn_plan.raw_message,
                            follow_ups=[
                                item.raw_message for item in turn_plan.tasks if item.raw_message
                            ],
                        )
                    if goal_resolution.outcome == "RESOLVED":
                        if goal is None or goal_resolution.goal_id != goal.goal_id:
                            goal = await self._attach_run_to_goal(
                                run=run,
                                goal_id=str(goal_resolution.goal_id),
                            )
                            continuation_draft = None
                            conversation_workspace = conversation_workspace.model_copy(
                                update={
                                    "active_goal_ref": f"goal:{goal.goal_id}",
                                    "active_target": None,
                                    "target_context": goal.target_context,
                                }
                            )
                            workspace_context = conversation_workspace.model_context()
                    elif goal_resolution.outcome == "NEW_GOAL":
                        if goal is not None and self._goal_is_established(goal):
                            goal = await self._start_conversation_goal(
                                run=run,
                                intent=intent,
                                summary=turn_intent.semantic_subject or intent.goal,
                            )
                        elif goal is not None:
                            goal = await self._initialize_goal_metadata(
                                run=run,
                                goal=goal,
                                intent=intent,
                                summary=turn_intent.semantic_subject or intent.goal,
                            )
                        else:
                            goal = await self._start_conversation_goal(
                                run=run,
                                intent=intent,
                                summary=turn_intent.semantic_subject or intent.goal,
                            )
                        continuation_draft = None
                        conversation_workspace = conversation_workspace.model_copy(
                            update={
                                "active_goal_ref": f"goal:{goal.goal_id}",
                                "active_target": None,
                                "target_context": TargetContext(),
                            }
                        )
                        workspace_context = conversation_workspace.model_context()
                    else:
                        # Status questions about a shared title should report every
                        # matching Goal instead of asking "A/B" with identical labels.
                        if str(turn_intent.operation).startswith("QUERY_"):
                            await self._answer_ambiguous_goal_query(
                                run=run,
                                turn_intent=turn_intent,
                                resolution=goal_resolution,
                                goals=conversation_goals,
                            )
                            return
                        await self._wait_for_goal_resolution(
                            run=run,
                            resolution=goal_resolution,
                        )
                        return
                if goal is None:
                    raise RuntimeError("ConversationGoal missing after turn resolution")
                temporal_current = await self._temporal_anchor_time(run)
                existing_schedule_run_at = await self._load_existing_schedule_run_at(goal)
                if turn_plan is not None and intent_delta is None:
                    turn_plan, temporal_resolution = self._apply_schedule_temporal(
                        turn_plan=turn_plan,
                        message=planning_prompt,
                        current_time=temporal_current,
                        existing_run_at=existing_schedule_run_at,
                        timezone=run.client_timezone or "Asia/Shanghai",
                    )
                    if (
                        temporal_resolution is not None
                        and temporal_resolution.run_at is None
                        and self._is_pure_schedule_time_update(turn_plan)
                    ):
                        pipeline_result = self.turn_pipeline.bind_and_compile(
                            turn_plan=turn_plan,
                            goal=goal,
                            run_id=run.id,
                            message_id=str(
                                dict(run.checkpoint or {}).get("message_id") or run.id
                            ),
                            intent=decision.intent,
                            target_context=goal.target_context,
                            intent_domain=decision.intent.domain,
                            intent_goal=decision.intent.goal,
                            client_timezone=run.client_timezone,
                            current_time=temporal_current,
                            existing_run_at=existing_schedule_run_at,
                        )
                        await self._wait_for_temporal_clarification(
                            run_id=run.id,
                            goal=goal,
                            resolution=temporal_resolution,
                            original_message=planning_prompt,
                            intent_delta=pipeline_result.intent_delta,
                        )
                        return
                if intent_delta is None:
                    if turn_intent is None or turn_plan is None:
                        raise RuntimeError("TurnPlan missing before IntentDelta binding")
                    pipeline_result = self.turn_pipeline.bind_and_compile(
                        turn_plan=turn_plan,
                        goal=goal,
                        run_id=run.id,
                        message_id=str(
                            dict(run.checkpoint or {}).get("message_id") or run.id
                        ),
                        intent=decision.intent,
                        target_context=goal.target_context,
                        intent_domain=decision.intent.domain,
                        intent_goal=decision.intent.goal,
                        client_timezone=run.client_timezone,
                        current_time=temporal_current,
                        existing_run_at=existing_schedule_run_at,
                    )
                    intent_delta = await self._persist_intent_delta_model(
                        run=run,
                        intent_delta=pipeline_result.intent_delta,
                    )
                if (
                    intent_delta is not None
                    and not target_resolved_from_clarification
                    and self._allows_target_state_write(intent_delta.operation_class)
                    and intent_delta.operation not in {"CREATE_POST", "OPEN_PLAN"}
                ):
                    resolution = self.target_resolver.resolve(
                        message=planning_prompt,
                        intent_delta=intent_delta,
                        goal=goal,
                        workspace=conversation_workspace,
                        artifacts=[
                            item.model_dump(mode="json")
                            for item in conversation_workspace.entities
                            if not item.goal_id or item.goal_id == goal.goal_id
                        ],
                        target_history=await self._load_target_history(run),
                    )
                    if resolution.error is not None:
                        raise RuntimeError(resolution.error)
                    if resolution.clarification is not None:
                        await self._wait_for_clarification(
                            run_id=run.id,
                            goal=goal,
                            clarification=resolution.clarification,
                        )
                        return
                    if resolution.selected is not None:
                        active_target = await self._bind_selected_target(
                            run=run,
                            candidate=resolution.selected,
                        )
                        target_context = self._merge_target_context(
                            conversation_workspace.target_context,
                            active_target,
                        )
                        conversation_workspace = conversation_workspace.model_copy(
                            update={
                                "active_target": active_target,
                                "target_context": target_context,
                            }
                        )
                        workspace_context = conversation_workspace.model_context()
                # 把 模型刚理解出的意图 持久化到数据库，并通知前端。
                await self._save_intent(run_id, intent.model_dump(mode="json"))
                # 写进 Run 的 intent_detail 字段，冻结意图细节。
                run.intent_detail = intent.model_dump(mode="json")
                if (
                    self._allows_target_state_write(intent_delta.operation_class)
                    and intent_delta.operation != "OPEN_PLAN"
                ):
                    await self._sync_goal_intent(run, intent)
                if goal.phase == "PUBLISHED" and intent_delta.operation in {
                    "APPEND_CONTENT",
                    "REPLACE_CONTENT",
                    "UPDATE_TITLE",
                    "UPDATE_SCHEDULE",
                    "CANCEL_SCHEDULE",
                    "PUBLISH_NOW",
                }:
                    title = next(iter(goal.artifact_titles or []), "") or "该帖子"
                    raise PermanentToolError(
                        f"《{title}》已经发布，不能再修改草稿内容或定时任务。"
                        "如需补充实战经验，请新开一篇帖子。"
                    )
                # ChangeCompiler: composable content±schedule mutations.
                temporal_current = await self._temporal_anchor_time(run)
                existing_schedule_run_at = await self._load_existing_schedule_run_at(goal)
                delta_plan = self.intent_delta_plan_compiler.compile(
                    intent_delta=intent_delta,
                    target_context=conversation_workspace.target_context,
                    intent=intent,
                    client_timezone=run.client_timezone,
                    current_time=temporal_current,
                    existing_run_at=existing_schedule_run_at,
                )
                if (
                    delta_plan is None
                    and intent_delta is not None
                    and intent_delta.operation == "UPDATE_SCHEDULE"
                ):
                    # Pure schedule time update without solidified run_at must
                    # clarify — never fall through to LLM guessing.
                    await self._wait_for_temporal_clarification(
                        run_id=run.id,
                        goal=goal,
                        resolution=TemporalResolution(
                            mode="AMBIGUOUS",
                            timezone=run.client_timezone or "Asia/Shanghai",
                            confidence=0.8,
                            source_text=planning_prompt[:120],
                            error_code="UNRESOLVED_SCHEDULE_TIME",
                            error="无法确定新的发布时间，请给出具体时间",
                        ),
                        original_message=planning_prompt,
                        intent_delta=intent_delta,
                    )
                    return
                if delta_plan is not None:
                    execution_path = "ORCHESTRATED"
                    plan = delta_plan
                    await self._record_deterministic_route(
                        run_id,
                        f"Goal Delta {intent_delta.operation} 使用确定性增量执行计划",
                    )
                elif frozen_intent is None or not isinstance(rejected_plan, dict):
                    execution_path, plan = normalize_execution_decision(
                        decision, self.registry
                    )
                else:
                    execution_path = "ORCHESTRATED"
                if execution_path == "ORCHESTRATED" and not plan.steps:
                    # 如果模型刚“猜”的路径是编排，并且没有步骤，则需要规划。
                    # 这是在 真正调大模型之前先扣预算，防止一次任务无限打模型。
                    await self._consume_budget(run_id, "model")
                    # 调用 LLM 规划。
                    plan = await self._track_duration(
                        run_id,
                        "model_duration_ms",
                        self.llm.plan(
                             prompt=planning_prompt,
                            context_post_id=run.context_post_id,
                            context_comment_id=run.context_comment_id,
                            client_timezone=run.client_timezone,
                            history=history,
                            memories=memories,
                            recalled_memories=recalled_memories,
                            structured_intent=intent,
                            continuation_draft=continuation_draft,
                            conversation_workspace=workspace_context,
                            referenced_entities=referenced_entities,
                            conversation_goal=goal,
                            intent_delta=intent_delta,
                            target_context=goal.target_context,
                            on_structured_retry=lambda: self._structured_output_retry(
                                run_id, "Planner"
                            ),
                        ),
                    )
                if execution_path != "DIRECT":
                    plan = await self._compile_or_replan(
                        run_id=run_id,
                        run=run,
                        plan=plan,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        continuation_draft=continuation_draft,
                        conversation_workspace=workspace_context,
                        referenced_entities=referenced_entities,
                        conversation_goal=goal,
                        intent_delta=intent_delta,
                        target_context=goal.target_context,
                        planning_prompt=planning_prompt,
                    )
                    execution_path = normalize_compiled_path(
                        execution_path,
                        plan,
                        self.registry,
                    )
                # 执行计划持久化到数据库，并通知前端“计划已生成”。主要写到 assistant_runs（模型 Run）。
                # 更新的是这一行 run 上的字段：plan、plan_hash、intent、summary、task_ledger、progress_ledger 等。
                await self._save_plan(run_id, plan)
                run.plan = plan.model_dump(mode="json")

                # 这是在给任务分 工作车道（workload lane）：READ 还是 WRITE。READ 是只读，WRITE 是只写。
                lane = workload_lane(
                    path=execution_path,
                    plan=plan,
                    registry=self.registry,
                    persists_comment_reply=bool(
                        run.context_comment_id and run.context_post_id
                    ),
                )
                # 这是在 进入工作车道（workload lane）：READ 还是 WRITE。READ 是只读，WRITE 是只写。
                entered_lane = await self._enter_workload_lane(
                    run_id=run_id,
                    path=execution_path,
                    lane=lane,
                    classification_summary=decision.classification_summary,
                    direct_response=decision.direct_response,
                )
                if not entered_lane:
                    return
                run.execution_path = execution_path
                run.workload_lane = lane
                run.checkpoint = {
                    **dict(run.checkpoint or {}),
                    "execution_path": execution_path,
                    "workload_lane": lane,
                    "classification_summary": decision.classification_summary,
                    "turn_relation": decision.turn_relation,
                    "referenced_entities": referenced_entities,
                    **(
                        {"direct_response": decision.direct_response}
                        if decision.direct_response
                        else {}
                    ),
                }
            # 如果工作车道是 ROUTING，则需要恢复已有任务并进入受控执行通道。
            # 这是给 已经有 plan、但还停在 ROUTING 的任务 补一次“进车道”。
            # 因为 编排器 只负责“猜”路径，但不知道任务 实际能不能执行。
            # 所以猜完还得“进车道”，让助手真正 尝试执行 一次，验证是否可行。
            # 如果不行，则取消任务；如果可以，则继续执行。
            if run.workload_lane == "ROUTING":
                lane = workload_lane(
                    path=execution_path,
                    plan=plan,
                    registry=self.registry,
                    persists_comment_reply=bool(
                        run.context_comment_id and run.context_post_id
                    ),
                )
                entered_lane = await self._enter_workload_lane(
                    run_id=run_id,
                    path=execution_path,
                    lane=lane,
                    classification_summary="恢复已有任务并进入受控执行通道",
                    direct_response=None,
                )
                if not entered_lane:
                    return
                run.execution_path = execution_path
                run.workload_lane = lane

            outputs: list[dict[str, Any]] = []
            seen_revision_signatures = {self._plan_step_signature(plan)}
            # 分层跑工具（读并行、写串行）→ 编排任务还要模型验收 → 不行就改计划再跑。
            # 这就是和经典纯 ReAct“每步现想”不同的 Plan-and-Execute + 校验重规划 核心。
            while True:
                progress_replan_focus: str | None = None
                # 给计划里的每个步骤分配一个唯一的 ordinal（序号）。
                # 这是为了后面 按顺序执行步骤 用的。
                ordinals = await self._step_ordinals(run_id, plan)
                # 按层级（execution_layers）分批处理步骤。
                # 这是为了 避免一次性处理太多步骤，导致内存占用过高。
                for layer_index, layer in enumerate(plan.execution_layers(), start=1):
                    # 记录每个步骤的输出。
                    # 这是为了后面 检查步骤依赖 用的。
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
                    if (
                        requires_verification(execution_path)
                        and self._should_review_progress(
                            plan,
                            completed_layer_index=layer_index,
                        )
                    ):
                        completed_ids = [
                            str(item["task_id"])
                            for item in outputs
                            if item.get("task_id")
                        ]
                        all_ids = [str(step.task_id) for step in plan.steps]
                        assessment_key = self._progress_assessment_key(
                            plan,
                            completed_ids,
                        )
                        progress = await self._load_progress_assessment(
                            run_id,
                            assessment_key,
                        )
                        if progress is None:
                            await self._consume_budget(run_id, "model")
                            progress = await self._track_duration(
                                run_id,
                                "model_duration_ms",
                                self.llm.assess_progress(
                                    prompt=planning_prompt,
                                    plan=plan,
                                    completed_task_ids=completed_ids,
                                    pending_task_ids=[
                                        task_id
                                        for task_id in all_ids
                                        if task_id not in set(completed_ids)
                                    ],
                                    tool_outputs=outputs,
                                    on_structured_retry=lambda: self._structured_output_retry(
                                        run_id, "Progress Supervisor"
                                    ),
                                ),
                            )
                            await self._save_progress_decision(
                                run_id,
                                progress,
                                assessment_key=assessment_key,
                            )
                        if progress.decision == "FAILED":
                            raise RuntimeError(progress.reason)
                        if (
                            progress.decision == "REPLAN"
                            or not progress.progress_made
                            or progress.in_loop
                        ):
                            progress_replan_focus = (
                                progress.next_focus or progress.reason
                            )
                            break

                if progress_replan_focus is not None:
                    await self._consume_budget(run_id, "replan")
                    await self._consume_budget(run_id, "model")
                    next_plan = await self._track_duration(
                        run_id,
                        "model_duration_ms",
                        self.llm.plan(
                            prompt=planning_prompt,
                            context_post_id=run.context_post_id,
                            context_comment_id=run.context_comment_id,
                            client_timezone=run.client_timezone,
                            history=history,
                            memories=memories,
                            recalled_memories=recalled_memories,
                            previous_execution={
                                "outputs": outputs,
                                "progress_decision": progress_replan_focus,
                            },
                            next_focus=progress_replan_focus,
                         structured_intent=plan.intent_detail,
                         conversation_goal=goal,
                         intent_delta=intent_delta,
                         target_context=goal.target_context,
                         planning_prompt=planning_prompt,
                         continuation_draft=continuation_draft,
                            conversation_workspace=workspace_context,
                            referenced_entities=referenced_entities,
                            on_structured_retry=lambda: self._structured_output_retry(
                                run_id, "Progress Replanner"
                            ),
                        ),
                    )
                    next_plan = await self._compile_or_replan(
                        run_id=run_id,
                        run=run,
                        plan=next_plan,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        continuation_draft=continuation_draft,
                        conversation_workspace=workspace_context,
                        referenced_entities=referenced_entities,
                        conversation_goal=goal,
                        intent_delta=intent_delta,
                        target_context=goal.target_context,
                        require_goal_coverage=False,
                    )
                    signature = self._plan_step_signature(next_plan)
                    if signature in seen_revision_signatures:
                        raise RuntimeError(
                            "任务重新规划后没有产生新的可执行动作，已停止重复循环"
                        )
                    seen_revision_signatures.add(signature)
                    plan = self._merge_replan(
                        plan,
                        next_plan,
                        completed_task_ids={
                            str(item["task_id"])
                            for item in outputs
                            if item.get("task_id")
                        },
                    )
                    await self._save_plan(run_id, plan, replanned=True)
                    continue

                if not requires_verification(execution_path):
                    break

                verification = deterministic_verification(
                    plan=plan,
                    outputs=outputs,
                    registry=self.registry,
                )
                if verification is None:
                    await self._consume_budget(run_id, "model")
                    verification = await self._track_duration(
                        run_id,
                        "model_duration_ms",
                        self.llm.verify(
                         prompt=planning_prompt,
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
                        prompt=planning_prompt,
                        context_post_id=run.context_post_id,
                        context_comment_id=run.context_comment_id,
                        client_timezone=run.client_timezone,
                        history=history,
                        memories=memories,
                        recalled_memories=recalled_memories,
                        previous_execution={"outputs": outputs},
                        next_focus=verification.next_focus,
                        structured_intent=plan.intent_detail,
                        conversation_goal=goal,
                        intent_delta=intent_delta,
                        target_context=goal.target_context,
                        continuation_draft=continuation_draft,
                        conversation_workspace=workspace_context,
                        referenced_entities=referenced_entities,
                        on_structured_retry=lambda: self._structured_output_retry(
                            run_id, "Planner"
                        ),
                    ),
                )
                next_plan = await self._compile_or_replan(
                    run_id=run_id,
                    run=run,
                    plan=next_plan,
                    history=history,
                    memories=memories,
                    recalled_memories=recalled_memories,
                    continuation_draft=continuation_draft,
                    conversation_workspace=workspace_context,
                    referenced_entities=referenced_entities,
                    conversation_goal=goal,
                    intent_delta=intent_delta,
                    target_context=goal.target_context,
                    planning_prompt=planning_prompt,
                    require_goal_coverage=False,
                )
                signature = self._plan_step_signature(next_plan)
                if signature in seen_revision_signatures:
                    raise RuntimeError(
                        "任务验收后生成了重复计划，已停止无进展循环"
                    )
                seen_revision_signatures.add(signature)
                plan = self._merge_replan(
                    plan,
                    next_plan,
                    completed_task_ids={
                        str(item["task_id"])
                        for item in outputs
                        if item.get("task_id")
                    },
                )
                await self._save_plan(run_id, plan, replanned=True)

            pending_response = (run.checkpoint or {}).get("pending_final_response")
            if isinstance(pending_response, str) and pending_response.strip():
                final_response = pending_response
            else:
                if execution_path == "DIRECT":
                    direct_response = (run.checkpoint or {}).get("direct_response")
                    if not isinstance(direct_response, str) or not direct_response.strip():
                        raise RuntimeError("DIRECT execution lost its checkpointed response")
                    final_response = direct_response.strip()
                elif execution_path == "CREATOR":
                    final_response = render_creator_result(outputs)
                elif plan.intent == "PUBLISH_CONTINUATION_DRAFT":
                    final_response = render_continuation_publish_result(outputs)
                else:
                    final_response = render_goal_delta_result(plan, outputs)
                    if final_response is None:
                        await self._consume_budget(run_id, "model")
                        final_response = await self._track_duration(
                            run_id,
                            "model_duration_ms",
                            self.llm.answer(
                                prompt=planning_prompt,
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
                reply_ordinal = await self._next_step_ordinal(run_id)
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
                    ordinal=reply_ordinal,
                    timeout_seconds=reply_definition.timeout_seconds,
                )
                outputs.append(
                    {
                        "ordinal": reply_ordinal,
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

    def _output_record(
        self,
        ordinal: int,
        planned_step: AgentPlanStep,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "task_id": str(planned_step.task_id),
            "agent": planned_step.agent,
            "tool": planned_step.tool,
            "artifact_type": self.registry.get(planned_step.tool).artifact_type,
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
            # 查询数据表assistant_runs，run_id是run_id，with_for_update=True表示加行锁，防止并发读写冲突。
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Run disappeared after claim")
            # 查询数据表assistant_messages，conversation_id是run.conversation_id，按照created_at降序排序，最多返回30条。
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == run.conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(30)
                )
            ).all()
            # 查询数据表assistant_user_memories，user_id是run.user_id，按照updated_at降序排序，最多返回20条。
            memories = (
                await session.scalars(
                    select(UserMemory)
                    .where(UserMemory.user_id == run.user_id)
                    .order_by(UserMemory.updated_at.desc())
                    .limit(20)
                )
            ).all()
            # 查询数据表assistant_conversations，conversation_id是run.conversation_id。
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

    async def _load_bound_goal(self, run: Run) -> ConversationGoal | None:
        """Load the Goal already attached to this Run, if any."""

        if not run.goal_id:
            return None
        async with self.database.sessions() as session:
            goal = await session.get(ConversationGoalRecord, run.goal_id)
            if goal is None:
                return None
            bindings = list(
                (
                    await session.scalars(
                        select(TargetBindingRecord)
                        .where(TargetBindingRecord.goal_id == goal.id)
                        .order_by(
                            TargetBindingRecord.version.desc(),
                            TargetBindingRecord.created_at.desc(),
                        )
                        .limit(30)
                    )
                ).all()
            )
            target_context = self._authoritative_target_context(goal, bindings)
            return ConversationGoal(
                goal_id=goal.id,
                conversation_id=goal.conversation_id,
                intent=goal.intent,
                summary=goal.summary,
                aliases=list(goal.aliases or []),
                status=goal.status,
                phase=goal.phase,
                active_target_ref=goal.active_target_ref,
                target_context=target_context,
                pending_clarification=(
                    PendingClarification.model_validate(goal.pending_clarification)
                    if goal.pending_clarification
                    else None
                ),
                pending_delta_id=goal.pending_delta_id,
                version=goal.version,
                updated_at=goal.updated_at,
            )

    async def _find_goal_with_pending_clarification(
        self,
        run: Run,
    ) -> ConversationGoal | None:
        """Find a Goal waiting on target clarification without attaching the Run."""

        async with self.database.sessions() as session:
            goal = await session.scalar(
                select(ConversationGoalRecord)
                .where(
                    ConversationGoalRecord.conversation_id == run.conversation_id,
                    ConversationGoalRecord.user_id == run.user_id,
                    ConversationGoalRecord.tenant_id == run.tenant_id,
                    ConversationGoalRecord.status == "WAITING_CLARIFICATION",
                    ConversationGoalRecord.pending_clarification.is_not(None),
                )
                .order_by(ConversationGoalRecord.updated_at.desc())
                .limit(1)
            )
            if goal is None:
                return None
            bindings = list(
                (
                    await session.scalars(
                        select(TargetBindingRecord)
                        .where(TargetBindingRecord.goal_id == goal.id)
                        .order_by(
                            TargetBindingRecord.version.desc(),
                            TargetBindingRecord.created_at.desc(),
                        )
                        .limit(30)
                    )
                ).all()
            )
            target_context = self._authoritative_target_context(goal, bindings)
            return ConversationGoal(
                goal_id=goal.id,
                conversation_id=goal.conversation_id,
                intent=goal.intent,
                summary=goal.summary,
                aliases=list(goal.aliases or []),
                status=goal.status,
                phase=goal.phase,
                active_target_ref=goal.active_target_ref,
                target_context=target_context,
                pending_clarification=(
                    PendingClarification.model_validate(goal.pending_clarification)
                    if goal.pending_clarification
                    else None
                ),
                pending_delta_id=goal.pending_delta_id,
                version=goal.version,
                updated_at=goal.updated_at,
            )

    async def _load_or_create_goal(
        self,
        run: Run,
    ) -> ConversationGoal:
        """Compatibility helper: bind a Goal only when the Run already has one,
        otherwise create a fresh aggregate (does not steal the latest ACTIVE).
        """

        bound = await self._load_bound_goal(run)
        if bound is not None:
            return bound
        return await self._start_conversation_goal(
            run=run,
            intent=CommunityIntent(
                domain="general_answer",
                goal="pending",
                required_capabilities=[],
                confidence=0.0,
            ),
            summary=None,
        )

    async def _attach_run_to_goal(
        self,
        *,
        run: Run,
        goal_id: str,
    ) -> ConversationGoal:
        """Attach the Run to a resolved Goal without mutating that Goal."""

        async with self.database.sessions() as session, session.begin():
            current_run = await session.get(Run, run.id, with_for_update=True)
            goal = await session.get(ConversationGoalRecord, goal_id)
            if current_run is None or current_run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker cannot attach resolved ConversationGoal")
            if (
                goal is None
                or goal.conversation_id != run.conversation_id
                or goal.user_id != run.user_id
                or goal.tenant_id != run.tenant_id
            ):
                raise RuntimeError("Resolved ConversationGoal is outside the current scope")
            current_run.goal_id = goal.id
            run.goal_id = goal.id
            bindings = list(
                (
                    await session.scalars(
                        select(TargetBindingRecord)
                        .where(TargetBindingRecord.goal_id == goal.id)
                        .order_by(
                            TargetBindingRecord.version.desc(),
                            TargetBindingRecord.created_at.desc(),
                        )
                        .limit(30)
                    )
                ).all()
            )
            context = self._authoritative_target_context(goal, bindings)
            return ConversationGoal(
                goal_id=goal.id,
                conversation_id=goal.conversation_id,
                intent=goal.intent,
                summary=goal.summary,
                aliases=list(goal.aliases or []),
                status=goal.status,
                phase=goal.phase,
                active_target_ref=goal.active_target_ref,
                target_context=context,
                pending_clarification=(
                    PendingClarification.model_validate(goal.pending_clarification)
                    if goal.pending_clarification
                    else None
                ),
                pending_delta_id=goal.pending_delta_id,
                version=goal.version,
                updated_at=goal.updated_at,
            )

    @staticmethod
    def _binding_from_record(binding: TargetBindingRecord) -> TargetBinding:
        return TargetBinding(
            target_type=binding.target_type,
            role=binding.role,
            target_id=binding.target_id,
            artifact_id=binding.artifact_id,
            content_sha256=binding.content_sha256,
            version=binding.version,
            confidence=binding.confidence,
            resolution_method=binding.resolution_method,
            schedule_id=binding.schedule_id,
            content_artifact_id=binding.content_artifact_id,
            content_artifact_version=binding.content_artifact_version,
        )

    @staticmethod
    def _goal_is_established(goal: ConversationGoal) -> bool:
        return bool(
            goal.intent != "UNKNOWN"
            or goal.phase != "DISCOVERING"
            or goal.active_target_ref
            or goal.target_context.content_target
            or goal.target_context.schedule_target
            or goal.target_context.publication_target
            or goal.target_context.interaction_target
        )

    async def _start_conversation_goal(
        self,
        *,
        run: Run,
        intent: CommunityIntent,
        summary: str | None = None,
    ) -> ConversationGoal:
        """Start a new aggregate instead of mutating an unrelated old Goal."""
        async with self.database.sessions() as session, session.begin():
            current_run = await session.get(Run, run.id, with_for_update=True)
            if current_run is None or current_run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker cannot start ConversationGoal")
            record = ConversationGoalRecord(
                conversation_id=run.conversation_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                intent=intent.domain.upper(),
                summary=(summary or intent.goal)[:500],
                aliases=[],
                status="ACTIVE",
                phase="DISCOVERING",
                target_context=TargetContext().model_dump(mode="json"),
                version=1,
            )
            session.add(record)
            await session.flush()
            current_run.goal_id = record.id
            run.goal_id = record.id
            await append_event(
                session,
                run.id,
                "CONVERSATION_GOAL_STARTED",
                {
                    "goal_id": record.id,
                    "goal_version": record.version,
                    "intent": intent.domain,
                },
            )
            return ConversationGoal(
                goal_id=record.id,
                conversation_id=record.conversation_id,
                intent=record.intent,
                summary=record.summary,
                aliases=list(record.aliases or []),
                status=record.status,
                phase=record.phase,
                active_target_ref=None,
                active_target=None,
                target_context=TargetContext(),
                version=record.version,
                updated_at=record.updated_at,
            )

    async def _initialize_goal_metadata(
        self,
        *,
        run: Run,
        goal: ConversationGoal,
        intent: CommunityIntent,
        summary: str,
    ) -> ConversationGoal:
        """Initialize a newly-created empty Goal without advancing its version."""

        async with self.database.sessions() as session, session.begin():
            record = await session.get(ConversationGoalRecord, goal.goal_id)
            if record is None:
                raise RuntimeError("ConversationGoal disappeared during initialization")
            record.intent = intent.domain.upper()
            record.summary = (summary or intent.goal)[:500]
            record.aliases = list(record.aliases or [])
            return goal.model_copy(
                update={
                    "intent": record.intent,
                    "summary": record.summary,
                    "aliases": list(record.aliases),
                }
            )

    async def _load_goals_for_resolution(self, run: Run) -> list[ConversationGoal]:
        """Load bounded, read-only Goal candidates and their artifact metadata."""

        async with self.database.sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(ConversationGoalRecord)
                        .where(
                            ConversationGoalRecord.conversation_id == run.conversation_id,
                            ConversationGoalRecord.user_id == run.user_id,
                            ConversationGoalRecord.tenant_id == run.tenant_id,
                        )
                        .order_by(ConversationGoalRecord.updated_at.desc())
                        .limit(24)
                    )
                ).all()
            )
            goal_ids = [record.id for record in records]
            run_rows = (
                list(
                    (
                        await session.scalars(
                            select(Run)
                            .where(
                                Run.goal_id.in_(goal_ids),
                                Run.id != run.id,
                            )
                            .order_by(Run.created_at.asc())
                            .limit(120)
                        )
                    ).all()
                )
                if goal_ids
                else []
            )
            run_ids = [item.id for item in run_rows]
            artifacts = (
                list(
                    (
                        await session.scalars(
                            select(Artifact)
                            .where(Artifact.run_id.in_(run_ids))
                            .order_by(Artifact.created_at.desc())
                            .limit(240)
                        )
                    ).all()
                )
                if run_ids
                else []
            )

        runs_by_goal: dict[str, list[Run]] = {}
        goal_by_run: dict[str, str] = {}
        for item in run_rows:
            if not item.goal_id:
                continue
            runs_by_goal.setdefault(item.goal_id, []).append(item)
            goal_by_run[item.id] = item.goal_id
        artifacts_by_goal: dict[str, list[Artifact]] = {}
        for artifact in artifacts:
            goal_id = goal_by_run.get(artifact.run_id)
            if goal_id:
                artifacts_by_goal.setdefault(goal_id, []).append(artifact)

        resolved: list[ConversationGoal] = []
        for record in records:
            context = TargetContext.model_validate(record.target_context or {})
            prompts = [
                item.prompt.strip()
                for item in runs_by_goal.get(record.id, [])
                if item.prompt and item.prompt.strip()
            ]
            titles: list[str] = []
            topics: list[str] = []
            explicit_refs = {record.id, f"goal:{record.id}"}
            for target in (
                context.content_target,
                context.schedule_target,
                context.publication_target,
                context.interaction_target,
            ):
                if target is None:
                    continue
                explicit_refs.add(target.target_id)
                explicit_refs.add(f"{target.target_type.lower()}:{target.target_id}")
                if target.artifact_id:
                    explicit_refs.add(target.artifact_id)
                    explicit_refs.add(f"artifact:{target.artifact_id}")
            for artifact in artifacts_by_goal.get(record.id, []):
                explicit_refs.add(artifact.id)
                explicit_refs.add(f"artifact:{artifact.id}")
                content = dict(artifact.content or {})
                title = str(content.get("title") or "").strip()
                if title:
                    titles.append(title[:500])
                topic = str(content.get("topic") or "").strip()
                if topic:
                    topics.append(topic[:200])
                topics.extend(
                    str(value).strip()[:200]
                    for value in list(content.get("tags") or [])
                    if str(value).strip()
                )
            aliases = list(
                dict.fromkeys(
                    [
                        *(str(value).strip() for value in list(record.aliases or [])),
                        *prompts,
                    ]
                )
            )[:12]
            summary = (
                record.summary
                or next(iter(prompts), None)
                or next(iter(titles), None)
                or record.intent
            )
            resolved.append(
                ConversationGoal(
                    goal_id=record.id,
                    conversation_id=record.conversation_id,
                    intent=record.intent,
                    summary=str(summary)[:500],
                    aliases=aliases,
                    artifact_titles=list(dict.fromkeys(titles))[:20],
                    artifact_topics=list(dict.fromkeys(topics))[:30],
                    explicit_refs=list(explicit_refs)[:40],
                    status=record.status,
                    phase=record.phase,
                    active_target_ref=record.active_target_ref,
                    target_context=context,
                    pending_clarification=(
                        PendingClarification.model_validate(record.pending_clarification)
                        if record.pending_clarification
                        else None
                    ),
                    pending_delta_id=record.pending_delta_id,
                    version=record.version,
                    updated_at=record.updated_at,
                )
            )
        return goals_for_resolution(resolved)

    async def _answer_ambiguous_goal_query(
        self,
        *,
        run: Run,
        turn_intent: TurnIntent,
        resolution: GoalResolution,
        goals: list[ConversationGoal],
    ) -> None:
        """Answer a status query across all close Goal candidates in one shot."""

        by_id = {item.goal_id: item for item in goals}
        lines: list[str] = []
        for index, match in enumerate(resolution.candidates[:6], start=1):
            goal = by_id.get(match.goal_id)
            if goal is None:
                lines.append(f"{index}. {match.label}")
                continue
            # Refresh stale SCHEDULED goals that already published on Java.
            refreshed = await self._refresh_goal_publication_status(run=run, goal=goal)
            goal = refreshed or goal
            lines.append(
                f"{index}. {GoalResolver.label_for_goal(goal)}"
                f"：{self._goal_status_sentence(goal)}"
            )
        response = (
            "同名任务有多条，当前状态如下：\n"
            + "\n".join(lines)
            + "\n如需改其中某一条，请带上草稿号或说“第几条”。"
        )
        await self._complete_run(run.id, response, [])

    @staticmethod
    def _goal_status_sentence(goal: ConversationGoal) -> str:
        phase = str(goal.phase or "").upper()
        content = goal.target_context.content_target if goal.target_context else None
        schedule = goal.target_context.schedule_target if goal.target_context else None
        publication = (
            goal.target_context.publication_target if goal.target_context else None
        )
        if phase == "PUBLISHED" or publication is not None:
            post_id = (
                publication.target_id
                if publication is not None
                else (content.target_id if content else None)
            )
            return f"已经发布" + (f"（帖子号 {post_id}）" if post_id else "")
        if phase == "SCHEDULED" and schedule is not None:
            draft_id = content.target_id if content else None
            return (
                f"已排定定时发布（定时号 {schedule.target_id}"
                + (f"，草稿号 {draft_id}" if draft_id else "")
                + "）"
            )
        if content is not None:
            return f"仍是草稿（草稿号 {content.target_id}），尚未发布"
        return f"当前阶段 {phase or goal.status}"

    async def _refresh_goal_publication_status(
        self,
        *,
        run: Run,
        goal: ConversationGoal,
    ) -> ConversationGoal | None:
        """If Java already published the bound draft, mark the Goal PUBLISHED."""

        content = goal.target_context.content_target if goal.target_context else None
        if content is None or not content.target_id:
            return None
        if goal.phase == "PUBLISHED":
            return goal
        try:
            capability = await self._issue_capability(
                run,
                action="community.list_own_posts",
                resources=[],
                max_uses=1,
            )
            posts = await self.community.list_own_posts(
                limit=100,
                offset=0,
                capability_token=capability.token,
                trace_id=run.trace_id,
            )
        except Exception:
            return None
        match = next(
            (
                item
                for item in posts
                if str(item.get("id") or item.get("postId") or "") == content.target_id
            ),
            None,
        )
        if match is None:
            return None
        status = str(match.get("status") or "").lower()
        if status not in {"published", "public", "online"}:
            return None
        async with self.database.sessions() as session, session.begin():
            record = await session.get(
                ConversationGoalRecord,
                goal.goal_id,
                with_for_update=True,
            )
            if record is None:
                return None
            context = TargetContext.model_validate(record.target_context or {})
            publication = TargetBinding(
                target_type="POST",
                role="PUBLICATION",
                target_id=content.target_id,
                artifact_id=content.artifact_id,
                content_sha256=content.content_sha256,
                version=(content.version or 1) + 1,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
                content_artifact_id=content.content_artifact_id or content.artifact_id,
                content_artifact_version=content.content_artifact_version,
            )
            next_context = context.model_copy(
                update={
                    "publication_target": publication,
                    "schedule_target": None,
                }
            )
            await self._cas_goal_target_context(
                session=session,
                goal=record,
                target_context=next_context,
                active_target_ref=f"post:{content.target_id}",
                phase="PUBLISHED",
            )
            return goal.model_copy(
                update={
                    "phase": "PUBLISHED",
                    "status": "COMPLETED",
                    "target_context": next_context,
                    "active_target_ref": f"post:{content.target_id}",
                }
            )

    async def _wait_for_goal_resolution(
        self,
        *,
        run: Run,
        resolution: GoalResolution,
    ) -> None:
        """Pause this Run for Goal clarification without mutating any Goal."""

        if resolution.outcome == "NOT_FOUND":
            response = "没有找到与这条消息匹配的会话任务，请说明帖子标题或目标。"
        else:
            options = "\n".join(
                f"{chr(ord('A') + index)}. {item.label}"
                for index, item in enumerate(resolution.candidates)
            )
            response = f"你指的是哪一个任务？\n{options}"
        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run.id, with_for_update=True)
            if current is None or current.lease_owner != self.worker_id:
                return
            current.status = "WAITING_CLARIFICATION"
            current.summary = "等待用户选择会话目标"
            current.final_response = response
            clarification_payload = {
                **resolution.model_dump(mode="json"),
                "kind": "GOAL",
                "question": response,
                "original_message": run.prompt,
            }
            current.checkpoint = {
                **dict(current.checkpoint or {}),
                "goal_resolution": resolution.model_dump(mode="json"),
                "pending_clarification": clarification_payload,
            }
            current.lease_owner = None
            current.lease_expires_at = None
            current.updated_at = utc_now()
            session.add(
                Message(
                    conversation_id=current.conversation_id,
                    role="assistant",
                    content=response,
                    parts=[
                        {
                            "kind": "goal_clarification",
                            "resolution": resolution.model_dump(mode="json"),
                        }
                    ],
                    run_id=current.id,
                )
            )
            await append_event(
                session,
                current.id,
                "GOAL_RESOLUTION_REQUIRED",
                resolution.model_dump(mode="json"),
            )

    async def _load_pending_goal_resolution(
        self,
        run: Run,
    ) -> GoalResolution | None:
        """Find the most recent unresolved goal clarification in this conversation."""
        async with self.database.sessions() as session:
            recent = await session.scalar(
                select(Run)
                .where(
                    Run.conversation_id == run.conversation_id,
                    Run.user_id == run.user_id,
                    Run.status == "WAITING_CLARIFICATION",
                    Run.id != run.id,
                )
                .order_by(Run.created_at.desc())
                .limit(1)
            )
        if recent is None:
            return None
        checkpoint = dict(recent.checkpoint or {})
        payload = checkpoint.get("pending_clarification") or checkpoint.get(
            "goal_resolution"
        )
        if not isinstance(payload, dict):
            return None
        try:
            resolution_payload = dict(payload)
            for display_key in ("kind", "question", "original_message"):
                resolution_payload.pop(display_key, None)
            return GoalResolution.model_validate(resolution_payload)
        except Exception:
            return None

    async def _clear_goal_resolution(self, run: Run) -> None:
        """Mark all pending goal clarifications as resolved."""
        async with self.database.sessions() as session, session.begin():
            pending_runs = (
                await session.scalars(
                    select(Run)
                    .where(
                        Run.conversation_id == run.conversation_id,
                        Run.user_id == run.user_id,
                        Run.status == "WAITING_CLARIFICATION",
                    )
                    .order_by(Run.created_at.desc())
                    .limit(12)
                )
            ).all()
            for pending in pending_runs:
                checkpoint = dict(pending.checkpoint or {})
                checkpoint.pop("goal_resolution", None)
                checkpoint.pop("pending_clarification", None)
                pending.checkpoint = checkpoint
                pending.status = "COMPLETED"
                pending.summary = "目标消歧已完成"
                pending.final_response = pending.final_response or "已按你的选择继续。"
                pending.completed_at = utc_now()
                pending.lease_owner = None
                pending.lease_expires_at = None

    @staticmethod
    def _target_context_from_records(
        bindings: list[TargetBindingRecord],
    ) -> TargetContext:
        """Materialize independent content/schedule/interaction focus slots."""

        content_target: TargetBinding | None = None
        schedule_target: TargetBinding | None = None
        publication_target: TargetBinding | None = None
        interaction_target: TargetBinding | None = None
        for record in reversed(bindings):
            binding = TargetBinding(
                target_type=record.target_type,
                role=record.role,
                target_id=record.target_id,
                artifact_id=record.artifact_id,
                content_sha256=record.content_sha256,
                version=record.version,
                confidence=record.confidence,
                resolution_method=record.resolution_method,
                schedule_id=record.schedule_id,
                content_artifact_id=record.content_artifact_id,
                content_artifact_version=record.content_artifact_version,
            )
            if record.target_type in {"DRAFT", "POST"}:
                if record.role == "PUBLICATION":
                    publication_target = binding
                content_target = binding
                if record.schedule_id:
                    schedule_target = TargetBinding(
                        target_type="SCHEDULE",
                        target_id=record.schedule_id,
                        artifact_id=record.artifact_id,
                        content_sha256=record.content_sha256,
                        version=record.version,
                        confidence=record.confidence,
                        resolution_method=record.resolution_method,
                        schedule_id=record.schedule_id,
                        content_artifact_id=record.content_artifact_id,
                        content_artifact_version=record.content_artifact_version,
                    )
            elif record.target_type == "SCHEDULE":
                schedule_target = binding
            elif record.target_type == "ARTIFACT":
                interaction_target = binding
        return TargetContext(
            content_target=content_target,
            schedule_target=schedule_target,
            publication_target=publication_target,
            interaction_target=interaction_target,
        )

    @staticmethod
    def _authoritative_target_context(
        goal: ConversationGoalRecord,
        bindings: list[TargetBindingRecord],
    ) -> TargetContext:
        persisted = TargetContext.model_validate(goal.target_context or {})
        if any(
            (
                persisted.content_target,
                persisted.schedule_target,
                persisted.publication_target,
                persisted.interaction_target,
            )
        ):
            return persisted
        return AgentWorker._target_context_from_records(bindings)

    @staticmethod
    def _merge_target_context(
        context: TargetContext,
        binding: TargetBinding,
    ) -> TargetContext:
        if binding.target_type in {"DRAFT", "POST"}:
            next_context = context.model_copy(update={"content_target": binding})
            if binding.role == "PUBLICATION" or binding.target_type == "POST":
                next_context = next_context.model_copy(
                    update={"publication_target": binding}
                )
            if binding.schedule_id:
                schedule = TargetBinding(
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id=binding.schedule_id,
                    artifact_id=binding.artifact_id,
                    content_sha256=binding.content_sha256,
                    version=binding.version,
                    confidence=binding.confidence,
                    resolution_method=binding.resolution_method,
                    schedule_id=binding.schedule_id,
                    content_artifact_id=binding.content_artifact_id,
                    content_artifact_version=binding.content_artifact_version,
                )
                next_context = next_context.model_copy(
                    update={"schedule_target": schedule}
                )
            return next_context
        if binding.target_type == "SCHEDULE":
            return context.model_copy(update={"schedule_target": binding})
        if binding.target_type == "ARTIFACT":
            return context.model_copy(update={"interaction_target": binding})
        return context

    async def _load_target_context(self, run: Run) -> TargetContext:
        """Load the authoritative role-scoped targets for this Goal."""
        if not run.goal_id:
            return TargetContext()
        async with self.database.sessions() as session:
            goal = await session.get(ConversationGoalRecord, run.goal_id)
            if goal is None:
                return TargetContext()
            bindings = list(
                (
                    await session.scalars(
                        select(TargetBindingRecord)
                        .where(TargetBindingRecord.goal_id == goal.id)
                        .order_by(TargetBindingRecord.version.desc(), TargetBindingRecord.created_at.desc())
                        .limit(30)
                    )
                ).all()
            )
            return self._authoritative_target_context(goal, bindings)

    async def _load_target_history(self, run: Run) -> list[TargetBinding]:
        if not run.goal_id:
            return []
        async with self.database.sessions() as session:
            bindings = list(
                (
                    await session.scalars(
                        select(TargetBindingRecord)
                        .where(TargetBindingRecord.goal_id == run.goal_id)
                        .order_by(TargetBindingRecord.version.desc())
                        .limit(12)
                    )
                ).all()
            )
        return [
            TargetBinding(
                target_type=item.target_type,
                role=item.role,
                target_id=item.target_id,
                artifact_id=item.artifact_id,
                content_sha256=item.content_sha256,
                version=item.version,
                confidence=item.confidence,
                resolution_method=item.resolution_method,
                schedule_id=item.schedule_id,
                content_artifact_id=item.content_artifact_id,
                content_artifact_version=item.content_artifact_version,
            )
            for item in bindings
        ]

    async def _load_intent_delta_by_id(self, delta_id: str | None) -> IntentDelta | None:
        if not delta_id:
            return None
        async with self.database.sessions() as session:
            record = await session.get(IntentDeltaRecord, delta_id)
            if record is None:
                return None
            return IntentDelta(
                delta_id=record.id,
                goal_id=record.goal_id,
                run_id=record.run_id,
                message_id=record.message_id,
                operation=record.operation,
                operation_class=record.operation_class,
                target_role=record.target_role,
                target_ref=record.target_ref,
                delta=dict(record.delta or {}),
                preserve=dict(record.preserve or {}),
                confidence=record.confidence,
                status=record.status,
            )

    async def _bind_selected_target(
        self,
        *,
        run: Run,
        candidate: TargetCandidate,
    ) -> TargetBinding:
        if not run.goal_id:
            raise RuntimeError("无法绑定目标：Run 未关联 ConversationGoal")
        async with self.database.sessions() as session, session.begin():
            goal = await session.get(
                ConversationGoalRecord,
                run.goal_id,
                with_for_update=True,
            )
            if goal is None:
                raise RuntimeError("无法绑定目标：ConversationGoal 不存在")
            previous = await session.scalar(
                select(TargetBindingRecord)
                .where(TargetBindingRecord.goal_id == goal.id)
                .order_by(TargetBindingRecord.version.desc())
                .limit(1)
            )
            same_target = previous is not None and previous.target_id == candidate.target_id
            binding = TargetBindingRecord(
                goal_id=goal.id,
                target_type=candidate.type,
                role=(
                    "SCHEDULE" if candidate.type == "SCHEDULE"
                    else "INTERACTION" if candidate.type == "ARTIFACT"
                    else "CONTENT"
                ),
                target_id=candidate.target_id,
                artifact_id=candidate.artifact_id
                or (previous.artifact_id if same_target and previous else None),
                content_sha256=previous.content_sha256
                if same_target and previous
                else None,
                version=await self._allocate_target_binding_version(
                    session, goal_id=goal.id
                ),
                confidence=candidate.score,
                resolution_method="USER_SELECTION",
                schedule_id=(
                    candidate.target_id
                    if candidate.type == "SCHEDULE"
                    else previous.schedule_id if same_target and previous else None
                ),
                content_artifact_id=(
                    candidate.content_artifact_id
                    or (previous.content_artifact_id if same_target and previous else None)
                ),
                content_artifact_version=(
                    candidate.content_artifact_version
                    or (previous.content_artifact_version if same_target and previous else None)
                ),
            )
            session.add(binding)
            previous_context = TargetContext.model_validate(goal.target_context or {})
            selected_binding = TargetBinding(
                target_type=binding.target_type,
                role=binding.role,
                target_id=binding.target_id,
                artifact_id=binding.artifact_id,
                content_sha256=binding.content_sha256,
                version=binding.version,
                confidence=binding.confidence,
                resolution_method=binding.resolution_method,
                schedule_id=binding.schedule_id,
                content_artifact_id=binding.content_artifact_id,
                content_artifact_version=binding.content_artifact_version,
            )
            next_context = self._merge_target_context(
                previous_context,
                selected_binding,
            )
            await self._cas_goal_target_context(
                session=session,
                goal=goal,
                target_context=next_context,
                active_target_ref=f"{candidate.type.lower()}:{candidate.target_id}",
                phase=goal.phase,
            )
            return TargetBinding(
                target_type=binding.target_type,
                role=binding.role,
                target_id=binding.target_id,
                artifact_id=binding.artifact_id,
                content_sha256=binding.content_sha256,
                version=binding.version,
                confidence=binding.confidence,
                resolution_method=binding.resolution_method,
                schedule_id=binding.schedule_id,
                content_artifact_id=binding.content_artifact_id,
                content_artifact_version=binding.content_artifact_version,
            )

    async def _clear_pending_clarification(self, run_id: str, goal_id: str) -> None:
        async with self.database.sessions() as session, session.begin():
            goal = await session.get(ConversationGoalRecord, goal_id, with_for_update=True)
            run = await session.get(Run, run_id, with_for_update=True)
            if goal is None or run is None:
                return
            pending_delta_id = goal.pending_delta_id
            goal.pending_clarification = None
            goal.pending_delta_id = None
            goal.status = "ACTIVE"
            goal.version += 1
            goal.updated_at = utc_now()
            checkpoint = dict(run.checkpoint or {})
            if checkpoint.get("intent_delta_id") is None:
                checkpoint["intent_delta_id"] = pending_delta_id
            checkpoint.pop("pending_clarification", None)
            checkpoint.pop("pending_temporal_clarification", None)
            run.checkpoint = checkpoint

    async def _supersede_pending_clarification(
        self,
        *,
        run_id: str,
        goal_id: str,
    ) -> None:
        """Discard an unanswered clarification when a fresh request arrives."""
        async with self.database.sessions() as session, session.begin():
            goal = await session.get(
                ConversationGoalRecord,
                goal_id,
                with_for_update=True,
            )
            run = await session.get(Run, run_id, with_for_update=True)
            if goal is None or run is None:
                return
            pending_delta_id = goal.pending_delta_id
            if pending_delta_id:
                pending_delta = await session.get(
                    IntentDeltaRecord,
                    pending_delta_id,
                    with_for_update=True,
                )
                if pending_delta is not None and pending_delta.status == "ACTIVE":
                    pending_delta.status = "SUPERSEDED"
                    pending_delta.updated_at = utc_now()
            goal.pending_clarification = None
            goal.pending_delta_id = None
            goal.status = "ACTIVE"
            goal.version += 1
            goal.updated_at = utc_now()
            checkpoint = dict(run.checkpoint or {})
            checkpoint.pop("pending_clarification", None)
            if checkpoint.get("intent_delta_id") == pending_delta_id:
                checkpoint.pop("intent_delta_id", None)
            run.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "TARGET_CLARIFICATION_SUPERSEDED",
                {
                    "delta_id": pending_delta_id,
                    "reason": "new_user_request",
                },
            )

    async def _temporal_anchor_time(self, run: Run) -> datetime:
        """Prefer message.created_at, then run.created_at — never tool-exec now."""

        checkpoint = dict(run.checkpoint or {})
        message_id = str(checkpoint.get("message_id") or "").strip()
        async with self.database.sessions() as session:
            message = None
            if message_id:
                message = await session.get(Message, message_id)
            if message is None:
                message = await session.scalar(
                    select(Message)
                    .where(Message.run_id == run.id, Message.role == "user")
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            if message is not None and message.created_at is not None:
                created = message.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                return created
            if run.created_at is not None:
                created = run.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                return created
        return utc_now()

    async def _load_existing_schedule_run_at(
        self, goal: ConversationGoal
    ) -> datetime | None:
        schedule = goal.target_context.schedule_target if goal.target_context else None
        action_id = schedule.target_id if schedule is not None else None
        if not action_id:
            return None
        async with self.database.sessions() as session:
            action = await session.get(ScheduledAction, action_id)
            if action is None or action.run_at is None:
                return None
            run_at = action.run_at
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            return run_at

    @staticmethod
    def _is_pure_schedule_time_update(turn_plan: TurnPlan) -> bool:
        schedule_updates = [
            change
            for change in turn_plan.changes
            if change.role == "SCHEDULE" and change.op == "UPDATE"
        ]
        content_changes = [change for change in turn_plan.changes if change.role == "CONTENT"]
        return bool(schedule_updates) and not content_changes

    def _apply_schedule_temporal(
        self,
        *,
        turn_plan: TurnPlan,
        message: str,
        current_time: datetime,
        existing_run_at: datetime | None,
        timezone: str,
    ) -> tuple[TurnPlan, TemporalResolution | None]:
        """Solidify absolute run_at into SCHEDULE UPDATE payloads before compile."""

        if not self._is_pure_schedule_time_update(turn_plan) and not any(
            change.role == "SCHEDULE" and change.op == "UPDATE"
            for change in turn_plan.changes
        ):
            return turn_plan, None
        # Prefer checkpoint-stamped time on retry/resume (no re-parse drift).
        for change in turn_plan.changes:
            if (
                change.role == "SCHEDULE"
                and change.op == "UPDATE"
                and change.payload.get("run_at")
            ):
                return turn_plan, None
        request = message
        for change in turn_plan.changes:
            if change.role == "SCHEDULE" and change.op == "UPDATE":
                request = str(
                    change.payload.get("schedule_request")
                    or change.payload.get("message")
                    or message
                )
                break
        resolution = resolve_schedule_time(
            message=request,
            current_time=current_time,
            timezone=timezone,
            existing_run_at=existing_run_at,
        )
        if resolution.run_at is None:
            return turn_plan, resolution
        stamped = normalize_run_at_for_tool(resolution.run_at)
        changes = []
        for change in turn_plan.changes:
            if change.role == "SCHEDULE" and change.op == "UPDATE":
                payload = dict(change.payload or {})
                payload["run_at"] = stamped
                payload["mutation"] = "UPDATE_SCHEDULE_TIME"
                payload["temporal_mode"] = resolution.mode
                payload["temporal_base_type"] = resolution.base_type
                if resolution.base_time is not None:
                    payload["temporal_base_time"] = resolution.base_time.isoformat()
                if resolution.offset_seconds is not None:
                    payload["offset_seconds"] = resolution.offset_seconds
                changes.append(change.model_copy(update={"payload": payload}))
            else:
                changes.append(change)
        return turn_plan.model_copy(update={"changes": changes}), resolution

    async def _wait_for_temporal_clarification(
        self,
        *,
        run_id: str,
        goal: ConversationGoal,
        resolution: TemporalResolution,
        original_message: str,
        intent_delta: IntentDelta | None = None,
    ) -> None:
        if resolution.mode == "PAST_TIME":
            response = (
                "该发布时间已经过去，请重新指定一个未来的时间，"
                "例如「明天上午八点」或「五分钟之后」。"
            )
            summary = "发布时间已过期，等待用户重新指定"
        else:
            response = (
                "请提供具体的新发布时间，例如「延迟十分钟」「五分钟之后」"
                "或「明天上午八点」。我还没有修改原来的定时任务。"
            )
            summary = "等待用户补充具体发布时间"
        persisted_delta = intent_delta
        if intent_delta is not None:
            async with self.database.sessions() as session:
                run_for_delta = await session.get(Run, run_id)
            if run_for_delta is None:
                raise RuntimeError("Run missing while waiting for temporal clarification")
            persisted_delta = await self._persist_intent_delta_model(
                run=run_for_delta,
                intent_delta=intent_delta,
            )
        clarification = PendingClarification(
            kind="TEMPORAL_SCHEDULE",
            question=response,
            candidates=[],
            delta_id=persisted_delta.delta_id if persisted_delta is not None else None,
            goal_id=goal.goal_id,
            original_message=original_message,
            temporal=resolution.model_dump(mode="json"),
        )
        pending_payload = clarification.model_dump(mode="json")
        async with self.database.sessions() as session, session.begin():
            run_record = await session.get(Run, run_id, with_for_update=True)
            goal_record = await session.get(
                ConversationGoalRecord,
                goal.goal_id,
                with_for_update=True,
            )
            if run_record is None or goal_record is None:
                return
            # Bind run to the already-resolved goal so resume does not fall
            # back to the newest active task.
            run_record.goal_id = goal.goal_id
            run_record.status = "WAITING_CLARIFICATION"
            run_record.summary = summary
            run_record.final_response = response
            run_record.checkpoint = {
                **dict(run_record.checkpoint or {}),
                "pending_temporal_clarification": pending_payload,
                "pending_clarification": pending_payload,
                "intent_delta_id": (
                    persisted_delta.delta_id if persisted_delta is not None else None
                ),
            }
            run_record.lease_owner = None
            run_record.lease_expires_at = None
            run_record.retry_after = None
            run_record.updated_at = utc_now()
            goal_record.status = "WAITING_CLARIFICATION"
            goal_record.pending_clarification = pending_payload
            goal_record.pending_delta_id = (
                persisted_delta.delta_id if persisted_delta is not None else None
            )
            goal_record.version += 1
            goal_record.updated_at = utc_now()
            session.add(
                Message(
                    conversation_id=run_record.conversation_id,
                    role="assistant",
                    content=response,
                    run_id=run_id,
                )
            )
            await append_event(
                session,
                run_id,
                "TEMPORAL_CLARIFICATION_REQUIRED",
                {
                    "mode": resolution.mode,
                    "error_code": resolution.error_code,
                    "goal_id": goal.goal_id,
                },
            )

    async def _wait_for_clarification(
        self,
        *,
        run_id: str,
        goal: ConversationGoal,
        clarification: PendingClarification,
    ) -> None:
        if not clarification.candidates:
            raise RuntimeError("澄清请求缺少目标候选")
        options = "\n".join(
            f"{chr(ord('A') + index)}. {candidate.label or candidate.reason}"
            f"（{candidate.type}:{candidate.target_id}）"
            for index, candidate in enumerate(clarification.candidates)
        )
        response = f"{clarification.question}\n{options}"
        async with self.database.sessions() as session, session.begin():
            run_record = await session.get(Run, run_id, with_for_update=True)
            goal_record = await session.get(
                ConversationGoalRecord,
                goal.goal_id,
                with_for_update=True,
            )
            if run_record is None or goal_record is None:
                return
            run_record.status = "WAITING_CLARIFICATION"
            run_record.summary = "等待用户选择操作对象"
            run_record.final_response = response
            run_record.checkpoint = {
                **dict(run_record.checkpoint or {}),
                "intent_delta_id": clarification.delta_id,
                "pending_clarification": clarification.model_dump(mode="json"),
            }
            run_record.lease_owner = None
            run_record.lease_expires_at = None
            run_record.retry_after = None
            run_record.updated_at = utc_now()
            goal_record.status = "WAITING_CLARIFICATION"
            goal_record.pending_clarification = clarification.model_dump(mode="json")
            goal_record.pending_delta_id = clarification.delta_id
            goal_record.version += 1
            goal_record.updated_at = utc_now()
            session.add(
                Message(
                    conversation_id=run_record.conversation_id,
                    role="assistant",
                    content=response,
                    parts=[
                        {
                            "kind": "target_clarification",
                            "candidates": [
                                item.model_dump(mode="json")
                                for item in clarification.candidates
                            ],
                        }
                    ],
                    run_id=run_record.id,
                )
            )
            await append_event(
                session,
                run_record.id,
                "TARGET_CLARIFICATION_REQUIRED",
                {
                    "status": "WAITING_CLARIFICATION",
                    "delta_id": clarification.delta_id,
                    "candidate_count": len(clarification.candidates),
                },
            )

    async def _sync_goal_intent(self, run: Run, intent: CommunityIntent) -> None:
        if not run.goal_id:
            return
        async with self.database.sessions() as session, session.begin():
            goal = await session.get(
                ConversationGoalRecord,
                run.goal_id,
                with_for_update=True,
            )
            if goal is None:
                return
            goal.intent = intent.domain.upper()
            if goal.phase == "DISCOVERING":
                goal.phase = "DRAFTING" if "content" in intent.domain else "READY"
            goal.status = "ACTIVE"
            goal.updated_at = utc_now()

    async def _load_intent_delta(self, run: Run) -> IntentDelta | None:
        """Restore the idempotent delta for a retry/resume of this Run."""
        async with self.database.sessions() as session:
            checkpoint_delta_id = str(dict(run.checkpoint or {}).get("intent_delta_id") or "")
            record = (
                await session.get(IntentDeltaRecord, checkpoint_delta_id)
                if checkpoint_delta_id
                else await session.scalar(
                    select(IntentDeltaRecord)
                    .where(IntentDeltaRecord.run_id == run.id)
                    .limit(1)
                )
            )
            if record is None:
                return None
            return IntentDelta(
                delta_id=record.id,
                goal_id=record.goal_id,
                run_id=record.run_id,
                message_id=record.message_id,
                operation=record.operation,
                operation_class=record.operation_class,
                target_role=record.target_role,
                target_ref=record.target_ref,
                delta=dict(record.delta or {}),
                preserve=dict(record.preserve or {}),
                confidence=record.confidence,
                status=record.status,
            )

    async def _persist_intent_delta(
        self,
        *,
        run: Run,
        goal: ConversationGoal,
        turn_intent: TurnIntent,
        decision: AdaptiveExecutionDecision,
        intent: CommunityIntent,
    ) -> IntentDelta:
        """Parse and persist one delta, idempotently, for the current Run."""
        async with self.database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(IntentDeltaRecord)
                .where(IntentDeltaRecord.run_id == run.id)
                .with_for_update()
            )
            if existing is not None:
                return IntentDelta(
                    delta_id=existing.id,
                    goal_id=existing.goal_id,
                    run_id=existing.run_id,
                    message_id=existing.message_id,
                    operation=existing.operation,
                    operation_class=existing.operation_class,
                    target_role=existing.target_role,
                    target_ref=existing.target_ref,
                    delta=dict(existing.delta or {}),
                    preserve=dict(existing.preserve or {}),
                    confidence=existing.confidence,
                    status=existing.status,
                )
            message = await session.scalar(
                select(Message)
                .where(Message.run_id == run.id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            if message is None:
                raise RuntimeError("Cannot create IntentDelta without the user message")
            parsed = self.intent_delta_binder.bind(
                turn_intent=turn_intent,
                message=run.prompt,
                goal=goal,
                target_context=goal.target_context,
                run_id=run.id,
                message_id=message.id,
                turn_relation=decision.turn_relation,
                intent_domain=intent.domain,
                intent_goal=intent.goal,
            )
            session.add(
                IntentDeltaRecord(
                    id=parsed.delta_id,
                    goal_id=parsed.goal_id,
                    run_id=parsed.run_id,
                    message_id=parsed.message_id,
                    operation=parsed.operation,
                    operation_class=parsed.operation_class,
                    target_role=parsed.target_role,
                    target_ref=parsed.target_ref,
                    delta=parsed.delta,
                    preserve=parsed.preserve,
                    confidence=parsed.confidence,
                    status=parsed.status,
                )
            )
            return parsed

    async def _persist_intent_delta_model(
        self,
        *,
        run: Run,
        intent_delta: IntentDelta | None,
    ) -> IntentDelta:
        """Persist a pre-built IntentDelta (from TurnPlan) idempotently."""

        if intent_delta is None:
            raise RuntimeError("Cannot persist an empty IntentDelta")
        async with self.database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(IntentDeltaRecord)
                .where(IntentDeltaRecord.run_id == run.id)
                .with_for_update()
            )
            if existing is not None:
                return IntentDelta(
                    delta_id=existing.id,
                    goal_id=existing.goal_id,
                    run_id=existing.run_id,
                    message_id=existing.message_id,
                    operation=existing.operation,  # type: ignore[arg-type]
                    operation_class=existing.operation_class,  # type: ignore[arg-type]
                    target_role=existing.target_role,  # type: ignore[arg-type]
                    target_ref=existing.target_ref,
                    delta=dict(existing.delta or {}),
                    preserve=dict(existing.preserve or {}),
                    confidence=existing.confidence,
                    status=existing.status,  # type: ignore[arg-type]
                )
            message = await session.scalar(
                select(Message)
                .where(Message.run_id == run.id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            if message is None:
                raise RuntimeError("Cannot create IntentDelta without the user message")
            record = IntentDeltaRecord(
                id=intent_delta.delta_id,
                goal_id=intent_delta.goal_id,
                run_id=run.id,
                message_id=message.id,
                operation=intent_delta.operation,
                operation_class=intent_delta.operation_class,
                target_role=intent_delta.target_role,
                target_ref=intent_delta.target_ref,
                delta=intent_delta.delta,
                preserve=intent_delta.preserve,
                confidence=intent_delta.confidence,
                status=intent_delta.status,
            )
            session.add(record)
            return intent_delta.model_copy(
                update={"run_id": run.id, "message_id": message.id}
            )

    async def _load_conversation_workspace(
        self,
        run: Run,
    ) -> ConversationWorkspace:
        """Freeze a bounded control-plane view for this run.

        The view is rebuilt from scoped Run and immutable Artifact rows on each
        new turn. A retry reuses the frozen snapshot, so model decisions cannot
        silently change because another worker observed newer state mid-run.
        """
        checkpoint = dict(run.checkpoint or {})
        saved = checkpoint.get("conversation_workspace")
        if checkpoint.get("conversation_workspace_frozen") is True and isinstance(
            saved, dict
        ):
            return ConversationWorkspace.model_validate(saved)

        async with self.database.sessions() as session:
            previous_runs = list(
                (
                    await session.scalars(
                        select(Run)
                        .where(
                            Run.conversation_id == run.conversation_id,
                            Run.user_id == run.user_id,
                            Run.tenant_id == run.tenant_id,
                            Run.id != run.id,
                            Run.created_at < run.created_at,
                        )
                        .order_by(Run.created_at.desc())
                        .limit(12)
                    )
                ).all()
            )
            previous_ids = [item.id for item in previous_runs]
            artifacts = (
                list(
                    (
                        await session.scalars(
                            select(Artifact)
                            .where(Artifact.run_id.in_(previous_ids))
                            .order_by(Artifact.created_at.desc())
                            .limit(80)
                        )
                    ).all()
                )
                if previous_ids
                else []
            )
            goal_records = list(
                (
                    await session.scalars(
                        select(ConversationGoalRecord)
                        .where(
                            ConversationGoalRecord.conversation_id
                            == run.conversation_id,
                            ConversationGoalRecord.user_id == run.user_id,
                            ConversationGoalRecord.tenant_id == run.tenant_id,
                        )
                        .order_by(ConversationGoalRecord.updated_at.desc())
                        .limit(12)
                    )
                ).all()
            )

        workspace = materialize_goal_workspace(
            conversation_id=run.conversation_id,
            goals=[
                {
                    "id": item.id,
                    "summary": item.summary,
                    "intent": item.intent,
                    "status": item.status,
                    "updated_at": item.updated_at,
                    "created_at": item.updated_at,
                }
                for item in goal_records
            ],
            runs=[
                {
                    "id": item.id,
                    "goal_id": item.goal_id,
                    "prompt": item.prompt,
                    "status": item.status,
                    "intent": item.intent,
                    "error": item.error,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in previous_runs
            ],
            artifacts=[
                {
                    "id": item.id,
                    "run_id": item.run_id,
                    "artifact_type": item.artifact_type,
                    "content": dict(item.content or {}),
                    "created_at": item.created_at,
                }
                for item in artifacts
            ],
        )
        serialized = workspace.model_dump(mode="json")
        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run.id, with_for_update=True)
            if current is not None and current.lease_owner == self.worker_id:
                current_checkpoint = dict(current.checkpoint or {})
                current_checkpoint["conversation_workspace_frozen"] = True
                current_checkpoint["conversation_workspace"] = serialized
                current.checkpoint = current_checkpoint
                await append_event(
                    session,
                    run.id,
                    "CONVERSATION_WORKSPACE_MATERIALIZED",
                    {
                        "revision": workspace.revision,
                        "active_goal_ref": workspace.active_goal_ref,
                        "focus_refs": workspace.focus_refs,
                        "open_loops": workspace.open_loops,
                        "entity_count": len(workspace.entities),
                    },
                )
        run.checkpoint = {
            **checkpoint,
            "conversation_workspace_frozen": True,
            "conversation_workspace": serialized,
        }
        return workspace

    async def _load_continuation_draft(self, run: Run) -> dict[str, Any] | None:
        """Compatibility adapter for older tests and in-flight checkpoints."""

        workspace = await self._load_conversation_workspace(run)
        return self.llm._workspace_draft(workspace.model_context())

    async def _record_deterministic_route(
        self,
        run_id: str,
        summary: str,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能记录确定性路由")
            await append_event(
                session,
                run_id,
                "DETERMINISTIC_ROUTE_SELECTED",
                {"summary": summary},
            )

    async def _record_control_plane_route(
        self,
        run_id: str,
        route: RouteDecision,
    ) -> None:
        """Persist QUERY/ACTION/CHAT classification without mutating Goal state."""

        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能记录控制面路由")
            checkpoint = dict(run.checkpoint or {})
            checkpoint["control_plane_route"] = route.as_dict()
            run.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "CONTROL_PLANE_ROUTE_SELECTED",
                route.as_dict(),
            )

    async def _record_task_manager_decision(
        self,
        run_id: str,
        task_turn,
    ) -> None:
        """Audit TaskManager CREATE/UPDATE/CANCEL without schema changes."""

        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能记录 TaskManager 决策")
            checkpoint = dict(run.checkpoint or {})
            payload = {
                "action": task_turn.action,
                "summary": task_turn.summary,
                "operation_override": task_turn.operation_override,
                "turn_relation_override": task_turn.turn_relation_override,
                "task": task_turn.task.as_dict() if task_turn.task else None,
                "goal_resolution": (
                    task_turn.goal_resolution.model_dump(mode="json")
                    if task_turn.goal_resolution is not None
                    else None
                ),
            }
            checkpoint["task_manager"] = payload
            run.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "TASK_MANAGER_DECISION",
                payload,
            )

    async def _record_entity_target_resolution(
        self,
        run_id: str,
        resolution: EntityTargetResolution,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能记录实体目标解析")
            checkpoint = dict(run.checkpoint or {})
            payload = resolution.model_dump(mode="json")
            checkpoint["entity_target_resolution"] = payload
            run.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "ENTITY_TARGET_RESOLVED",
                {
                    "resolution_method": resolution.resolution_method,
                    "task_id": resolution.task_id,
                    "goal_id": resolution.goal_id,
                    "confidence": resolution.confidence,
                    "reference_kinds": list(resolution.reference_kinds),
                },
            )

    @staticmethod
    def _goal_resolution_from_entity(
        resolution: EntityTargetResolution,
    ) -> GoalResolution:
        if (
            resolution.resolution_method != "AMBIGUOUS"
            and resolution.task_id
        ):
            return GoalResolution(
                outcome="RESOLVED",
                goal_id=resolution.task_id,
                candidates=[
                    GoalMatch(
                        goal_id=resolution.task_id,
                        label=next(
                            (
                                item.label or item.task_id
                                for item in resolution.candidates
                                if item.task_id == resolution.task_id
                            ),
                            resolution.task_id,
                        ),
                        score=resolution.confidence,
                        resolution_method="RECENT_ACTIVE",
                    )
                ],
                confidence=resolution.confidence,
            )
        return GoalResolution(
            outcome="NEEDS_CLARIFICATION",
            candidates=[
                GoalMatch(
                    goal_id=item.task_id,
                    label=item.label or item.task_id,
                    score=item.score or 0.5,
                    resolution_method="RECENT_ACTIVE",
                )
                for item in resolution.candidates[:8]
            ],
            confidence=resolution.confidence,
        )

    async def _enter_query_path(
        self,
        *,
        run_id: str,
        run: Run,
        history: list[dict[str, str]],
        memories: list[dict[str, str]],
        recalled_memories: list[dict[str, Any]],
        route: RouteDecision,
    ) -> None:
        """QUERY path: QueryAgent → read tools → answer. No Goal/Task/IntentDelta."""

        del history, memories, recalled_memories
        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run_id, with_for_update=True)
            if current is None or current.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能进入查询通道")
            current.execution_path = "TOOL"
            current.workload_lane = "READ"
            current.summary = route.summary or "查询通道"
            checkpoint = dict(current.checkpoint or {})
            checkpoint["control_plane_route"] = route.as_dict()
            checkpoint["query_path_hook"] = "query_agent"
            current.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "QUERY_PATH_ENTERED",
                {
                    "domain": route.domain,
                    "confidence": route.confidence,
                    "hook": "query_agent",
                },
            )

        schedules = await self._load_schedules_for_query(run)
        await self._consume_budget(run_id, "tool")

        async def execute_read_tool(
            tool_name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            return await self._execute_query_read_tool(
                run=run,
                tool_name=tool_name,
                arguments=arguments,
            )

        result = await self.query_agent.handle(
            message=run.prompt,
            route=route,
            execute_tool=execute_read_tool,
            schedules=schedules,
        )
        if (
            result.created_goal
            or result.touched_task
            or result.used_goal_resolver
            or result.used_target_resolver
            or result.created_intent_delta
        ):
            raise RuntimeError("QueryAgent 违反只读边界")

        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run_id, with_for_update=True)
            if current is None or current.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能保存查询结果")
            checkpoint = dict(current.checkpoint or {})
            checkpoint["query_result"] = result.as_dict()
            current.checkpoint = checkpoint
            current.summary = result.kind
            await append_event(
                session,
                run_id,
                "QUERY_AGENT_COMPLETED",
                {
                    "kind": result.kind,
                    "tool_name": result.tool_name,
                    "data_keys": sorted(result.data.keys()),
                },
            )

        outputs = (
            [
                {
                    "tool": result.tool_name,
                    "output": result.data,
                }
            ]
            if result.tool_name
            else [{"tool": "query.schedule_status", "output": result.data}]
        )
        await self._complete_run(run_id, result.answer, outputs)

    async def _load_schedules_for_query(self, run: Run) -> list[dict[str, Any]]:
        """Read-only schedule snapshot for QUERY. Does not resolve Goals/Tasks."""

        async with self.database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ScheduledAction)
                        .where(ScheduledAction.user_id == run.user_id)
                        .order_by(
                            ScheduledAction.run_at.desc(),
                            ScheduledAction.created_at.desc(),
                        )
                        .limit(20)
                    )
                ).all()
            )
        return [
            {
                "id": row.id,
                "status": row.status,
                "run_at": row.run_at.isoformat() if row.run_at else None,
                "draft_id": row.draft_id,
                "conversation_hint": None,
            }
            for row in rows
        ]

    async def _execute_query_read_tool(
        self,
        *,
        run: Run,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Allowlisted read-tool dispatch for QueryAgent only."""

        allowed = {
            "community.list_own_posts",
            "community.analyze_engagement",
            "community.search_posts",
        }
        if tool_name not in allowed:
            raise PermanentToolError(f"QueryAgent 不允许调用工具: {tool_name}")
        definition = self.registry.get(tool_name)
        if definition.risk != RiskLevel.READ:
            raise PermanentToolError(f"QueryAgent 仅允许 READ 工具: {tool_name}")
        request_id = f"query-{run.id}-{tool_name}-{uuid.uuid4().hex[:8]}"
        context = ToolInvocationContext(
            run_id=run.id,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            conversation_id=run.conversation_id,
            request_id=request_id,
            operation_key=f"query:{run.id}:{tool_name}",
            idempotency_key=f"query:{run.id}:{tool_name}",
            attempt=1,
            deadline_at=run.deadline_at,
            workload_lane="READ",
            trace_metadata={"tool": tool_name, "query_path": True},
        )
        credentials = ToolCredentials(
            access_token=self.token_vault.decrypt(run.delegated_token),
            trace_id=run.trace_id,
        )
        result = await self.execution_runtime.invoke(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
            run=run,
            ordinal=0,
            credentials=credentials,
            skip_policy=True,
            raise_on_failure=True,
        )
        if result.output is None:
            raise PermanentToolError(f"{tool_name} returned empty output")
        return result.output

    async def _enter_chat_path(
        self,
        *,
        run_id: str,
        run: Run,
        history: list[dict[str, str]],
        memories: list[dict[str, str]],
        recalled_memories: list[dict[str, Any]],
        route: RouteDecision,
    ) -> None:
        """CHAT path: direct answer without GoalResolver / Planner / Tools."""

        plan = AgentPlan(
            intent="GENERAL_ANSWER",
            summary=route.summary or "直接回答",
            steps=[],
        )
        await self._consume_budget(run_id, "model")
        response = await self._track_duration(
            run_id,
            "model_duration_ms",
            self.llm.answer(
                prompt=run.prompt,
                plan=plan,
                tool_outputs=[],
                history=history,
                memories=memories,
                recalled_memories=recalled_memories,
            ),
        )
        async with self.database.sessions() as session, session.begin():
            current = await session.get(Run, run_id, with_for_update=True)
            if current is None or current.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能进入闲聊通道")
            current.execution_path = "DIRECT"
            current.workload_lane = "READ"
            current.summary = route.summary or "直接回答"
            checkpoint = dict(current.checkpoint or {})
            checkpoint["control_plane_route"] = route.as_dict()
            checkpoint["direct_response"] = response
            current.checkpoint = checkpoint
            await append_event(
                session,
                run_id,
                "CHAT_PATH_ENTERED",
                {"domain": route.domain, "confidence": route.confidence},
            )
        await self._complete_run(run_id, response, [])

    # 用当前 run.prompt（用户刚说的话）当查询
    # 调 AssistantMemory.recall(...) 从情节记忆 +（可选）向量检索里捞相关条目
    # 结果写进 run 的 checkpoint 并冻结，避免中途重复变
    # 有召回时写事件 MEMORY_RECALLED
    # 后面规划/回答会把 recalled_memories 塞进模型上下文
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

    async def _compile_or_replan(
        self,
        *,
        run_id: str,
        run: Run,
        plan: AgentPlan,
        history: list[dict[str, str]],
        memories: list[dict[str, str]],
        recalled_memories: list[dict[str, Any]],
        continuation_draft: dict[str, Any] | None = None,
        conversation_workspace: dict[str, Any] | None = None,
        referenced_entities: list[str] | None = None,
        conversation_goal: ConversationGoal | None = None,
        intent_delta: IntentDelta | None = None,
        target_context: TargetContext | None = None,
        planning_prompt: str | None = None,
        require_goal_coverage: bool = True,
    ) -> AgentPlan:
        candidate = plan
        while True:
            candidate = self.operation_plan_guard.enforce(
                intent_delta=intent_delta,
                plan=candidate,
            )
            result = self.plan_compiler.compile(
                candidate,
                require_goal_coverage=require_goal_coverage,
            )
            if result.status == "EXECUTABLE" and result.compiled_plan is not None:
                self._assert_delta_reuses_target(candidate, intent_delta)
                async with self.database.sessions() as session, session.begin():
                    current = await session.get(Run, run_id, with_for_update=True)
                    if current is None or current.lease_owner != self.worker_id:
                        raise RuntimeError("过期 Worker 不能保存计划编译结果")
                    await append_event(
                        session,
                        run_id,
                        "PLAN_COMPILED",
                        {
                            "step_count": len(result.compiled_plan.steps),
                            "replan_count": current.replan_count,
                        },
                    )
                return result.compiled_plan

            diagnostics = [
                item.model_dump(mode="json") for item in result.diagnostics
            ]
            async with self.database.sessions() as session, session.begin():
                current = await session.get(Run, run_id, with_for_update=True)
                if current is None or current.lease_owner != self.worker_id:
                    raise RuntimeError("过期 Worker 不能保存计划诊断")
                current.summary = "计划不可执行，正在重新拆解任务"
                current.checkpoint = {
                    **dict(current.checkpoint or {}),
                    "compile_diagnostics": diagnostics,
                    "invalid_plan": candidate.model_dump(mode="json"),
                }
                await append_event(
                    session,
                    run_id,
                    "PLAN_COMPILE_REJECTED",
                    {
                        "status": result.status,
                        "diagnostics": diagnostics,
                    },
                )

            if result.status in {"NEEDS_INPUT", "UNSUPPORTED"}:
                rendered = "; ".join(item.message for item in result.diagnostics)
                raise RuntimeError(rendered or "当前任务缺少可执行的社区能力")

            await self._consume_budget(run_id, "replan")
            await self._consume_budget(run_id, "model")
            candidate = await self._track_duration(
                run_id,
                "model_duration_ms",
                self.llm.plan(
                    prompt=planning_prompt or run.prompt,
                    context_post_id=run.context_post_id,
                    context_comment_id=run.context_comment_id,
                    client_timezone=run.client_timezone,
                    history=history,
                    memories=memories,
                    recalled_memories=recalled_memories,
                    previous_execution={
                        "compile_diagnostics": diagnostics,
                        "rejected_plan": candidate.model_dump(mode="json"),
                    },
                    next_focus=(
                        "根据 Plan Compiler 诊断把复合步骤拆成原子步骤，"
                        "只使用已注册工具，并覆盖全部目标能力"
                    ),
                    structured_intent=candidate.intent_detail,
                    conversation_goal=conversation_goal,
                    intent_delta=intent_delta,
                    target_context=target_context,
                    continuation_draft=continuation_draft,
                    conversation_workspace=conversation_workspace,
                    referenced_entities=referenced_entities,
                    on_structured_retry=lambda: self._structured_output_retry(
                        run_id, "Plan Compiler Repair"
                    ),
                ),
            )
            continue

    @staticmethod
    def _assert_delta_reuses_target(
        plan: AgentPlan,
        intent_delta: IntentDelta | None,
    ) -> None:
        """Prevent content mutations from silently creating a new draft."""
        if intent_delta is None or intent_delta.operation in {
            "CREATE_POST",
            "OPEN_PLAN",
            "UPDATE_SCHEDULE",
            "PUBLISH_NOW",
            "CANCEL_SCHEDULE",
            "QUERY_SCHEDULE",
            "QUERY_CONTENT",
            "QUERY_PUBLICATION_STATUS",
        }:
            return
        if any(step.tool == "creator.create_draft" for step in plan.steps):
            raise RuntimeError(
                "IntentDelta 要求复用当前 TargetBinding，内容修改计划不能创建新的 Draft"
            )

    async def _save_plan(
        self, run_id: str, plan: AgentPlan, *, replanned: bool = False
    ) -> None:
        graph = graph_descriptor(plan)
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker cannot save plan")
            previous_plan = dict(run.plan or {}) if run.plan else None
            previous_completed = set(
                (run.progress_ledger or {}).get("completed") or []
            )
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
                        "primary_capability": step.primary_capability,
                        "capabilities": step.capabilities,
                        "tool": step.tool,
                        "label": step.label,
                        "success_criteria": step.success_criteria,
                        "expected_artifact_type": step.expected_artifact_type,
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
                "completed": [
                    str(step.task_id)
                    for step in plan.steps
                    if str(step.task_id) in previous_completed
                ],
                "pending": [
                    str(step.task_id)
                    for step in plan.steps
                    if str(step.task_id) not in previous_completed
                ],
                "failed": [],
                "active_layer": None,
                "updated_at": utc_now().isoformat(),
            }
            checkpoint = dict(run.checkpoint or {})
            revisions = list(checkpoint.get("plan_revisions") or [])
            if replanned and previous_plan:
                revisions.append(
                    {
                        "revision": max(0, run.replan_count - 1),
                        "plan_hash": _stable_hash(previous_plan),
                        "plan": previous_plan,
                        "superseded_at": utc_now().isoformat(),
                    }
                )
            checkpoint["plan_revisions"] = revisions[-10:]
            checkpoint["active_plan_revision"] = run.replan_count
            run.checkpoint = checkpoint
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
                f"已理解：{goal[:180]}，正在选择执行路径"
                if goal
                else "已理解需求，正在选择执行路径"
            )
            run.updated_at = utc_now()
            await append_event(
                session,
                run_id,
                "INTENT_UNDERSTOOD",
                intent_detail,
            )

    async def _enter_workload_lane(
        self,
        *,
        run_id: str,
        path: ExecutionPath,
        lane: str,
        classification_summary: str,
        direct_response: str | None,
    ) -> bool:
        now = utc_now()
        capacity = (
            self.settings.max_concurrent_read_runs_per_user
            if lane == "READ"
            else self.settings.max_concurrent_runs_per_user
        )
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if (
                run is None
                or run.lease_owner != self.worker_id
                or run.status != "RUNNING"
            ):
                raise RuntimeError("过期 Worker 不能选择执行通道")
            if session.get_bind().dialect.name == "postgresql":
                await session.scalar(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(
                                f"assistant:lane:{run.user_id}:{lane}"
                            )
                        )
                    )
                )
            active_in_lane = await session.scalar(
                select(func.count(Run.id)).where(
                    Run.user_id == run.user_id,
                    Run.id != run.id,
                    Run.status == "RUNNING",
                    Run.workload_lane == lane,
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at >= now,
                )
            )
            run.execution_path = path
            run.workload_lane = lane
            checkpoint = {
                **dict(run.checkpoint or {}),
                "execution_path": path,
                "workload_lane": lane,
                "classification_summary": classification_summary,
                "execution_selected_at": now.isoformat(),
            }
            if direct_response:
                checkpoint["direct_response"] = direct_response
            run.checkpoint = checkpoint
            run.updated_at = now

            if int(active_in_lane or 0) >= capacity:
                run.status = "WAITING_LANE"
                run.retry_after = now + timedelta(
                    seconds=max(0.1, self.settings.worker_poll_seconds)
                )
                run.lease_owner = None
                run.lease_expires_at = None
                lane_label = "写入" if lane == "WRITE" else "只读"
                run.summary = f"排队中：等待其他{lane_label}任务结束后继续"
                # Surface queue state to the chat UI without completing the Run.
                if not run.final_response:
                    run.final_response = (
                        f"当前已有进行中的{lane_label}任务，你的请求已排队，"
                        "完成后会自动继续执行。"
                    )
                await append_event(
                    session,
                    run.id,
                    "WORKLOAD_LANE_WAITING",
                    {
                        "execution_path": path,
                        "workload_lane": lane,
                        "capacity": capacity,
                        "user_message": run.final_response,
                    },
                )
                return False

            await append_event(
                session,
                run.id,
                "EXECUTION_PATH_SELECTED",
                {
                    "execution_path": path,
                    "workload_lane": lane,
                    "classification_summary": classification_summary,
                    "capacity": capacity,
                },
            )
            return True

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
            previous_ledger = dict(run.progress_ledger or {})
            run.progress_ledger = {
                "completed": [task for task in all_tasks if task in completed],
                "pending": [task for task in all_tasks if task not in completed],
                "failed": [],
                "active_layer": active_layer,
                "updated_at": utc_now().isoformat(),
                **(
                    {
                        "decision": previous_ledger["decision"],
                        "assessment_key": previous_ledger["assessment_key"],
                    }
                    if isinstance(previous_ledger.get("decision"), dict)
                    and isinstance(previous_ledger.get("assessment_key"), str)
                    else {}
                ),
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

    def _should_review_progress(
        self,
        plan: AgentPlan,
        *,
        completed_layer_index: int,
    ) -> bool:
        layers = plan.execution_layers()
        if completed_layer_index >= len(layers):
            return False
        current = layers[completed_layer_index - 1]
        remaining = [
            step
            for layer in layers[completed_layer_index:]
            for step in layer
        ]
        current_is_observation = any(
            self.registry.get(step.tool).risk == RiskLevel.READ
            and self.registry.get(step.tool).requires_progress_review
            for step in current
        )
        next_layer = layers[completed_layer_index]
        next_layer_continues_observation = any(
            self.registry.get(step.tool).risk == RiskLevel.READ
            and not self.registry.get(step.tool).side_effecting
            for step in next_layer
        )
        if next_layer_continues_observation:
            return False
        remaining_uses_observation = any(
            step.depends_on or step.condition is not None for step in remaining
        )
        remaining_has_side_effect = any(
            self.registry.get(step.tool).side_effecting for step in remaining
        )
        return current_is_observation and (
            remaining_uses_observation or remaining_has_side_effect
        )

    @staticmethod
    def _plan_step_signature(plan: AgentPlan) -> str:
        payload = [
            {
                "tool": step.tool,
                "primary_capability": step.primary_capability,
                "arguments": step.arguments,
                "depends_on": step.depends_on,
            }
            for step in plan.steps
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    async def _save_progress_decision(
        self,
        run_id: str,
        decision: ProgressDecision,
        *,
        assessment_key: str,
    ) -> None:
        payload = decision.model_dump(mode="json")
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能保存进度决策")
            run.progress_ledger = {
                **dict(run.progress_ledger or {}),
                "decision": payload,
                "assessment_key": assessment_key,
                "updated_at": utc_now().isoformat(),
            }
            run.checkpoint = {
                **dict(run.checkpoint or {}),
                "progress_decision": payload,
                "progress_assessment_key": assessment_key,
            }
            await append_event(
                session,
                run_id,
                "PROGRESS_ASSESSED",
                {**payload, "assessment_key": assessment_key},
            )

    @classmethod
    def _progress_assessment_key(
        cls,
        plan: AgentPlan,
        completed_task_ids: list[str],
    ) -> str:
        return _stable_hash(
            {
                "plan_signature": cls._plan_step_signature(plan),
                "completed_task_ids": sorted(set(completed_task_ids)),
            }
        )

    async def _load_progress_assessment(
        self,
        run_id: str,
        assessment_key: str,
    ) -> ProgressDecision | None:
        async with self.database.sessions() as session:
            run = await session.get(Run, run_id)
        ledger = dict(run.progress_ledger or {}) if run else {}
        if ledger.get("assessment_key") != assessment_key:
            return None
        payload = ledger.get("decision")
        if not isinstance(payload, dict):
            return None
        return ProgressDecision.model_validate(payload)

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
    def _merge_replan(
        current: AgentPlan,
        revision: AgentPlan,
        *,
        completed_task_ids: set[str],
    ) -> AgentPlan:
        """Replace unfinished work while preserving immutable completed tasks."""
        revision_signature = AgentWorker._plan_step_signature(revision)[:10]
        prefix = f"replan-{revision_signature}"
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
                    for step in [
                        *[
                            step
                            for step in current.steps
                            if str(step.task_id) in completed_task_ids
                        ],
                        *revised_steps,
                    ]
                ],
            }
        )

    async def _step_ordinals(
        self,
        run_id: str,
        plan: AgentPlan,
    ) -> dict[str, int]:
        """Keep task identity stable when a revised plan replaces pending work."""
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(RunStep.task_key, RunStep.ordinal).where(
                        RunStep.run_id == run_id
                    )
                )
            ).all()
        existing = {
            str(task_key): int(ordinal)
            for task_key, ordinal in rows
            if task_key
        }
        next_ordinal = max(existing.values(), default=0) + 1
        resolved: dict[str, int] = {}
        for step in plan.steps:
            task_id = str(step.task_id)
            if task_id in existing:
                resolved[task_id] = existing[task_id]
            else:
                resolved[task_id] = next_ordinal
                next_ordinal += 1
        return resolved

    async def _next_step_ordinal(self, run_id: str) -> int:
        async with self.database.sessions() as session:
            maximum = await session.scalar(
                select(func.max(RunStep.ordinal)).where(RunStep.run_id == run_id)
            )
        return int(maximum or 0) + 1

    async def _start_step(
        self, run_id: str, ordinal: int, planned: AgentPlanStep
    ) -> RunStep:
        async with self.database.sessions() as session, session.begin():
            # Global state-transition lock order: parent Run before RunStep.
            run = await session.get(Run, run_id, with_for_update=True)
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

    async def _prepare_artifact_lifecycle(
        self,
        *,
        session: Any,
        run: Run,
        step: RunStep,
        output: dict[str, Any],
        artifact_type: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Compute immutable lifecycle metadata before Artifact INSERT."""
        if not run.goal_id:
            return dict(output), {}
        goal = await session.get(ConversationGoalRecord, run.goal_id)
        if goal is None:
            return dict(output), {}
        operation_class = await session.scalar(
            select(IntentDeltaRecord.operation_class)
            .where(IntentDeltaRecord.run_id == run.id)
            .limit(1)
        )
        if not self._allows_target_state_write(operation_class):
            return dict(output), {}
        context = TargetContext.model_validate(goal.target_context or {})
        content = context.content_target
        schedule = context.schedule_target
        if artifact_type == "CONTENT_DRAFT":
            parent = (
                await session.get(Artifact, content.artifact_id)
                if content and content.artifact_id
                else None
            )
            delta = await session.scalar(
                select(IntentDeltaRecord)
                .where(IntentDeltaRecord.run_id == run.id)
                .limit(1)
            )
            return dict(output), {
                "parent_artifact_id": parent.id if parent else None,
                "parent_artifact_ids": [parent.id] if parent else [],
                "version": (parent.version + 1) if parent else 1,
                "change_type": str(delta.operation) if delta else "CREATE_POST",
            }
        if artifact_type in {"SCHEDULE_RECEIPT", "PUBLICATION_RECEIPT"}:
            if content is None or not content.artifact_id:
                raise ValueError(
                    "Schedule/Publication must bind a concrete content artifact version"
                )
            content_version = content.content_artifact_version or content.version
            if artifact_type == "PUBLICATION_RECEIPT" and schedule is not None and (
                schedule.content_artifact_id != content.artifact_id
                or schedule.content_artifact_version != content_version
            ):
                raise ValueError(
                    "Publication schedule is bound to a different content artifact version"
                )
            return {
                **output,
                "content_artifact_id": content.artifact_id,
                "content_artifact_version": content_version,
            }, {
                "change_type": (
                    "UPDATE_SCHEDULE"
                    if artifact_type == "SCHEDULE_RECEIPT"
                    else "PUBLISH_NOW"
                )
            }
        return dict(output), {}

    async def _complete_step(self, step_id: str, output: dict[str, Any]) -> None:
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(RunStep.run_id).where(RunStep.id == step_id)
            )
            if run_id is None:
                return
            run = await session.get(Run, run_id, with_for_update=True)
            step = await session.get(RunStep, step_id, with_for_update=True)
            if step is None or step.run_id != run_id:
                return
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 的步骤结果已拒绝")
            if step.status == "COMPLETED":
                return
            receipt = await session.scalar(
                select(ToolExecutionReceipt).where(
                    ToolExecutionReceipt.run_id == run.id,
                    ToolExecutionReceipt.step_id == step.id,
                )
            )
            provenance_key = (
                receipt.idempotency_key
                if receipt is not None
                else f"tool-step:{step.id}"
            )
            artifact_type = self.registry.get(str(step.tool_name)).artifact_type
            output, lifecycle = await self._prepare_artifact_lifecycle(
                session=session,
                run=run,
                step=step,
                output=output,
                artifact_type=artifact_type,
            )
            step.status = "COMPLETED"
            step.output = output
            step.completed_at = utc_now()
            artifact = await publish_step_artifact(
                session,
                step=step,
                output=output,
                artifact_type=artifact_type,
                provenance_key=provenance_key,
                **lifecycle,
            )
            if receipt is not None:
                receipt.result_ref = f"artifact:{artifact.id}"
            operation_class = await session.scalar(
                select(IntentDeltaRecord.operation_class)
                .where(IntentDeltaRecord.run_id == run.id)
                .limit(1)
            )
            if self._allows_target_state_write(operation_class):
                await self._update_active_target_from_artifact(
                    session=session,
                    run=run,
                    artifact=artifact,
                    output=output,
                )
            await append_event(
                session,
                step.run_id,
                "STEP_COMPLETED",
                {
                    "step_id": step.id,
                    "ordinal": step.ordinal,
                    "tool": step.tool_name,
                    "artifact_id": artifact.id,
                    "artifact_type": artifact.artifact_type,
                    "artifact_version": artifact.version,
                    "output": output,
                },
            )

    async def _update_active_target_from_artifact(
        self,
        *,
        session: Any,
        run: Run,
        artifact: Artifact,
        output: dict[str, Any],
    ) -> None:
        """Promote a successful immutable artifact into typed Goal targets."""

        if not run.goal_id:
            return
        goal = await session.get(ConversationGoalRecord, run.goal_id, with_for_update=True)
        if goal is None:
            return

        artifact_type = str(artifact.artifact_type or "")
        draft_id = str(output.get("draft_id") or output.get("draftId") or "").strip()
        post_id = str(output.get("post_id") or output.get("postId") or "").strip()
        content_sha256 = str(
            output.get("content_sha256") or output.get("contentSha256") or ""
        ).lower() or None

        records = list(
            (
                await session.scalars(
                    select(TargetBindingRecord)
                    .where(TargetBindingRecord.goal_id == goal.id)
                    .order_by(
                        TargetBindingRecord.version.desc(),
                        TargetBindingRecord.created_at.desc(),
                    )
                    .limit(30)
                )
            ).all()
        )
        current = records[0] if records else None
        content_record = next(
            (item for item in records if item.target_type in {"DRAFT", "POST"}),
            None,
        )

        if artifact_type == "CONTENT_DRAFT" and draft_id:
            parent = (
                await session.get(Artifact, content_record.artifact_id)
                if content_record and content_record.artifact_id
                else None
            )
            if parent:
                await self._ensure_artifact_relation(
                    session=session,
                    source_artifact_id=artifact.id,
                    target_artifact_id=parent.id,
                    relation_type="DERIVED_FROM",
                )
                if str(output.get("supersedes_draft_id") or "").strip():
                    await self._ensure_artifact_relation(
                        session=session,
                        source_artifact_id=artifact.id,
                        target_artifact_id=parent.id,
                        relation_type="SUPERSEDES",
                    )
            # Unchanged verify (get_own_draft) must not allocate a binding
            # version — parallel schedule verifies already claim the next slots.
            # Replay of the same Creator completion also hits this path when the
            # Goal already points at this draft/sha (and usually this artifact).
            if (
                content_record is not None
                and content_record.target_id == draft_id
                and content_sha256
                and (content_record.content_sha256 or "").lower() == content_sha256
            ):
                if parent:
                    await self._ensure_artifact_relation(
                        session=session,
                        source_artifact_id=artifact.id,
                        target_artifact_id=parent.id,
                        relation_type="DERIVED_FROM",
                    )
                    if str(output.get("supersedes_draft_id") or "").strip():
                        await self._ensure_artifact_relation(
                            session=session,
                            source_artifact_id=artifact.id,
                            target_artifact_id=parent.id,
                            relation_type="SUPERSEDES",
                        )
                return
            # TargetBinding.version is a per-goal monotonic counter shared by
            # CONTENT/SCHEDULE/PUBLICATION rows. Artifact.version is a separate
            # content lineage counter — never reuse it as the binding version.
            next_binding_version = await self._allocate_target_binding_version(
                session, goal_id=goal.id
            )
            content_artifact_version = int(artifact.version or 1)
            binding = TargetBindingRecord(
                goal_id=goal.id,
                target_type="DRAFT",
                role="CONTENT",
                target_id=draft_id,
                artifact_id=artifact.id,
                content_sha256=content_sha256,
                version=next_binding_version,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
                schedule_id=None,
                content_artifact_id=artifact.id,
                content_artifact_version=content_artifact_version,
            )
            session.add(binding)
            next_context = self._merge_target_context(
                self._authoritative_target_context(goal, records),
                TargetBinding(
                    target_type="DRAFT",
                    role="CONTENT",
                    target_id=draft_id,
                    artifact_id=artifact.id,
                    content_sha256=content_sha256,
                    version=next_binding_version,
                    confidence=1.0,
                    resolution_method="TOOL_OUTPUT",
                    schedule_id=None,
                    content_artifact_id=artifact.id,
                    content_artifact_version=content_artifact_version,
                ),
            )
            await self._cas_goal_target_context(
                session=session,
                goal=goal,
                target_context=next_context,
                active_target_ref=f"draft:{draft_id}",
                phase="READY",
            )
            return

        if artifact_type == "SCHEDULE_RECEIPT":
            action_id = str(
                output.get("action_id") or output.get("actionId") or ""
            ).strip()
            if not action_id:
                return
            next_version = await self._allocate_target_binding_version(
                session, goal_id=goal.id
            )
            binding = TargetBindingRecord(
                goal_id=goal.id,
                target_type="SCHEDULE",
                role="SCHEDULE",
                target_id=action_id,
                artifact_id=artifact.id,
                version=next_version,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
                schedule_id=action_id,
            )
            session.add(binding)
            schedule_binding = TargetBinding(
                target_type="SCHEDULE",
                role="SCHEDULE",
                target_id=action_id,
                artifact_id=artifact.id,
                version=next_version,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
                schedule_id=action_id,
            )
            current_context = self._authoritative_target_context(goal, records)
            content_target = current_context.content_target
            if content_target is None or not content_target.artifact_id:
                raise ValueError(
                    "Schedule must bind a concrete content artifact version"
                )
            content_version = content_target.content_artifact_version or content_target.version
            binding.content_artifact_id = content_target.artifact_id
            binding.content_artifact_version = content_version
            schedule_binding = schedule_binding.model_copy(
                update={
                    "content_artifact_id": content_target.artifact_id,
                    "content_artifact_version": content_version,
                }
            )
            await self._ensure_artifact_relation(
                session=session,
                source_artifact_id=artifact.id,
                target_artifact_id=content_target.artifact_id,
                relation_type="SCHEDULED_FROM",
            )
            schedule_status = str(output.get("status") or "").upper()
            if schedule_status in {"CANCELLED", "COMPLETED", "FAILED"}:
                next_context = current_context.model_copy(
                    update={"schedule_target": None}
                )
                content_target = next_context.content_target
                next_active_target_ref = (
                    f"{content_target.target_type.lower()}:{content_target.target_id}"
                    if content_target is not None
                    else None
                )
                next_phase = "PUBLISHED" if schedule_status == "COMPLETED" else "READY"
            else:
                next_context = self._merge_target_context(
                    current_context,
                    schedule_binding,
                )
                next_active_target_ref = (
                    f"draft:{draft_id}" if draft_id else f"schedule:{action_id}"
                )
                next_phase = "SCHEDULED"
            await self._cas_goal_target_context(
                session=session,
                goal=goal,
                target_context=next_context,
                active_target_ref=next_active_target_ref,
                phase=next_phase,
            )
            return

        if artifact_type == "PUBLICATION_RECEIPT" and post_id:
            current_context = self._authoritative_target_context(goal, records)
            content_target = current_context.content_target
            schedule_target = current_context.schedule_target
            if content_target is None or not content_target.artifact_id:
                raise ValueError(
                    "Publication must reference a concrete content artifact version"
                )
            content_version = content_target.content_artifact_version or content_target.version
            if schedule_target is not None and (
                schedule_target.content_artifact_id != content_target.artifact_id
                or schedule_target.content_artifact_version != content_version
            ):
                raise ValueError(
                    "Publication schedule is bound to a different content artifact version"
                )
            await self._ensure_artifact_relation(
                session=session,
                source_artifact_id=artifact.id,
                target_artifact_id=content_target.artifact_id,
                relation_type="PUBLISHED_FROM",
            )
            if schedule_target is not None and schedule_target.artifact_id:
                await self._ensure_artifact_relation(
                    session=session,
                    source_artifact_id=artifact.id,
                    target_artifact_id=schedule_target.artifact_id,
                    relation_type="PUBLISHED_FROM",
                )
            next_version = await self._allocate_target_binding_version(
                session, goal_id=goal.id
            )
            binding = TargetBindingRecord(
                goal_id=goal.id,
                target_type="POST",
                role="PUBLICATION",
                target_id=post_id,
                artifact_id=artifact.id,
                content_artifact_id=content_target.artifact_id,
                content_artifact_version=content_version,
                version=next_version,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
            )
            session.add(binding)
            next_context = self._merge_target_context(
                current_context,
                TargetBinding(
                    target_type="POST",
                    role="PUBLICATION",
                    target_id=post_id,
                    artifact_id=artifact.id,
                    content_artifact_id=content_target.artifact_id,
                    content_artifact_version=content_version,
                    version=next_version,
                    confidence=1.0,
                    resolution_method="TOOL_OUTPUT",
                ),
            )
            await self._cas_goal_target_context(
                session=session,
                goal=goal,
                target_context=next_context,
                active_target_ref=f"post:{post_id}",
                phase="PUBLISHED",
            )
            return

        if artifact_type == "POST_CONTENT" and post_id:
            next_version = await self._allocate_target_binding_version(
                session, goal_id=goal.id
            )
            binding = TargetBindingRecord(
                goal_id=goal.id,
                target_type="POST",
                role="PUBLICATION",
                target_id=post_id,
                artifact_id=artifact.id,
                version=next_version,
                confidence=1.0,
                resolution_method="TOOL_OUTPUT",
            )
            session.add(binding)
            next_context = self._merge_target_context(
                self._target_context_from_records(records),
                TargetBinding(
                    target_type="POST",
                    role="PUBLICATION",
                    target_id=post_id,
                    artifact_id=artifact.id,
                    version=next_version,
                    confidence=1.0,
                    resolution_method="TOOL_OUTPUT",
                ),
            )
            await self._cas_goal_target_context(
                session=session,
                goal=goal,
                target_context=next_context,
                active_target_ref=f"post:{post_id}",
                phase="READY",
            )

    @staticmethod
    def _next_target_binding_version(records: list[Any]) -> int:
        """Allocate the next TargetBinding.version for a goal.

        Binding versions are shared across CONTENT/SCHEDULE/PUBLICATION roles
        under UniqueConstraint(goal_id, version). Content artifact lineage
        versions must not be reused here.
        """

        if not records:
            return 1
        return max(int(item.version or 0) for item in records) + 1

    @staticmethod
    async def _allocate_target_binding_version(
        session: Any, *, goal_id: str
    ) -> int:
        """Read the live max binding version under the goal row lock."""

        max_version = await session.scalar(
            select(func.max(TargetBindingRecord.version)).where(
                TargetBindingRecord.goal_id == goal_id
            )
        )
        return int(max_version or 0) + 1

    @staticmethod
    async def _cas_goal_target_context(
        *,
        session: Any,
        goal: ConversationGoalRecord,
        target_context: TargetContext,
        active_target_ref: str | None,
        phase: str,
    ) -> None:
        """Persist a typed target transition only if its source version is current."""

        expected_version = goal.version
        result = await session.execute(
            update(ConversationGoalRecord)
            .where(
                ConversationGoalRecord.id == goal.id,
                ConversationGoalRecord.version == expected_version,
            )
            .values(
                target_context=target_context.model_dump(mode="json"),
                active_target_ref=active_target_ref,
                phase=phase,
                status="COMPLETED" if phase == "PUBLISHED" else "ACTIVE",
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise TransientToolError(
                f"TargetContext CAS conflict for goal {goal.id} at version {expected_version}"
            )

    @staticmethod
    async def _ensure_artifact_relation(
        *,
        session: Any,
        source_artifact_id: str,
        target_artifact_id: str,
        relation_type: str,
    ) -> None:
        existing = await session.scalar(
            select(ArtifactRelation).where(
                ArtifactRelation.source_artifact_id == source_artifact_id,
                ArtifactRelation.target_artifact_id == target_artifact_id,
                ArtifactRelation.relation_type == relation_type,
            )
        )
        if existing is None:
            session.add(
                ArtifactRelation(
                    source_artifact_id=source_artifact_id,
                    target_artifact_id=target_artifact_id,
                    relation_type=relation_type,
                )
            )

    async def _fail_step(self, step_id: str, error: str) -> None:
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(RunStep.run_id).where(RunStep.id == step_id)
            )
            if run_id is None:
                return
            run = await session.get(Run, run_id, with_for_update=True)
            step = await session.get(RunStep, step_id, with_for_update=True)
            if step is None or step.run_id != run_id:
                return
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
            poll_seconds = self.settings.creator_dependency_poll_seconds
            if dependency.status == "WAITING_HUMAN":
                # Human waits are long-lived; avoid high-frequency polling.
                poll_seconds = max(float(poll_seconds), 120.0)
            run.retry_after = now + timedelta(seconds=poll_seconds)
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
        target_context = await self._load_target_context(run)
        resolved_targets = self._resolved_targets_for_tool(tool, target_context)
        intent_delta = await self._load_intent_delta(run)
        if intent_delta is not None and intent_delta.operation_class == "READ":
            resolved_targets = {
                role: ResolvedTargetView.from_binding(
                    goal_id=intent_delta.goal_id,
                    binding=binding,
                )
                for role, binding in resolved_targets.items()
            }
        args = self._resolve_arguments(
            run=run,
            tool=tool,
            arguments=dict(plan_step.arguments),
            previous_outputs=previous_outputs,
            artifact_sources=plan_step.artifact_sources,
            resolved_targets=resolved_targets,
        )
        definition = self.registry.get(tool)
        request_id = f"tool-{run.id}-{ordinal}-{uuid.uuid4().hex[:12]}"
        operation_key = (
            f"assistant-effect-{run.id}-{ordinal}"
            if definition.side_effecting
            else f"assistant-read-{run.id}-{ordinal}"
        )
        context = ToolInvocationContext(
            run_id=run.id,
            step_id=None,
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            conversation_id=run.conversation_id,
            request_id=request_id,
            operation_key=operation_key,
            idempotency_key=operation_key,
            attempt=max(1, int(run.attempts or 1)),
            deadline_at=run.deadline_at,
            workload_lane=getattr(run, "workload_lane", None),
            trace_metadata={
                "ordinal": ordinal,
                "tool": tool,
                "risk": definition.risk.value,
            },
        )
        try:
            credentials = ToolCredentials(
                access_token=self.token_vault.decrypt(run.delegated_token),
                trace_id=run.trace_id,
            )
            result = await self.execution_runtime.invoke(
                tool_name=tool,
                arguments=args,
                context=context,
                run=run,
                ordinal=ordinal,
                credentials=credentials,
                skip_input_validation=False,
                raise_on_failure=True,
            )
        except ToolRuntimeError as exc:
            await self._record_tool_invocation_event(
                run_id=run.id,
                tool=tool,
                context=context,
                status=exc.status.value,
                error_code=exc.error_code,
                duration_ms=exc.duration_ms,
                trace_id=exc.trace_id,
            )
            if exc.status == ToolInvocationStatus.UNKNOWN:
                raise UnknownSideEffectError(
                    str(exc),
                    error_code=exc.error_code
                    or ToolErrorCode.UNKNOWN_SIDE_EFFECT.value,
                    operation_key=exc.operation_key or operation_key,
                ) from exc
            if exc.status == ToolInvocationStatus.DENIED:
                raise PermissionError(str(exc)) from exc
            if exc.status == ToolInvocationStatus.RETRYABLE_FAILURE:
                raise TransientToolError(str(exc)) from exc
            raise PermanentToolError(str(exc)) from exc

        await self._record_tool_invocation_event(
            run_id=run.id,
            tool=tool,
            context=context,
            status=result.status.value,
            error_code=result.error_code,
            duration_ms=result.duration_ms,
            trace_id=result.trace_id,
        )
        if result.output is None:
            raise PermanentToolError(f"{tool} returned empty output")
        return result.output

    async def _record_tool_invocation_event(
        self,
        *,
        run_id: str,
        tool: str,
        context: ToolInvocationContext,
        status: str,
        error_code: str | None,
        duration_ms: int,
        trace_id: str | None,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            await append_event(
                session,
                run_id,
                "TOOL_INVOCATION",
                {
                    "tool": tool,
                    "trace_id": trace_id,
                    "request_id": context.request_id,
                    "operation_key": context.operation_key,
                    "status": status,
                    "error_code": error_code,
                    "duration_ms": duration_ms,
                    "attempt": context.attempt,
                },
            )

    async def _legacy_tool_executor(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        run: Run,
        ordinal: int,
        continuation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Step 1 bridge: policy / ledger / dispatch remain on Worker."""
        if tool_name in MIGRATED_READ_TOOLS or tool_name in MIGRATED_WRITE_TOOLS:
            raise RuntimeError(
                f"{tool_name} is migrated and must not enter legacy_executor"
            )
        del continuation
        tool = tool_name
        args = arguments
        definition = self.registry.get(tool)
        approval_granted = (
            await self._has_approval(run.id, ordinal, args)
            if definition.risk == RiskLevel.EXTERNAL_WRITE
            else False
        )
        decision = self.policy.evaluate(
            context=PolicyContext(
                run_id=run.id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                principal_role=run.principal_role,
                action=tool,
                resource=_policy_resource(tool, args, definition),
                approval_granted=approval_granted,
            ),
            definition=definition,
            registry=self.registry,
        )
        await self._record_policy_decision(
            run=run,
            tool=tool,
            resource=_policy_resource(tool, args, definition),
            decision=decision,
        )
        if decision.decision == PolicyDecisionType.DENY:
            raise PermissionError(decision.reason)
        if decision.decision == PolicyDecisionType.REQUIRE_APPROVAL:
            raise ApprovalRequired(args)
        if definition.execution_mode == ExecutionMode.ASYNC:
            return await self._enqueue_or_read_tool_job(
                run=run,
                tool=tool,
                args=args,
                ordinal=ordinal,
            )
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
            operation_key=context.operation_key
            or f"assistant-read-{run.id}-{ordinal}",
            continuation=None,
        )
        return self.registry.validate_output(
            tool, raw_output, args, run_id=run.id
        )

    async def _record_policy_decision(
        self,
        *,
        run: Run,
        tool: str,
        resource: dict[str, Any],
        decision: PolicyDecision,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            session.add(
                PolicyAudit(
                    run_id=run.id,
                    user_id=run.user_id,
                    tenant_id=run.tenant_id,
                    principal_role=run.principal_role,
                    action=tool,
                    resource=resource,
                    decision=decision.decision.value,
                    reason=decision.reason,
                    policy_version=decision.policy_version,
                    context={"limits": decision.limits},
                )
            )
            await append_event(
                session,
                run.id,
                "POLICY_DECIDED",
                {
                    "action": tool,
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "policy_version": decision.policy_version,
                },
            )

    async def _enqueue_or_read_tool_job(
        self,
        *,
        run: Run,
        tool: str,
        args: dict[str, Any],
        ordinal: int,
    ) -> dict[str, Any]:
        request_hash = _stable_hash({"tool": tool, "arguments": args})
        idempotency_key = f"assistant-tool-job-{run.id}-{ordinal}"
        async with self.database.sessions() as session, session.begin():
            job = await session.scalar(
                select(ToolJob)
                .where(
                    ToolJob.run_id == run.id,
                    ToolJob.step_ordinal == ordinal,
                    ToolJob.tool_name == tool,
                )
                .with_for_update()
            )
            if job is None:
                owning_run = await session.get(Run, run.id, with_for_update=True)
                if (
                    owning_run is None
                    or owning_run.lease_owner != self.worker_id
                ):
                    raise RuntimeError("过期 Worker 不能创建异步工具任务")
                if owning_run.tool_calls >= owning_run.max_tool_calls:
                    raise RuntimeError("工具调用预算已耗尽")
                owning_run.tool_calls += 1
                job = ToolJob(
                    run_id=run.id,
                    step_ordinal=ordinal,
                    tool_name=tool,
                    arguments=args,
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    status="PENDING",
                    max_attempts=self.settings.tool_job_max_attempts,
                    next_attempt_at=utc_now(),
                )
                session.add(job)
                await session.flush()
                await append_event(
                    session,
                    run.id,
                    "BUDGET_UPDATED",
                    {
                        "model_calls": owning_run.model_calls,
                        "tool_calls": owning_run.tool_calls,
                        "replan_count": owning_run.replan_count,
                    },
                )
                await append_event(
                    session,
                    run.id,
                    "TOOL_JOB_QUEUED",
                    {
                        "job_id": job.id,
                        "tool": tool,
                        "max_attempts": job.max_attempts,
                    },
                )
            elif job.request_hash != request_hash:
                raise RuntimeError(
                    "同一步骤的异步工具参数已变化，拒绝复用旧任务"
                )
            if job.status == "COMPLETED" and job.result is not None:
                return self.registry.validate_output(
                    tool,
                    dict(job.result),
                    args,
                    run_id=run.id,
                )
            if job.status == "DEAD_LETTER":
                raise RuntimeError(
                    f"异步工具任务进入 Dead Letter：{job.error or tool}"
                )
            if job.status == "CANCELLED":
                raise RuntimeError("异步工具任务已取消")
            job_id = job.id
            job_status = job.status
            state = {
                "job_id": job.id,
                "tool": tool,
                "status": job.status,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
            }
        raise DependencyPending(
            task_id=job_id,
            status=job_status,
            state=state,
            dependency_type="TOOL_JOB",
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
        handler = self.execution_runtime.handler_for(tool)
        if handler is None:
            raise ValueError(f"No execution handler registered for tool: {tool}")
        return await handler(
            run=run,
            tool=tool,
            args=args,
            ordinal=ordinal,
            timeout_seconds=timeout_seconds,
            operation_key=operation_key,
            continuation=continuation,
        )

    async def _dispatch_builtin_tool(
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
        if tool in MIGRATED_READ_TOOLS or tool in MIGRATED_WRITE_TOOLS:
            raise RuntimeError(
                f"{tool} was migrated to ToolRuntime and must not use "
                "_dispatch_builtin_tool"
            )
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
        if tool == "community.list_active_users":
            capability = await self._issue_capability(
                run,
                action=tool,
                resources=[],
            )
            return await asyncio.wait_for(
                self.community.list_active_users(
                    days=int(args["days"]),
                    limit=int(args["limit"]),
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.list_posts_by_users":
            capability = await self._issue_capability(
                run,
                action=tool,
                resources=[],
            )
            return await asyncio.wait_for(
                self.community.list_posts_by_users(
                    user_ids=list(args["user_ids"]),
                    days=int(args["days"]),
                    limit=int(args["limit"]),
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.aggregate_post_topics":
            capability = await self._issue_capability(
                run,
                action=tool,
                resources=[],
            )
            return await asyncio.wait_for(
                self.community.aggregate_post_topics(
                    user_ids=list(args["user_ids"]),
                    days=int(args["days"]),
                    limit=int(args["limit"]),
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
        if tool == "community.get_own_draft":
            draft_id = str(args["draft_id"])
            capability = await self._issue_capability(
                run,
                action="community.get_own_draft",
                resources=[f"post:{draft_id}"],
            )
            try:
                draft = await asyncio.wait_for(
                    self.community.get_own_draft(
                        draft_id,
                        capability_token=capability.token,
                        trace_id=run.trace_id,
                    ),
                    timeout=timeout_seconds,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    raise await self._draft_access_denied_error(
                        run=run,
                        draft_id=draft_id,
                        timeout_seconds=timeout_seconds,
                    ) from exc
                raise
            guarded_draft = guard_post_payload(draft)
            content_sha256 = str(
                draft.get("contentSha256")
                or draft.get("content_sha256")
                or ""
            ).lower()
            return {
                "draft_id": str(draft.get("id") or draft_id),
                "title": guarded_draft.get("title"),
                "description": guarded_draft.get("description"),
                "body_markdown": guarded_draft.get("bodyMarkdown")
                or guarded_draft.get("body_markdown"),
                "tags": list(guarded_draft.get("tags") or []),
                "status": "READY",
                "content_sha256": content_sha256,
                "untrusted_content": True,
                "injection_signals": list(
                    guarded_draft.get("injection_signals") or []
                ),
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
        if tool in {"creator.create_draft", "creator.revise_draft"}:
            raise RuntimeError(
                f"{tool} was migrated to ToolRuntime and must not use "
                "_dispatch_builtin_tool"
            )
        if tool in {"publication.schedule", "publication.cancel_schedule"}:
            raise RuntimeError(
                f"{tool} was migrated to ToolRuntime and must not use "
                "_dispatch_builtin_tool"
            )
        if tool == "publication.schedule_batch":
            base_run_at = _parse_run_at(args["run_at"])
            interval = int(args["interval_minutes"])
            items = list(args["items"])
            final_run_at = base_run_at + timedelta(
                minutes=interval * (len(items) - 1)
            )
            if base_run_at <= utc_now() + timedelta(
                seconds=self.settings.publication_min_lead_seconds
            ):
                raise ValueError("批量定时发布时间必须至少晚于当前时间 15 秒")
            if final_run_at > utc_now() + timedelta(
                days=self.settings.publication_max_schedule_days
            ):
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
            raise RuntimeError(
                f"{tool} was migrated to ToolRuntime and must not use "
                "_dispatch_builtin_tool"
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
            chunk_size = self.settings.deletion_batch_chunk_size
            for chunk_index, start in enumerate(range(0, len(post_ids), chunk_size)):
                chunk = post_ids[start : start + chunk_size]
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
        except UnknownSideEffectError as unknown:
            await self._finish_side_effect(
                run.id,
                ordinal,
                status="UNKNOWN",
                error=str(unknown),
            )
            if unknown.operation_key is None:
                unknown.operation_key = operation_key
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
                # Write result is unconfirmed — keep operation_key and forbid
                # blind automatic re-invocation (Step 3 adds reconciliation).
                raise UnknownSideEffectError(
                    f"{tool} 结果未知，禁止盲目重试：{exc}",
                    operation_key=operation_key,
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
            step = await session.scalar(
                select(RunStep).where(
                    RunStep.run_id == run_id,
                    RunStep.ordinal == ordinal,
                )
            )
            if step is None:
                raise RuntimeError("Cannot prepare a tool execution without a RunStep")
            receipt = await session.scalar(
                select(ToolExecutionReceipt)
                .where(
                    ToolExecutionReceipt.run_id == run_id,
                    ToolExecutionReceipt.step_id == step.id,
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
                if receipt is None:
                    receipt = ToolExecutionReceipt(
                        run_id=run_id,
                        step_id=step.id,
                        tool_name=tool,
                        idempotency_key=operation_key,
                        input_hash=request_hash,
                        status="PREPARED",
                        result_ref=f"side-effect:{effect.id}",
                    )
                    session.add(receipt)
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
                if receipt is None:
                    receipt = ToolExecutionReceipt(
                        run_id=run_id,
                        step_id=step.id,
                        tool_name=tool,
                        idempotency_key=effect.operation_key,
                        input_hash=request_hash,
                        status=effect.status,
                        result_ref=f"side-effect:{effect.id}",
                    )
                    session.add(receipt)
                    await session.flush()
                if effect.status == "COMPLETED" and effect.result is not None:
                    receipt.status = "COMPLETED"
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
            if (
                receipt.tool_name != tool
                or receipt.input_hash != request_hash
                or receipt.idempotency_key != operation_key
            ):
                raise RuntimeError(
                    "Tool execution idempotency key was reused with different input"
                )
            receipt.status = "IN_FLIGHT"
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
            receipt = await session.scalar(
                select(ToolExecutionReceipt)
                .where(ToolExecutionReceipt.idempotency_key == effect.operation_key)
                .with_for_update()
            )
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 的副作用结果已拒绝")
            effect.status = status
            effect.result = result
            effect.remote_operation_id = remote_operation_id
            effect.error = error[:4_000] if error else None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            if receipt is not None:
                receipt.status = status
                receipt.result_ref = f"side-effect:{effect.id}"
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

    async def _draft_access_denied_error(
        self,
        *,
        run: Run,
        draft_id: str,
        timeout_seconds: float,
    ) -> PermanentToolError:
        """Translate draft 403 into an actionable user error and sync Goal phase."""

        status_label = "不可再作为草稿读取"
        try:
            capability = await self._issue_capability(
                run,
                action="community.get_post",
                resources=[f"post:{draft_id}"],
            )
            post = await asyncio.wait_for(
                self.community.get_post(
                    draft_id,
                    capability_token=capability.token,
                    trace_id=run.trace_id,
                ),
                timeout=timeout_seconds,
            )
            raw_status = str(
                post.get("status") or post.get("postStatus") or ""
            ).strip().lower()
            title = str(post.get("title") or "").strip()
            if raw_status in {"published", "public", "online"}:
                status_label = "已发布"
                if run.goal_id:
                    async with self.database.sessions() as session, session.begin():
                        goal = await session.get(
                            ConversationGoalRecord,
                            run.goal_id,
                            with_for_update=True,
                        )
                        if goal is not None and goal.phase != "PUBLISHED":
                            context = TargetContext.model_validate(
                                goal.target_context or {}
                            )
                            content = context.content_target
                            publication = TargetBinding(
                                target_type="POST",
                                role="PUBLICATION",
                                target_id=draft_id,
                                artifact_id=(
                                    content.artifact_id if content else None
                                ),
                                content_sha256=(
                                    content.content_sha256 if content else None
                                ),
                                version=(
                                    (content.version + 1) if content else 1
                                ),
                                confidence=1.0,
                                resolution_method="TOOL_OUTPUT",
                                content_artifact_id=(
                                    content.content_artifact_id
                                    or (content.artifact_id if content else None)
                                ),
                                content_artifact_version=(
                                    content.content_artifact_version
                                    if content
                                    else None
                                ),
                            )
                            # Avoid UniqueViolation: only CAS phase/status here.
                            await self._cas_goal_target_context(
                                session=session,
                                goal=goal,
                                target_context=context.model_copy(
                                    update={
                                        "publication_target": publication,
                                        "schedule_target": None,
                                    }
                                ),
                                active_target_ref=f"post:{draft_id}",
                                phase="PUBLISHED",
                            )
                titled = f"《{title}》" if title else "该帖子"
                return PermanentToolError(
                    f"{titled}已经发布，不能再修改草稿内容或定时任务。"
                    "如需补充实战经验，请新开一篇帖子；"
                    "若只想调整已发布内容，请说明具体改法。"
                )
            if raw_status:
                status_label = raw_status
        except Exception:
            # Fall through to the generic denial message.
            pass
        return PermanentToolError(
            f"无法读取草稿 {draft_id}（{status_label}）。"
            "该内容可能已发布、不属于当前用户，或不是 AI 草稿。"
        )

    @staticmethod
    def _resolved_targets_for_tool(
        tool: str,
        target_context: TargetContext,
    ) -> dict[str, Any]:
        """Project the authoritative context into typed runtime targets."""
        resolved: dict[str, Any] = {}
        if tool in {
            "publication.get_schedule",
            "publication.update_schedule",
            "publication.cancel_schedule",
        }:
            if target_context.schedule_target is not None:
                resolved["SCHEDULE"] = target_context.schedule_target
        elif tool in {"community.get_own_draft", "creator.revise_draft"}:
            if target_context.content_target is not None:
                resolved["CONTENT"] = target_context.content_target
        elif tool in {"publication.publish_now", "publication.schedule"}:
            if target_context.content_target is not None:
                resolved["CONTENT"] = target_context.content_target
            if tool == "publication.publish_now" and target_context.schedule_target is not None:
                resolved["SCHEDULE"] = target_context.schedule_target
        return resolved

    @staticmethod
    def _allows_target_state_write(operation_class: str | None) -> bool:
        """READ operations may observe targets but never bind or promote them."""

        return operation_class != "READ"

    def _resolve_arguments(
        self,
        *,
        run: Run,
        tool: str,
        arguments: dict[str, Any],
        previous_outputs: list[dict[str, Any]],
        artifact_sources: dict[str, list[str]] | None = None,
        resolved_targets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        registry = getattr(self, "registry", tool_registry)
        runtime = getattr(self, "tool_runtime", tool_adapter_runtime)
        definition = registry.get(tool)
        typed_outputs = [
            {
                **item,
                "artifact_type": item.get("artifact_type")
                or self._compatibility_artifact_type(item, registry),
            }
            for item in previous_outputs
        ]
        # A tool that runs later in the *same* run may consume the artifact
        # produced by an earlier step before the worker has reloaded the
        # persisted goal.  Materialize that output as an ephemeral
        # TargetBinding.  This is deliberately scoped to the current run's
        # typed artifact output; it is not a latest-draft lookup and never
        # crosses conversation/run boundaries.
        if resolved_targets is None or (
            isinstance(resolved_targets, dict)
            and not resolved_targets
            and definition.required_target_roles
        ):
            resolved_targets = self._resolved_targets_from_current_run_outputs(
                tool=tool,
                typed_outputs=typed_outputs,
            )
        args = runtime.prepare_arguments(
            definition=definition,
            planner_arguments=arguments,
            artifacts=typed_outputs,
            context=ToolRuntimeContext(
                prompt=run.prompt,
                context_post_id=getattr(run, "context_post_id", None),
                context_comment_id=getattr(run, "context_comment_id", None),
                resolved_targets=resolved_targets,
            ),
            binding_sources=artifact_sources,
        )
        if tool == "community.search_posts":
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
        elif tool in {"publication.schedule", "publication.publish_now"}:
            supplied_draft_id = str(arguments.get("draft_id") or "").strip()
            if (
                supplied_draft_id
                and not self._is_draft_placeholder(supplied_draft_id)
                and supplied_draft_id != str(args.get("draft_id") or "")
            ):
                raise ValueError(
                    "发布只能使用当前任务中 Creator 生成并绑定版本的草稿"
                )
            supplied_sha = str(
                arguments.get("expected_content_sha256") or ""
            ).lower()
            if supplied_sha and supplied_sha != str(
                args.get("expected_content_sha256") or ""
            ).lower():
                raise ValueError("计划中的草稿版本与 Creator 实际产物不一致")
        elif tool == "publication.schedule_batch":
            bound_drafts = {
                str(item["draft_id"]): item for item in list(args.get("items") or [])
            }
            if not 2 <= len(bound_drafts) <= 10:
                raise ValueError("批量定时发布需要当前任务生成的 2—10 篇草稿")
            args["items"] = list(bound_drafts.values())
        return args

    @staticmethod
    def _resolved_targets_from_current_run_outputs(
        *,
        tool: str,
        typed_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a short-lived binding from an earlier step in this run.

        Persistent cross-run targeting always comes from ConversationGoal's
        TargetBinding.  This fallback only bridges a create -> publish (or
        create -> revise) sequence within one execution, where the artifact
        has just been produced and is already present in ``typed_outputs``.
        """
        draft_consumers = {
            "creator.revise_draft",
            "publication.schedule",
            "publication.publish_now",
        }
        schedule_consumers = {
            "publication.update_schedule",
            "publication.cancel_schedule",
        }
        if tool in schedule_consumers:
            for item in reversed(typed_outputs):
                result = item.get("result")
                if not isinstance(result, dict):
                    continue
                action_id = str(
                    result.get("action_id") or result.get("actionId") or ""
                ).strip()
                if not action_id:
                    continue
                return {
                    "SCHEDULE": {
                        "target_type": "SCHEDULE",
                        "role": "SCHEDULE",
                        "target_id": action_id,
                        "schedule_id": action_id,
                        "artifact_id": item.get("artifact_id"),
                        "version": 1,
                        "confidence": 1.0,
                        "resolution_method": "TOOL_OUTPUT",
                    }
                }
            return {}
        if tool not in draft_consumers:
            return None
        for item in reversed(typed_outputs):
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            draft_id = result.get("draft_id")
            if not draft_id:
                continue
            artifact_type = str(item.get("artifact_type") or "")
            if artifact_type and artifact_type != "CONTENT_DRAFT":
                continue
            return {
                "CONTENT": {
                    "target_type": "DRAFT",
                    "role": "CONTENT",
                    "target_id": str(draft_id),
                    "artifact_id": item.get("artifact_id"),
                    "content_sha256": result.get("content_sha256")
                    or result.get("contentSha256"),
                    "version": 1,
                    "confidence": 1.0,
                    "resolution_method": "TOOL_OUTPUT",
                }
            }
        return {}

    @staticmethod
    def _compatibility_artifact_type(
        output: dict[str, Any],
        registry: ToolRegistry,
    ) -> str:
        """Type an output restored from a pre-Artifact-Contract checkpoint."""
        tool = output.get("tool")
        if tool:
            try:
                return registry.get(str(tool)).artifact_type
            except ValueError:
                pass
        result = output.get("result")
        if isinstance(result, dict) and result.get("draft_id") and (
            result.get("content_sha256") or result.get("contentSha256")
        ):
            return "CONTENT_DRAFT"
        return "TOOL_RESULT"

    @staticmethod
    def _reference_results(
        args: dict[str, Any], previous: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
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
            elif (
                item.get("tool") == "community.list_posts_by_users"
                and isinstance(result, dict)
            ):
                references.extend(
                    {
                        "id": str(entry.get("post_id") or entry.get("id") or ""),
                        "title": str(entry.get("title") or "社区帖子"),
                        "description": entry.get("description"),
                        "tags": list(entry.get("tags") or []),
                        "type": entry.get("type"),
                        "author_id": entry.get("author_id"),
                    }
                    for entry in list(result.get("posts") or [])
                    if isinstance(entry, dict)
                    and (entry.get("post_id") or entry.get("id"))
                )
            elif (
                item.get("tool") == "community.aggregate_post_topics"
                and isinstance(result, dict)
            ):
                references.append(
                    {
                        "id": "analytics:active-user-topics",
                        "title": "社区活跃用户发帖主题分析",
                        "description": "基于当前社区公开帖子聚合出的主题分布。",
                        "body_markdown": json.dumps(
                            {"topics": list(result.get("topics") or [])},
                            ensure_ascii=False,
                        ),
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
        lowered = text.lower()
        if (
            text.startswith("{{")
            and text.endswith("}}")
            and "draft" in lowered
        ):
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
# 对 kind="model" 大致做两件事：
# 分布式限流（若开了 Redis）
# rate_limiter.consume_model_call(user_id)：全站/单用户每分钟次数
# 本任务配额
# 检查有没有超时（deadline_at）
# model_calls + 1
# 若已到 max_model_calls（默认常 6）→ 抛错，不再调模型
# 同类还有：
# "tool"：扣工具调用次数
# "replan"：扣重规划次数
# 出现在 decide_execution / plan 前面，意思是：先记账，再允许这次模型调用。
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
            run.summary = "任务验收完成，正在汇总结果"
            run.updated_at = utc_now()
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

    async def _checkpoint_task_bag(
        self,
        *,
        run_id: str,
        primary_summary: str,
        follow_ups: list[str],
    ) -> None:
        """Persist remaining Task Bag prompts for serial follow-up Runs."""

        if not follow_ups:
            return
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                return
            checkpoint = dict(run.checkpoint or {})
            checkpoint["task_bag"] = {
                "primary": primary_summary,
                "remaining": follow_ups[:3],
                "total": 1 + len(follow_ups[:3]),
            }
            run.checkpoint = checkpoint
            run.summary = (
                f"本轮包含 {1 + len(follow_ups[:3])} 个独立任务，"
                f"先执行第 1 个，其余将排队串行继续"
            )
            await append_event(
                session,
                run_id,
                "TASK_BAG_QUEUED",
                {
                    "total": 1 + len(follow_ups[:3]),
                    "remaining": follow_ups[:3],
                },
            )

    async def _enqueue_task_bag_followups(self, run_id: str) -> None:
        """Create serial follow-up Runs for remaining Task Bag prompts."""

        async with self.database.sessions() as session, session.begin():
            parent = await session.get(Run, run_id)
            if parent is None:
                return
            bag = dict(dict(parent.checkpoint or {}).get("task_bag") or {})
            remaining = [
                str(item).strip()
                for item in list(bag.get("remaining") or [])
                if str(item).strip()
            ]
            if not remaining:
                return
            next_prompt = remaining[0]
            rest = remaining[1:]
            child = Run(
                conversation_id=parent.conversation_id,
                user_id=parent.user_id,
                tenant_id=parent.tenant_id,
                principal_role=parent.principal_role,
                prompt=next_prompt,
                context_post_id=parent.context_post_id,
                context_comment_id=parent.context_comment_id,
                client_timezone=parent.client_timezone,
                delegated_token=parent.delegated_token,
                status="QUEUED",
                max_model_calls=parent.max_model_calls,
                max_tool_calls=parent.max_tool_calls,
                max_replans=parent.max_replans,
                max_attempts=parent.max_attempts,
                runtime_identity=parent.runtime_identity,
                deadline_at=utc_now()
                + timedelta(seconds=self.settings.run_timeout_seconds),
                checkpoint={
                    "task_bag_parent_run_id": parent.id,
                    "task_bag": {
                        "primary": next_prompt,
                        "remaining": rest,
                        "total": int(bag.get("total") or (1 + len(remaining))),
                        "ordinal": int(bag.get("total") or 0)
                        - len(remaining)
                        + 1,
                    },
                },
            )
            # Parent completion already cleared delegated_token; re-encrypt is
            # unavailable here. Prefer copying only when still present; otherwise
            # leave None and let auth fail closed on write tools.
            if parent.delegated_token:
                child.delegated_token = parent.delegated_token
            session.add(child)
            await session.flush()
            session.add(
                Message(
                    conversation_id=parent.conversation_id,
                    role="user",
                    content=next_prompt,
                    parts=[
                        {
                            "kind": "task_bag_followup",
                            "parent_run_id": parent.id,
                            "ordinal": child.checkpoint.get("task_bag", {}).get(
                                "ordinal"
                            ),
                        }
                    ],
                    run_id=child.id,
                )
            )
            await append_event(
                session,
                child.id,
                "RUN_QUEUED",
                {
                    "status": "QUEUED",
                    "task_bag_parent_run_id": parent.id,
                    "prompt": next_prompt,
                },
            )
            await append_event(
                session,
                parent.id,
                "TASK_BAG_FOLLOWUP_ENQUEUED",
                {
                    "child_run_id": child.id,
                    "remaining": rest,
                },
            )

    async def _complete_run(
        self, run_id: str, final_response: str, outputs: list[dict[str, Any]]
    ) -> None:
        bag_followups = False
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id, with_for_update=True)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("Stale worker completion rejected")
            bag = dict(dict(run.checkpoint or {}).get("task_bag") or {})
            remaining = list(bag.get("remaining") or [])
            bag_followups = bool(remaining)
            # Keep token long enough to clone into the next Task Bag run.
            retained_token = run.delegated_token
            if bag_followups and retained_token:
                checkpoint = dict(run.checkpoint or {})
                checkpoint["task_bag_token_retained"] = True
                run.checkpoint = checkpoint
            else:
                retained_token = None
            response_text = final_response
            if remaining:
                response_text = (
                    f"{final_response.rstrip()}\n\n"
                    f"（同一条消息里还有 {len(remaining)} 个后续任务，"
                    "将自动排队继续执行。）"
                )
            run.status = "COMPLETED"
            run.summary = "任务已完成"
            run.completed_at = utc_now()
            run.final_response = response_text
            if not bag_followups:
                run.delegated_token = None
            run.lease_owner = None
            run.lease_expires_at = None
            run.version += 1
            run.updated_at = utc_now()
            delta = await session.scalar(
                select(IntentDeltaRecord)
                .where(
                    or_(
                        IntentDeltaRecord.run_id == run.id,
                        IntentDeltaRecord.id
                        == str(dict(run.checkpoint or {}).get("intent_delta_id") or ""),
                    )
                )
                .with_for_update()
            )
            if delta is not None:
                delta.status = "APPLIED"
                delta.updated_at = utc_now()
            session.add(
                Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=response_text,
                    parts=outputs,
                    run_id=run.id,
                )
            )
            final_artifact = await publish_final_artifact(
                session,
                run=run,
                final_response=response_text,
            )
            await append_event(
                session,
                run.id,
                "RUN_COMPLETED",
                {
                    "status": "COMPLETED",
                    "response": response_text,
                    "final_artifact_id": final_artifact.id,
                },
            )
        if bag_followups:
            try:
                await self._enqueue_task_bag_followups(run_id)
            finally:
                async with self.database.sessions() as session, session.begin():
                    run = await session.get(Run, run_id, with_for_update=True)
                    if run is not None:
                        run.delegated_token = None
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
            can_retry = (
                _is_transient_exception(error)
                and run.attempts < run.max_attempts
                and (run.deadline_at is None or run.deadline_at > utc_now())
            )
            error_text = _public_execution_error(error, retrying=can_retry)
            if _database_sqlstate(error) is not None:
                logger.warning(
                    "Run %s hit a retryable database concurrency conflict",
                    run_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
            retry_delay = (
                error.retry_after_seconds
                if isinstance(error, DistributedLimitExceeded)
                else min(60, 5 * (2 ** max(0, run.attempts - 1)))
            )
            run.status = "RETRYING" if can_retry else "FAILED"
            run.summary = "等待自动重试" if can_retry else "执行失败"
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
            if not can_retry:
                delta = await session.scalar(
                    select(IntentDeltaRecord)
                    .where(
                        or_(
                            IntentDeltaRecord.run_id == run.id,
                            IntentDeltaRecord.id
                            == str(dict(run.checkpoint or {}).get("intent_delta_id") or ""),
                        )
                    )
                    .with_for_update()
                )
                if delta is not None:
                    delta.status = "FAILED"
                    delta.updated_at = utc_now()
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
            # Include lease-expired RUNNING so Java-success / local-incomplete
            # rows can be reclaimed and reconciled (same idempotency key).
            action = await session.scalar(
                select(ScheduledAction)
                .where(
                    ScheduledAction.status.in_(
                        ["SCHEDULED", "RETRYING", "RUNNING"]
                    ),
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
            # Fresh SCHEDULED/RETRYING: normal claim. Expired RUNNING: reclaim.
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
        # Pre-publish re-check: lease/status/token must still authorize publish.
        gate = await self.schedule_repository.assert_publishable_for_worker(
            action_id=action_id,
            worker_id=self.worker_id,
        )
        if gate is None:
            logger.warning(
                "Skipping scheduled publish for %s: stale lease, cancelled, "
                "or invalid RUNNING state",
                action_id,
            )
            return
        async with self.database.sessions() as session:
            action = await session.get(ScheduledAction, action_id)
        if action is None:
            return
        try:
            if not action.capability_token:
                raise RuntimeError("定时发布缺少委托能力令牌")
            published = await execute_publish_now(
                community=self.community,
                registry=self.registry,
                request=PublishNowRequest(
                    draft_id=action.draft_id,
                    expected_content_sha256=str(
                        action.expected_content_sha256 or ""
                    ).lower(),
                    creator_id=action.user_id,
                    idempotency_key=action.idempotency_key,
                    capability_token=self.token_vault.decrypt(
                        action.capability_token
                    ),
                    trace_id=action.run_id,
                    source="SCHEDULER",
                    run_id=action.run_id,
                ),
            )
            result = published.output
            async with self.database.sessions() as session, session.begin():
                current = await session.get(
                    ScheduledAction, action_id, with_for_update=True
                )
                if current is None or current.lease_owner != self.worker_id:
                    return
                current.status = "COMPLETED"
                current.result = {
                    **result,
                    "source": "SCHEDULER",
                    "idempotency_key_hash": published.idempotency_key_hash,
                }
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
                # Unknown / transient: RETRYING with same idempotency key.
                # Do not mint a new key. Permanent business failures → FAILED.
                retryable = _is_transient_exception(exc) or isinstance(
                    exc, UnknownSideEffectError
                )
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


def _resolve_schedule_run_at(
    arguments: dict[str, Any],
    *,
    now: datetime | None = None,
) -> datetime:
    current = (now or utc_now()).astimezone(timezone.utc)
    delay_seconds = arguments.get("delay_seconds")
    if delay_seconds is not None:
        return current + timedelta(seconds=int(delay_seconds))
    return _parse_run_at(arguments.get("run_at"))


async def append_retry_delay(attempt: int) -> None:
    await asyncio.sleep(min(4.0, 0.5 * (2 ** max(0, attempt - 1))))


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_transient_exception(error: BaseException) -> bool:
    if isinstance(error, UnknownSideEffectError):
        return False
    if isinstance(error, ToolRuntimeError):
        return error.status == ToolInvocationStatus.RETRYABLE_FAILURE
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
    # deadlock_detected / serialization_failure: transaction rollback removes
    # partial state, so the durable Run can be replayed safely.
    if _database_sqlstate(error) in {"40P01", "40001"}:
        return True
    return False


def _database_sqlstate(error: BaseException) -> str | None:
    if not isinstance(error, DBAPIError):
        return None
    original = error.orig
    return (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or getattr(getattr(original, "__cause__", None), "sqlstate", None)
    )


def _public_execution_error(error: BaseException, *, retrying: bool) -> str:
    if isinstance(error, PermanentToolError):
        return str(error)[:4_000]
    sqlstate = _database_sqlstate(error)
    if sqlstate in {"40P01", "40001"}:
        if retrying:
            return "数据库并发冲突，任务将从最近检查点自动重试。"
        return (
            "数据库并发冲突的自动重试次数已耗尽，请点击重试继续；"
            "已经完成的步骤不会重复执行。"
        )
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 403:
        return (
            "没有权限读取或修改该草稿。若发布时间已被提前到最近几分钟，"
            "帖子可能已经自动发布，请新开一篇或换一篇仍处于草稿/定时中的帖子。"
        )
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 401:
        return (
            "社区工具鉴权失败（能力令牌无效、过期或使用次数已耗尽）。"
            "请重试；若刚做了“先搜索再创作”，多半是搜索扩展查询耗尽了单次令牌，"
            "服务端已修复，直接重试即可。"
        )
    return str(error)[:4_000]


def _effect_resource_id(tool: str, arguments: dict[str, Any]) -> str | None:
    if tool in {
        "publication.schedule",
        "publication.publish_now",
    }:
        return f"post:{arguments.get('draft_id')}"
    if tool == "publication.schedule_batch":
        ids = ",".join(
            str(item.get("draft_id"))
            for item in list(arguments.get("items") or [])
        )
        return f"posts:{ids}"
    if tool in {
        "publication.get_schedule",
        "publication.update_schedule",
        "publication.cancel_schedule",
    }:
        return f"schedule:{arguments.get('action_id')}"
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


def _policy_resource(
    tool: str,
    arguments: dict[str, Any],
    definition: ToolDefinition,
) -> dict[str, Any]:
    if tool.startswith(("community.", "publication.")):
        authority = "JAVA"
    elif tool.startswith("creator."):
        authority = "CREATOR_AGENT"
    else:
        authority = "MCP"
    return {
        "authority": authority,
        "resource_id": _effect_resource_id(tool, arguments),
        "side_effecting": definition.side_effecting,
        "risk": definition.risk.value,
        "open_world": tool.startswith("mcp.")
        and not tool.startswith("mcp.creator."),
    }


def _remote_operation_id(output: dict[str, Any]) -> str | None:
    for key in ("action_id", "post_id", "draft_id", "id", "task_id"):
        value = output.get(key)
        if value is not None:
            return str(value)
    return None
