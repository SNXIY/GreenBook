"""RuntimeAgentService — full Runtime pipeline for one Agent turn.

Phase 5.1: CREATE_CONTENT only.  Properly wired:
  TaskIntent → Mapper → Orchestrator → Validator → Worker → Result

All Phase 4.x modules connected: ToolRuntime, Ledger, Worker+Trace,
ArtifactStore, TraceCollector.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from greenbook_assistant_core.artifact.store import ArtifactStore
from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.capability.mapper import CapabilityMapper
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.argument_binder import ArgumentBinder
from greenbook_assistant_core.execution.capability_executor import CapabilityExecutor
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.execution_queue import (
    ExecutionQueueMessage,
    ExecutionQueueProtocol,
)
from greenbook_assistant_core.execution.models import ExecutionStatus, StepStatus
from greenbook_assistant_core.execution.operation_tracking import ExternalOperationTracker
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_assistant_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_assistant_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_assistant_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_assistant_core.observability.collector import TraceCollector
from greenbook_assistant_core.observability.context import TraceContext
from greenbook_assistant_core.observability.trace import AgentTrace
from greenbook_assistant_core.orchestration.context import PlanningContext
from greenbook_assistant_core.orchestration.models import TaskPlan
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.models import ExecutablePlan
from greenbook_assistant_core.planning.validation import PlanValidator
from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.models import TaskIntent

from ..models.runtime_context import RuntimeContext, TargetContext, TaskContext
from ..models.runtime_result import RuntimeResult

logger = logging.getLogger(__name__)

RuntimeCompletionCallback = Callable[
    [RuntimeResult], Awaitable[None] | None
]


class RuntimeAgentService:
    """Execute a turn through the new Runtime pipeline.

    Phase 6.1: execute() handles decomposition + routing;
    _execute_single() carries the stable single-task pipeline.
    Phase 6.5: HumanInteractionManager for clarification pauses.
    """

    def __init__(
        self,
        *,
        repository: ExecutionRepository | None = None,
        event_store: ExecutionEventStore | None = None,
        checkpoint_store: Any | None = None,
        operation_tracker: ExternalOperationTracker | None = None,
        execution_queue: ExecutionQueueProtocol | None = None,
        dispatch_mode: str = "direct",
    ) -> None:
        self._execution_repository = repository
        self._execution_event_store = event_store
        self._execution_checkpoint_store = checkpoint_store
        self._operation_tracker = operation_tracker
        self._execution_queue = execution_queue
        self._dispatch_mode = dispatch_mode.strip().lower()
        self._registry = CapabilityRegistry()
        self._mapper = CapabilityMapper(self._registry)
        self._orchestrator = TaskOrchestrator(self._registry)
        self._validator = PlanValidator(self._registry)
        # Phase 6.5: Human-in-the-loop
        from greenbook_assistant_core.human.manager import HumanInteractionManager
        self._human_mgr = HumanInteractionManager()
        # Phase 6.6: Agent Memory
        from greenbook_assistant_core.agent_memory.manager import MemoryManager
        self._memory_mgr = MemoryManager()
        self._paused_contexts: dict[str, tuple[RuntimeContext, str]] = {}
        # Detached executions are owned by the Runtime service, not by an
        # HTTP request.  This keeps long Creator calls alive after the 202
        # response and gives the API a read-only result registry for the
        # conversation projection.
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_results: dict[str, RuntimeResult] = {}
        # interaction_id → (ctx, checkpoint_name)

    # ── Phase 6.1 entry: decomposition + routing ────────────────

    async def execute(
        self,
        ctx: RuntimeContext,
        *,
        detach: bool = False,
        completion_callback: RuntimeCompletionCallback | None = None,
    ) -> RuntimeResult:
        """Execute the resolved TaskContext through Planner and Runtime."""
        # Single task — use decomposer intent if original is minimal/empty
        task_context = ctx.task_context
        if (
            task_context is None
            or not task_context.task_id
            or task_context.task_intent is None
        ):
            return self._fail(
                ctx,
                "TASK_CONTEXT_REQUIRED",
                "A resolved TaskContext with task_id is required before Runtime execution.",
            )

        # Preserve the existing Planner/ArgumentBinder interface while making
        # TaskContext the only source for business target data.
        ctx.task_id = task_context.task_id
        # Keep the compiled TaskContext immutable.  Legacy interaction and
        # argument-binding paths may mutate their working intent, so Runtime
        # receives a detached compatibility projection only.
        ctx.task_intent = (
            task_context.task_intent.model_copy(deep=True)
            if hasattr(task_context.task_intent, "model_copy")
            else task_context.task_intent
        )
        ctx.active_artifact_id = task_context.active_artifact_id
        return await self._execute_single(
            ctx,
            detach=detach,
            completion_callback=completion_callback,
        )

    def background_result(self, run_id: str) -> RuntimeResult | None:
        """Return a completed detached result, if one is available."""
        return self._background_results.get(run_id)

    async def execute_queued(
        self,
        message: ExecutionQueueMessage,
        *,
        mcp: Any,
        llm: Any = None,
        model: str = "",
        auth: Any = None,
    ) -> RuntimeResult:
        """Execute an already-created PlanExecution from a queue envelope.

        The queue worker supplies process-local MCP/LLM/auth dependencies. The
        queue message supplies only a serializable dispatch snapshot and never
        a bearer token. This method does not create a second Execution.
        """

        payload = message.payload
        try:
            task_intent_data = dict(payload["task_intent"])
            if payload.get("intent_spec") is not None:
                task_intent_data["intent_spec"] = payload["intent_spec"]
            task_intent = TaskIntent.model_validate(task_intent_data)
            executable = ExecutablePlan.model_validate(payload["executable_plan"])
            task_context_data = payload.get("task_context") or {}
            target_data = task_context_data.get("target") or None
            target = (
                TargetContext(**target_data)
                if isinstance(target_data, dict)
                else None
            )
            task_context = TaskContext(
                task_id=str(task_context_data.get("task_id") or payload["task_id"]),
                goal=str(task_context_data.get("goal") or ""),
                task_intent=task_intent,
                target=target,
                constraints=tuple(task_context_data.get("constraints") or ()),
                active_artifact_id=task_context_data.get("active_artifact_id"),
                artifact_refs=tuple(task_context_data.get("artifact_refs") or ()),
            )
            session_payload = payload.get("session") or {}
            session = SessionContext.model_validate({
                "conversation_id": session_payload.get(
                    "conversation_id", payload.get("conversation_id", "")
                ),
                "user_id": session_payload.get("user_id", payload.get("user_id", "")),
                "tenant_id": session_payload.get(
                    "tenant_id", payload.get("tenant_id", "")
                ),
                **{
                    key: value
                    for key, value in session_payload.items()
                    if key not in {"conversation_id", "user_id", "tenant_id"}
                },
            })
            ctx = RuntimeContext(
                conversation_id=str(payload.get("conversation_id", "")),
                run_id=str(payload.get("run_id", "")),
                trace_id=str(payload.get("trace_id", message.trace_id)),
                task_id=str(payload["task_id"]),
                execution_id=message.execution_id,
                task_context=task_context,
                user_id=str(payload.get("user_id", "")),
                tenant_id=str(payload.get("tenant_id", "")),
                timezone=str(payload.get("timezone", "Asia/Shanghai")),
                user_message=str(payload.get("user_message", "")),
                conversation_history=[
                    dict(item) for item in (payload.get("conversation_history") or ())
                ],
                task_intent=task_intent,
                session=session,
                active_artifact_id=payload.get("active_artifact_id"),
                active_draft_id=payload.get("active_draft_id"),
                active_schedule_id=payload.get("active_schedule_id"),
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
            )
        except Exception as exc:
            return self._fail(
                RuntimeContext(
                    run_id=str(payload.get("run_id", "")),
                    trace_id=str(payload.get("trace_id", message.trace_id)),
                ),
                "EXECUTION_DISPATCH_INVALID",
                f"Queued execution dispatch payload is invalid: {exc}",
            )

        return await self._execute_single(
            ctx,
            existing_execution_id=message.execution_id,
            executable_override=executable,
        )

    async def _execute_single(
        self,
        ctx: RuntimeContext,
        *,
        detach: bool = False,
        completion_callback: RuntimeCompletionCallback | None = None,
        existing_execution_id: str | None = None,
        executable_override: ExecutablePlan | None = None,
    ) -> RuntimeResult:
        t0 = time.monotonic()
        task_context = ctx.task_context
        if (
            task_context is None
            or not task_context.task_id
            or task_context.task_intent is None
        ):
            return self._fail(
                ctx,
                "TASK_CONTEXT_REQUIRED",
                "A resolved TaskContext with task_id is required before Runtime execution.",
            )
        task_id = task_context.task_id

        # Phase 6.6: Recall agent memory
        self._recall_memories(ctx)

        # ── 1. Capabilities ─────────────────────────────────
        gc = getattr(ctx.task_intent, "goal_category", "CREATE_CONTENT")
        if not self._mapper.capabilities_for_goal(gc):
            return self._fail(ctx, "NO_CAPABILITY", f"No capabilities for {gc}")

        # ── 1.3 Task Reference Resolution (Phase 6.2.2-B) ────
        # ── 1.5 Resource Resolution (Phase 5.6) ──────────────
        # ── 2. Plan ─────────────────────────────────────────
        reqs = getattr(ctx.task_intent, "requirements", []) or [{"type": "CREATE"}]
        intent_spec = _intent_spec_from_task_intent(ctx.task_intent)
        planning_context: PlanningContext | None = None
        if isinstance(ctx.task_intent, TaskIntent):
            planning_context = self._orchestrator.build_planning_context(
                ctx.task_intent,
                intent_spec,
            )
        if executable_override is None:
            plan = self._orchestrator.generate_plan(
                task_id=task_id, goal_category=gc, requirements=reqs,
                planning_context=planning_context,
            )
        else:
            plan = TaskPlan(
                plan_id=executable_override.plan_id,
                task_id=task_id,
                template_name=executable_override.template_name,
                steps=[step.model_copy(deep=True) for step in executable_override.steps],
            )

        # Bind known arguments before validation/execution.  Runtime execution
        # still binds at the last boundary so upstream artifact IDs (for
        # example the generated draft_id) can be merged into later steps.
        argument_binder = ArgumentBinder(
            _tool_schemas_from_mcp(ctx.mcp),
            registry=self._registry,
            timezone=ctx.timezone,
        )
        argument_binder.bind_plan(
            plan,
            planning_context,
            intent_spec,
            user_message=ctx.user_message,
            timezone=ctx.timezone,
            active_draft_id=ctx.active_draft_id,
            active_schedule_id=ctx.active_schedule_id,
        )

        # ── 3. Validate ─────────────────────────────────────
        executable = executable_override or self._validator.validate(plan)
        if not executable.is_valid:
            errors = "; ".join(e.message for e in executable.errors)
            return self._fail(ctx, "PLAN_INVALID", errors)

        # ── 4. Observability ────────────────────────────────
        collector = TraceCollector()
        trace_context = TraceContext(
            conversation_id=ctx.conversation_id,
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            task_id=task_id,
        )
        trace = AgentTrace(
            collector,
            trace_context=trace_context,
        )
        trace.task_created(goal=ctx.user_message, category=gc)
        trace.plan_created(template=plan.template_name, step_count=len(plan.steps))

        # ── 5. ToolRuntime (idempotency + ledger + trace) ───
        ledger = ToolExecutionLedger()
        mcp, session = ctx.mcp, ctx.session
        # Keep tool result payloads at the service/result boundary so the
        # presentation layer can show a draft title/content and schedule
        # time.  This is deliberately local data; it is not added to
        # PlanExecution or the Worker state model.
        tool_results: dict[str, dict[str, Any]] = {}
        worker_ref: dict[str, Any] = {}

        def evidence_payload(value: Any) -> dict[str, Any] | None:
            if value is None:
                return None
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump(mode="json")
                return dict(dumped) if isinstance(dumped, dict) else None
            if isinstance(value, dict):
                return dict(value)
            return None

        async def raw_handler(tool_name: str, tool_args: dict) -> dict:
            tool_call_id = str(uuid.uuid4())
            call_kwargs = {
                "auth": ctx.auth,
                "session": session,
                "trace_id": ctx.trace_id,
                "agent_run_id": ctx.run_id,
                "tool_call_id": tool_call_id,
            }
            if detach:
                call_kwargs["async_mode"] = True
            # Do not remove async_mode on a TypeError.  That compatibility
            # fallback silently re-entered the synchronous Creator wait and
            # made a detached HTTP request block or time out.  A host that
            # has not implemented the contract must fail explicitly.
            raw_result = await mcp.execute_tool(
                tool_name,
                **call_kwargs,
                **tool_args,
            )
            if isinstance(raw_result, dict):
                enriched = dict(raw_result)
                evidence = evidence_payload(enriched.get("evidence")) or {}
                evidence["tool_call_id"] = tool_call_id
                enriched["evidence"] = evidence
                return enriched
            return raw_result

        async def on_async_complete(
            inv_ctx: ToolInvocationContext,
            inv_result: Any,
        ) -> None:
            """Resume the existing Worker after a long tool callback."""
            tool_results[inv_ctx.step_id] = {
                "tool_name": inv_result.tool_name,
                "capability": inv_ctx.capability,
                "ok": inv_result.ok,
                "data": dict(inv_result.data or {}),
                "error_code": inv_result.error_code,
                "error_message": inv_result.error_message,
                "pending": False,
                "async_task_id": inv_result.async_task_id,
                "evidence": evidence_payload(inv_result.evidence),
            }
            current_worker = worker_ref.get("worker")
            if current_worker is None:
                return
            try:
                continuation_outcome = await current_worker.run(ctx.execution_id)
                if continuation_outcome == RunOutcome.WAITING_ASYNC:
                    # A second async handle is still outstanding.  Its own
                    # completion callback will resume this same Worker.
                    return
                if continuation_outcome == RunOutcome.WAITING_APPROVAL:
                    completed_result = self._pause_for_approval(
                        ctx, current_worker, ctx.execution_id,
                    )
                else:
                    completed_result = self._finish_execution(
                        ctx=ctx,
                        worker=current_worker,
                        execution_id=ctx.execution_id,
                        outcome=continuation_outcome,
                        task_id=task_id,
                        t0=t0,
                        trace=trace,
                        collector=collector,
                        template_name=plan.template_name,
                        tool_results=tool_results,
                    )
            except Exception as exc:
                logger.exception(
                    "Async Runtime continuation failed execution_id=%s",
                    ctx.execution_id,
                )
                self._fail_detached_execution(current_worker, ctx.execution_id, exc)
                completed_result = RuntimeResult(
                    success=False,
                    status="FAILED",
                    run_id=ctx.run_id,
                    task_id=task_id,
                    trace_id=ctx.trace_id,
                    execution_id=ctx.execution_id,
                    execution_path="runtime",
                    error_code="RUNTIME_ERROR",
                    error_message="Runtime execution failed",
                    content="Runtime execution failed",
                    started_execution=True,
                )
            self._background_results[ctx.run_id] = completed_result
            if completion_callback is not None:
                try:
                    callback_result = completion_callback(completed_result)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                except Exception:
                    # Projection persistence must not turn a completed
                    # Runtime execution into an unobserved task exception.
                    logger.exception(
                        "Failed to publish async Runtime result run_id=%s",
                        ctx.run_id,
                    )

        tool_runtime = ToolRuntime(
            raw_handler,
            ledger=ledger,
            trace=trace,
            on_async_complete=on_async_complete,
        )

        # ── 6. CapabilityExecutor (via invoke_fn → ToolRuntime) ──
        async def invoke_fn(inv_ctx: ToolInvocationContext) -> dict:
            inv_result = await tool_runtime.invoke(inv_ctx)
            tool_results[inv_ctx.step_id] = {
                "tool_name": inv_result.tool_name,
                "capability": inv_ctx.capability,
                "ok": inv_result.ok,
                "data": dict(inv_result.data or {}),
                "error_code": inv_result.error_code,
                "error_message": inv_result.error_message,
                "pending": inv_result.pending,
                "async_task_id": inv_result.async_task_id,
                "evidence": evidence_payload(inv_result.evidence),
            }
            return {
                "ok": inv_result.ok,
                "code": inv_result.error_code,
                "data": inv_result.data,
                "user_message": inv_result.error_message,
                "retryable": inv_result.retryable,
                "request_sent": inv_result.request_sent,
                "pending": inv_result.pending,
                "async_task_id": inv_result.async_task_id,
                "evidence": evidence_payload(inv_result.evidence),
            }

        executor = CapabilityExecutor(
            self._registry, invoke_fn=invoke_fn,
            task_id=task_id, execution_id="",
            argument_binder=argument_binder,
            planning_context=planning_context,
            intent_spec=intent_spec,
            user_message=ctx.user_message,
            timezone=ctx.timezone,
            active_draft_id=ctx.active_draft_id,
            active_schedule_id=ctx.active_schedule_id,
        )

        # ── 7. Worker (with trace) ──────────────────────────
        worker = ExecutionWorker(
            executor,
            repository=self._execution_repository,
            trace=trace,
            event_store=self._execution_event_store,
            checkpoint_store=self._execution_checkpoint_store,
            operation_tracker=self._operation_tracker,
        )
        if existing_execution_id:
            execution = worker._repo.find_by_id(existing_execution_id)
            if execution is None:
                return self._fail(
                    ctx,
                    "EXECUTION_NOT_FOUND",
                    f"Queued execution {existing_execution_id} was not found.",
                )
        else:
            execution = worker.init_from_plan(executable, task_id=task_id)
        worker_ref["worker"] = worker

        # ExecutionStateManager intentionally owns lifecycle transitions and
        # does not know tool arguments.  Persist the already-bound plan
        # constraints in each step checkpoint so Worker can carry them into
        # its PlanStep without changing that protected state machine.
        if not existing_execution_id:
            _seed_step_constraints(execution, plan, getattr(worker, "_repo", None))

        # Backfill execution_id for trace + executor
        ctx.execution_id = execution.execution_id
        trace.execution_id = execution.execution_id
        executor._execution_id = execution.execution_id
        trace_context = trace_context.for_execution(execution.execution_id)
        trace.bind_context(trace_context)
        executor.bind_trace_context(trace_context)
        worker.bind_trace_context(trace_context)
        if self._dispatch_mode == "queue" and not existing_execution_id:
            if self._execution_queue is None:
                return self._fail(
                    ctx,
                    "EXECUTION_QUEUE_UNAVAILABLE",
                    "Runtime queue dispatch is enabled but no ExecutionQueue is configured.",
                )
            self._execution_queue.enqueue(
                execution.execution_id,
                trace_id=ctx.trace_id,
                payload=_execution_dispatch_payload(
                    ctx=ctx,
                    executable=executable,
                    intent_spec=intent_spec,
                ),
            )
            initial_steps = [
                {
                    "step_id": step.step_id,
                    "capability": step.capability,
                    "status": step.status.value,
                    "retry_count": step.retry_count,
                    "error_code": step.error_code,
                    "error_message": step.error_message,
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                }
                for step in execution.steps
            ]
            return RuntimeResult(
                success=False,
                status="QUEUED",
                run_id=ctx.run_id,
                task_id=task_id,
                plan_id=executable.plan_id,
                execution_id=execution.execution_id,
                content="",
                summary=ctx.user_message[:200],
                started_execution=False,
                execution_path="runtime",
                steps=initial_steps,
                trace_id=ctx.trace_id,
            )

        trace.execution_started()

        # ── 8. Execute ──────────────────────────────────────
        if detach:
            # Claim the execution before returning so the API can expose a
            # RUNNING task immediately.  ExecutionWorker still owns every
            # step transition; this only moves its existing run loop to a
            # service-owned asyncio task.
            worker._state.start_execution(execution.execution_id)

            async def run_detached() -> None:
                try:
                    background_outcome = await worker.run(execution.execution_id)
                    if (
                        background_outcome == RunOutcome.WAITING_ASYNC
                    ):
                        # A pending AsyncTaskHandle will resume the same
                        # Worker from ToolRuntime's completion callback.
                        return
                    if background_outcome == RunOutcome.WAITING_APPROVAL:
                        background_result = self._pause_for_approval(
                            ctx, worker, execution.execution_id,
                        )
                    else:
                        background_result = self._finish_execution(
                            ctx=ctx,
                            worker=worker,
                            execution_id=execution.execution_id,
                            outcome=background_outcome,
                            task_id=task_id,
                            t0=t0,
                            trace=trace,
                            collector=collector,
                            template_name=plan.template_name,
                            tool_results=tool_results,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "Detached Runtime execution failed execution_id=%s",
                        execution.execution_id,
                    )
                    self._fail_detached_execution(worker, execution.execution_id, exc)
                    background_result = RuntimeResult(
                        success=False,
                        status="FAILED",
                        run_id=ctx.run_id,
                        task_id=task_id,
                        trace_id=ctx.trace_id,
                        execution_id=execution.execution_id,
                        execution_path="runtime",
                        error_code="RUNTIME_ERROR",
                        error_message="Runtime execution failed",
                        content="Runtime execution failed",
                        started_execution=True,
                    )

                self._background_results[ctx.run_id] = background_result
                if completion_callback is not None:
                    try:
                        callback_result = completion_callback(background_result)
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    except Exception:
                        logger.exception(
                            "Failed to publish detached Runtime result run_id=%s",
                            ctx.run_id,
                        )

            background_task = asyncio.create_task(
                run_detached(),
                name=f"runtime-execution:{execution.execution_id}",
            )
            self._background_tasks[execution.execution_id] = background_task

            def _forget_task(_task: asyncio.Task[None]) -> None:
                self._background_tasks.pop(execution.execution_id, None)

            background_task.add_done_callback(_forget_task)
            initial_steps = [
                {
                    "step_id": step.step_id,
                    "capability": step.capability,
                    "status": step.status.value,
                    "retry_count": step.retry_count,
                    "error_code": step.error_code,
                    "error_message": step.error_message,
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                }
                for step in execution.steps
            ]
            return RuntimeResult(
                success=False,
                status="RUNNING",
                run_id=ctx.run_id,
                task_id=task_id,
                execution_id=execution.execution_id,
                content="",
                summary=ctx.user_message[:200],
                started_execution=True,
                execution_path="runtime",
                steps=initial_steps,
                trace_id=ctx.trace_id,
            )

        outcome = await worker.run(execution.execution_id)

        # ── 8.5 Approval → HumanInteraction (Phase 6.5) ──────
        if outcome == RunOutcome.WAITING_APPROVAL:
            return self._pause_for_approval(ctx, worker, execution.execution_id)

        # ── 9. Artifacts via ArtifactStore ──────────────────
        return self._finish_execution(
            ctx=ctx, worker=worker, execution_id=execution.execution_id,
            outcome=outcome, task_id=task_id, t0=t0,
            trace=trace, collector=collector,
            template_name=plan.template_name,
            tool_results=tool_results,
        )

    @staticmethod
    @staticmethod
    def _clarification_result(
        ctx: RuntimeContext, resolution: Any,
    ) -> RuntimeResult:
        """Return a result asking the user to clarify ambiguous targets."""
        # Handle both ResourceResolutionResult and ReferenceResolution
        try:
            candidates_str = ", ".join(
                f"{t.resource_type.value}@{t.resource_id or '?'}"
                for t in resolution.targets if t.is_ambiguous
            )
        except (AttributeError, TypeError):
            candidates_str = ", ".join(
                f"{getattr(t, 'goal', '?')}"
                for t in (resolution.targets or [])
            )
        return RuntimeResult(
            success=False, status="WAITING_APPROVAL",
            run_id=ctx.run_id, trace_id=ctx.trace_id,
            content=f"找到多个匹配的资源，请明确指定要操作的目标。候选: {candidates_str}",
            error_code="AMBIGUOUS_TARGET",
            error_message="Multiple resources match the request",
            execution_path="runtime",
            fallback_allowed=True,
        )

    def _pause_for_approval(
        self, ctx: RuntimeContext, worker: Any, execution_id: str,
    ) -> RuntimeResult:
        """Pause execution for user approval via HumanInteractionManager."""
        from greenbook_assistant_core.human.models import InteractionType
        req = self._human_mgr.pause(
            execution_id=execution_id, type=InteractionType.APPROVAL,
            question="此操作需要您的确认。是否继续？",
            options=[
                {"value": "ACCEPT", "label": "确认"},
                {"value": "REJECT", "label": "取消"},
            ],
        )
        # Store enough context to resume
        self._paused_contexts[req.interaction_id] = (
            ctx, "approval", worker, execution_id,
        )
        return RuntimeResult(
            success=False, status="WAITING_HUMAN",
            run_id=ctx.run_id, trace_id=ctx.trace_id,
            execution_id=execution_id,
            content=f"需要您的确认才能继续。Interaction: {req.interaction_id}",
            error_code="WAITING_HUMAN",
            execution_path="runtime",
            approval_id=req.interaction_id,
            approval_data={
                "approval_id": req.interaction_id,
                "execution_id": execution_id,
                "operation": "RUNTIME_APPROVAL",
                "description": "Runtime execution requires approval",
                "payload": {"interaction_id": req.interaction_id},
            },
            partial_results={"interaction_id": req.interaction_id},
        )

    def _pause_for_input(
        self, ctx: RuntimeContext, question: str, options: list[dict] | None = None,
    ) -> RuntimeResult:
        """Pause execution to request free-form user input."""
        from greenbook_assistant_core.human.models import InteractionType
        req = self._human_mgr.pause(
            execution_id=ctx.run_id, type=InteractionType.INPUT,
            question=question, options=options or [],
        )
        self._paused_contexts[req.interaction_id] = (ctx, "input")
        return RuntimeResult(
            success=False, status="WAITING_HUMAN",
            run_id=ctx.run_id, trace_id=ctx.trace_id,
            content=f"需要您的输入。Interaction: {req.interaction_id}",
            error_code="WAITING_HUMAN",
            execution_path="runtime",
            partial_results={"interaction_id": req.interaction_id},
        )

    def _pause_for_clarification(
        self, ctx: RuntimeContext, resolution: Any, checkpoint: str,
    ) -> RuntimeResult:
        """Pause execution for user clarification via HumanInteractionManager."""
        from greenbook_assistant_core.human.models import InteractionType
        options = []
        for t in (resolution.targets or []):
            label = getattr(t, "goal", "") or getattr(t, "match_reason", "")
            rid = getattr(t, "resource_id", None) or getattr(t, "task_id", "")
            options.append({"value": rid, "label": f"{label} ({rid})"})

        req = self._human_mgr.pause(
            execution_id=ctx.run_id, type=InteractionType.CLARIFICATION,
            question="找到多个匹配，请选择：",
            options=options,
            context={"checkpoint": checkpoint},
        )
        self._paused_contexts[req.interaction_id] = (ctx, checkpoint)
        return RuntimeResult(
            success=False, status="WAITING_HUMAN",
            run_id=ctx.run_id, trace_id=ctx.trace_id,
            content=f"需要您的选择才能继续。Interaction: {req.interaction_id}",
            error_code="WAITING_HUMAN",
            execution_path="runtime",
            partial_results={"interaction_id": req.interaction_id},
        )

    async def resume_human_interaction(
        self, interaction_id: str, selected_value: str,
        *,
        content: str = "",
        decision: str = "",
    ) -> RuntimeResult:
        """Resume execution after user response.

        For CLARIFICATION: pass selected_value.
        For APPROVAL: pass decision="ACCEPT" or "REJECT".
        For INPUT: pass content (free-text).
        """
        from greenbook_assistant_core.human.models import (
            HumanInteractionResponse,
        )
        resp = HumanInteractionResponse(
            interaction_id=interaction_id,
            decision=decision or ("SELECT" if selected_value else "INPUT"),
            selected_value=selected_value,
            content=content,
        )
        request = self._human_mgr.resume(interaction_id, resp)
        if request is None:
            return RuntimeResult(
                success=False, status="FAILED",
                error_code="INTERACTION_EXPIRED",
                error_message="Interaction not found or expired",
                execution_path="runtime",
            )

        entry = self._paused_contexts.pop(interaction_id, None)
        if entry is None:
            return RuntimeResult(
                success=False, status="FAILED",
                error_code="NO_PAUSED_CONTEXT",
                error_message="Paused context not found",
                execution_path="runtime",
            )

        # Approval checkpoint — different tuple format
        if len(entry) == 4:
            ctx, checkpoint, worker, execution_id = entry
            if resp.decision == "REJECT":
                from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
                state = ExecutionStateManager(worker._repo)
                state.cancel_execution(execution_id)
                return RuntimeResult(
                    success=False, status="CANCELLED",
                    run_id=ctx.run_id, trace_id=ctx.trace_id,
                    execution_id=execution_id,
                    content="操作已取消。",
                    execution_path="runtime",
                )
            # ACCEPT → resume worker
            import time as _time

            from greenbook_assistant_core.execution.worker import RunOutcome
            outcome = await worker.resume_after_approval(execution_id)
            if outcome == RunOutcome.COMPLETED:
                return self._finish_execution(
                    ctx=ctx, worker=worker, execution_id=execution_id,
                    outcome=outcome, task_id=ctx.task_id or "",
                    t0=_time.monotonic(),
                    trace=None, collector=None,
                )
            return RuntimeResult(
                success=False, status="FAILED",
                run_id=ctx.run_id, trace_id=ctx.trace_id,
                execution_id=execution_id,
                content="审批后执行失败",
                execution_path="runtime",
            )

        # Clarification / Input checkpoint
        ctx, checkpoint = entry
        if checkpoint == "input":
            # Inject user's free-text input into context
            if resp.content:
                ctx.user_message = (
                    f"{ctx.user_message}\n[用户补充]: {resp.content}"
                )
                # Also inject into constraints for tool args
                if ctx.task_intent:
                    existing = getattr(ctx.task_intent, "constraints", []) or []
                    existing.append({"type": "USER_INPUT", "value": resp.content})
                    ctx.task_intent.constraints = existing
            return await self._execute_single(ctx)

        if selected_value:
            ctx.task_id = selected_value
            if ctx.task_intent:
                ctx.task_intent.target_task_id = selected_value
                ctx.task_intent.target_task_hint = None

        return await self._execute_single(ctx)

    def _finish_execution(
        self, *, ctx: RuntimeContext, worker: Any, execution_id: str,
        outcome: Any, task_id: str, t0: float,
        trace: Any, collector: Any,
        template_name: str = "",
        tool_results: dict[str, dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        """Collect artifacts + build result after Worker completes."""
        artifact_store = ArtifactStore()
        final = worker._repo.find_by_id(execution_id)

        if final is not None:
            for step_ex in final.steps:
                if step_ex.status != StepStatus.COMPLETED:
                    continue
                if step_ex.output_artifact is None:
                    continue
                art = artifact_store.create_from_result(
                    type("_R", (), {
                        "ok": True,
                        "artifact": step_ex.output_artifact,
                        "tool_name": (tool_results or {}).get(step_ex.step_id, {}).get(
                            "tool_name", step_ex.capability
                        ),
                        "capability": step_ex.capability,
                        "tool_result": {
                            "data": (tool_results or {}).get(step_ex.step_id, {}).get(
                                "data", {}
                            ),
                        },
                    })(),
                    task_id=task_id,
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                )
                if art is not None and trace is not None:
                    trace.artifact_created(step_ex, art)

        if outcome == RunOutcome.COMPLETED:
            if trace is not None:
                trace.execution_completed()
        else:
            if trace is not None:
                trace.execution_failed(str(outcome))

        elapsed = (time.monotonic() - t0) * 1000.0
        draft_id: str | None = None
        schedule_id: str | None = None
        for art in artifact_store.find_by_execution(execution_id):
            if art.artifact_type == "DRAFT" and art.resource_id:
                draft_id = art.resource_id
            if art.artifact_type == "SCHEDULE" and art.resource_id:
                schedule_id = art.resource_id

        failed_steps = [
            step for step in (final.steps if final else [])
            if step.status in (StepStatus.FAILED, StepStatus.FAILED_RETRYABLE)
        ]
        failed_step = failed_steps[0] if failed_steps else None
        execution_failed = outcome != RunOutcome.COMPLETED or bool(failed_steps)
        failure_code = (
            failed_step.error_code
            if failed_step and failed_step.error_code
            else ("EXECUTION_FAILED" if execution_failed else "")
        )
        failure_message = (
            failed_step.error_message
            if failed_step and failed_step.error_message
            else (f"Execution stopped with outcome {outcome}" if execution_failed else "")
        )

        presentation_artifacts: list[dict[str, Any]] = []
        for art in artifact_store.find_by_execution(execution_id):
            payload = art.metadata.get("tool_result")
            if not isinstance(payload, dict):
                payload = {}
            presentation_artifacts.append({
                "artifact_id": art.artifact_id,
                "artifact_type": art.artifact_type,
                "type": art.artifact_type,
                "resource_id": art.resource_id,
                "summary": art.summary,
                "data": payload,
                "step_id": art.step_id,
            })

        result_steps = [
            {
                "step_id": step.step_id,
                "capability": step.capability,
                "status": step.status.value,
                "retry_count": step.retry_count,
                "error_code": step.error_code,
                "error_message": step.error_message,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
            }
            for step in (final.steps if final else [])
        ]
        schedule_payload = next(
            (
                item.get("data")
                for item in presentation_artifacts
                if item.get("artifact_type") == "SCHEDULE"
                and isinstance(item.get("data"), dict)
            ),
            None,
        )

        # Phase 6.6: record episodic + procedural memory
        self._record_episodic(
            ctx=ctx, task_id=task_id,
            status="COMPLETED" if not execution_failed else "FAILED",
            draft_id=draft_id, schedule_id=schedule_id,
        )
        self._record_procedural(
            ctx=ctx,
            status="COMPLETED" if not execution_failed else "FAILED",
            template_name=template_name,
            step_count=len(final.steps) if final else 0,
            tool_count=sum(1 for s in (final.steps if final else [])
                           if s.status in (StepStatus.COMPLETED, StepStatus.FAILED,
                                            StepStatus.FAILED_RETRYABLE)
                           and s.capability not in ("ANALYZE_CONTENT_PATTERNS",
                                                    "VALIDATE_QUALITY")),
        )

        return RuntimeResult(
            success=not execution_failed,
            status="FAILED" if execution_failed else "COMPLETED",
            run_id=ctx.run_id, task_id=task_id,
            execution_id=execution_id,
            content=(
                f"执行失败：{failure_message}"
                if execution_failed
                else f"已完成：{ctx.user_message[:100]}"
            ),
            summary=ctx.user_message[:200],
            started_execution=True,
            side_effect_committed=draft_id is not None,
            fallback_allowed=True,
            execution_path="runtime",
            events=_events(collector, ctx.trace_id, ctx.run_id) if collector else [],
            draft_id=draft_id,
            artifact_ids=[a.artifact_id
                          for a in artifact_store.find_by_execution(execution_id)],
            artifacts=presentation_artifacts,
            steps=result_steps,
            schedule=schedule_payload,
            trace_id=ctx.trace_id,
            tool_rounds=sum(1 for s in (final.steps if final else [])
                            if s.status in (StepStatus.COMPLETED, StepStatus.FAILED,
                                             StepStatus.FAILED_RETRYABLE)
                             and s.capability not in ("ANALYZE_CONTENT_PATTERNS",
                                                      "VALIDATE_QUALITY")),
            duration_ms=elapsed,
            error_code=failure_code,
            error_message=failure_message,
            failure_state=(
                {
                    "step_id": failed_step.step_id,
                    "capability": failed_step.capability,
                    "status": failed_step.status.value,
                }
                if failed_step is not None
                else None
            ),
        )

    @staticmethod
    def _fail_detached_execution(
        worker: Any,
        execution_id: str,
        error: Exception,
    ) -> None:
        """Make an unexpected detached-task exception visible to Runtime API."""
        state = worker._state
        try:
            execution = state._require_execution(execution_id)
        except ValueError:
            return

        running = [step for step in execution.steps if step.status == StepStatus.RUNNING]
        if running:
            for step in running:
                state.fail_step(
                    execution_id,
                    step.step_execution_id,
                    error_code="RUNTIME_ERROR",
                    error_message=str(error) or "Runtime execution failed",
                    permanent=True,
                )
            return

        # No step was claimed yet.  Persist the terminal state directly and
        # emit the same lifecycle event used by normal state transitions.
        execution.status = ExecutionStatus.FAILED
        execution.updated_at = _now_iso()
        worker._repo.save(execution)
        state.event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=EventType.EXECUTION_FAILED,
                payload={
                    "error_code": "RUNTIME_ERROR",
                    "error_message": str(error) or "Runtime execution failed",
                },
            )
        )

    def _recall_memories(self, ctx: RuntimeContext) -> None:
        """Populate ctx.memory_context with recalled memories."""
        try:
            from greenbook_assistant_core.agent_memory.models import (
                MemoryQuery,
                MemoryType,
            )

            # Semantic: user preferences
            prefs = self._memory_mgr.recall(
                MemoryQuery(user_id=ctx.user_id, type=MemoryType.SEMANTIC,
                            limit=5))
            ctx.memory_context["preferences"] = [
                {"type": r.metadata.get("preference_type", ""),
                 "value": r.metadata.get("value", ""),
                 "confidence": r.metadata.get("confidence", 0.0)}
                for r in prefs
            ]

            # Episodic: recent task history
            recent = self._memory_mgr.recall(
                MemoryQuery(user_id=ctx.user_id, type=MemoryType.EPISODIC,
                            limit=5, sort_by="created_at"))
            ctx.memory_context["recent_tasks"] = [
                {"goal": r.metadata.get("goal", ""),
                 "category": r.metadata.get("goal_category", ""),
                 "status": r.metadata.get("status", ""),
                 "draft_id": r.metadata.get("draft_id"),
                 "schedule_id": r.metadata.get("schedule_id")}
                for r in recent
            ]
        except Exception:
            logger.debug("Memory recall failed", exc_info=True)
            ctx.memory_context = {}

        try:
            # Procedural: strategies for current goal
            from greenbook_assistant_core.agent_memory.strategy import (
                StrategyRetriever,
            )
            gc = getattr(ctx.task_intent, "goal_category", "")
            retriever = StrategyRetriever(self._memory_mgr.store)
            strategies = retriever.retrieve(
                user_id=ctx.user_id, goal_category=gc or "", limit=3,
            )
            ctx.memory_context["strategies"] = strategies
        except Exception:
            logger.debug("Procedural memory recall failed", exc_info=True)

    def _record_procedural(
        self, *, ctx: RuntimeContext, status: str,
        step_count: int = 0, tool_count: int = 0,
        template_name: str = "",
    ) -> None:
        """Extract and save procedural memory from execution outcome."""
        try:
            from greenbook_assistant_core.agent_memory.extractor import (
                ProceduralMemoryExtractor,
            )
            gc = getattr(ctx.task_intent, "goal_category", "")
            record = ProceduralMemoryExtractor.extract(
                user_id=ctx.user_id,
                goal_category=gc or "",
                template_name=template_name,
                status=status,
                tool_count=tool_count,
                step_count=step_count,
            )
            if record is not None:
                self._memory_mgr.remember(record)
        except Exception:
            logger.debug("Failed to record procedural memory", exc_info=True)

    def _record_episodic(
        self, *, ctx: RuntimeContext, task_id: str, status: str,
        draft_id: str | None = None, schedule_id: str | None = None,
    ) -> None:
        """Record execution outcome as episodic memory."""
        try:
            gc = getattr(ctx.task_intent, "goal_category", "")
            goal = getattr(ctx.task_intent, "goal", ctx.user_message)
            self._memory_mgr.remember_execution(
                user_id=ctx.user_id,
                goal=goal,
                category=gc or "",
                status=status,
                draft_id=draft_id,
                schedule_id=schedule_id,
            )
        except Exception:
            logger.debug("Failed to record episodic memory", exc_info=True)

    @staticmethod
    def _fail(ctx: RuntimeContext, code: str, msg: str) -> RuntimeResult:
        return RuntimeResult(
            success=False, status="FAILED",
            run_id=ctx.run_id, trace_id=ctx.trace_id,
            error_code=code, error_message=msg,
            execution_path="runtime",
        )


def _intent_spec_from_task_intent(task_intent: Any) -> IntentSpec | None:
    """Recover the lossless IntentSpec snapshot when an L2 turn provided it."""

    raw = getattr(task_intent, "intent_spec", None)
    if not raw:
        return None
    if isinstance(raw, IntentSpec):
        return raw
    try:
        return IntentSpec.model_validate(raw)
    except Exception:
        logger.debug("Ignoring malformed IntentSpec snapshot", exc_info=True)
        return None


def _tool_schemas_from_mcp(mcp: Any) -> Sequence[Mapping[str, Any]] | None:
    """Read exported MCP schemas without requiring a concrete MCP class.

    Test doubles often use ``AsyncMock`` and therefore expose arbitrary
    attributes.  Only a synchronous list of definitions is accepted here;
    malformed or unavailable schema exports fall back to the core capability
    input metadata inside ``ArgumentBinder``.
    """

    provider = getattr(mcp, "get_tool_definitions", None)
    if provider is None or inspect.iscoroutinefunction(provider):
        return None
    try:
        definitions = provider()
    except Exception:
        logger.debug("MCP tool schema export failed", exc_info=True)
        return None
    if inspect.isawaitable(definitions) or not isinstance(definitions, Sequence):
        return None
    return definitions


def _seed_step_constraints(execution: Any, plan: Any, repository: Any) -> None:
    """Persist bound plan arguments in step checkpoints for Worker consumption."""

    if repository is None or not hasattr(repository, "save"):
        return

    constraints_by_step = {
        step.step_id: dict(step.constraints)
        for step in plan.steps
    }
    for step_execution in execution.steps:
        bound = constraints_by_step.get(step_execution.step_id)
        if bound is not None:
            step_execution.checkpoint_data["constraints"] = bound
    repository.save(execution)


def _execution_dispatch_payload(
    *,
    ctx: RuntimeContext,
    executable: Any,
    intent_spec: IntentSpec | None,
) -> dict[str, Any]:
    """Build a non-secret handoff snapshot for a queue consumer.

    The queue owns delivery only. A future process-specific execution handler
    can use this snapshot to rebuild its Runtime context. Access tokens are
    intentionally removed; a worker must resolve authorization through its
    configured credential boundary instead of reading a bearer token from the
    queue.
    """

    auth_payload = _safe_model_payload(ctx.auth)
    for secret_name in ("raw_access_token", "access_token", "refresh_token"):
        auth_payload.pop(secret_name, None)
    task_context = ctx.task_context
    trace_context = TraceContext(
        conversation_id=ctx.conversation_id,
        run_id=ctx.run_id,
        trace_id=ctx.trace_id,
        task_id=ctx.task_id,
        execution_id=ctx.execution_id,
    )
    return {
        "run_id": ctx.run_id,
        "trace_id": ctx.trace_id,
        "conversation_id": ctx.conversation_id,
        "user_id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "task_id": ctx.task_id,
        "timezone": ctx.timezone,
        "user_message": ctx.user_message,
        "conversation_history": [dict(item) for item in ctx.conversation_history],
        "active_artifact_id": ctx.active_artifact_id,
        "active_draft_id": ctx.active_draft_id,
        "active_schedule_id": ctx.active_schedule_id,
        "task_intent": _safe_model_payload(ctx.task_intent),
        "task_context": {
            "task_id": task_context.task_id,
            "goal": task_context.goal,
            "constraints": [dict(item) for item in task_context.constraints],
            "target": _safe_model_payload(task_context.target),
            "active_artifact_id": task_context.active_artifact_id,
            "artifact_refs": [
                _safe_model_payload(item) for item in task_context.artifact_refs
            ],
        } if task_context is not None else None,
        "intent_spec": (
            intent_spec.model_dump(mode="json") if intent_spec is not None else None
        ),
        "executable_plan": _safe_model_payload(executable),
        "session": _safe_model_payload(ctx.session),
        "auth_context": auth_payload,
        "trace_context": trace_context.model_dump(mode="json"),
    }


def _safe_model_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        dumped = asdict(value)
        return dict(dumped) if isinstance(dumped, dict) else {}
    return {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _events(collector: TraceCollector, trace_id: str, run_id: str) -> list[dict]:
    return [
        {"event": e.event_type.value, "data": {
            "run_id": run_id,
            "step_id": e.step_id,
            "capability": e.capability,
            "tool_name": e.tool_name,
            "payload": e.payload,
            "trace_context": (
                e.trace_context.model_dump(mode="json")
                if e.trace_context is not None else None
            ),
        }}
        for e in collector.timeline(trace_id)
    ]
