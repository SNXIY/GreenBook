"""RuntimeAgentService — full Runtime pipeline for one Agent turn.

Phase 5.5: resolved ExecutionInput → Validator → Worker → Result.

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

from greenbook_agent_core.agent.state import AgentState
from greenbook_agent_core.artifact.store import ArtifactStorePort
from greenbook_agent_core.capability.mapper import CapabilityMapper
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.execution.argument_binder import ArgumentBinder
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.event_store import ExecutionEventStore
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.execution_queue import (
    ExecutionQueueMessage,
    ExecutionQueueProtocol,
)
from greenbook_agent_core.execution.input import ExecutionInput
from greenbook_agent_core.execution.models import ExecutionStatus, StepStatus
from greenbook_agent_core.execution.operation_tracking import ExternalOperationTracker
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_agent_core.goal.models import GoalTree
from greenbook_agent_core.observability.collector import TraceCollector
from greenbook_agent_core.observability.context import TraceContext
from greenbook_agent_core.observability.metrics import MetricsCollector
from greenbook_agent_core.observability.trace import AgentTrace
from greenbook_agent_core.planning.contracts import PlanningDecisionType, TaskPlan
from greenbook_agent_core.planning.dynamic import DynamicPlanner
from greenbook_agent_core.planning.models import ExecutablePlan
from greenbook_agent_core.planning.validation import PlanValidator
from greenbook_agent_core.runtime.container import RuntimeContainer

from ..models.runtime_context import RuntimeContext, TargetContext, TaskContext
from ..models.runtime_result import RuntimeResult

logger = logging.getLogger(__name__)

RuntimeCompletionCallback = Callable[
    [RuntimeResult], Awaitable[None] | None
]


class RuntimeAgentService:
    """Execute a turn through the new Runtime pipeline.

    ``submit_plan`` accepts a typed TaskPlan and delegates the stable
    single-task pipeline to Reliable Execution.
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
        metrics_collector: MetricsCollector | None = None,
        retry_scheduler: RetryScheduler | None = None,
        artifact_store: ArtifactStorePort | None = None,
        container: RuntimeContainer | None = None,
        memory_manager: Any | None = None,
        task_manager: Any | None = None,
    ) -> None:
        self._container = container or RuntimeContainer.for_testing()
        self._execution_repository = repository or self._container.execution_repository
        self._execution_event_store = event_store or self._container.event_store
        self._execution_checkpoint_store = (
            checkpoint_store or self._container.persistence.checkpoint_store
        )
        self._operation_tracker = operation_tracker
        self._execution_queue = execution_queue or self._container.persistence.execution_queue
        self._dispatch_mode = dispatch_mode.strip().lower()
        self._metrics = metrics_collector
        self._retry_scheduler = retry_scheduler
        self._artifact_store = artifact_store or self._container.artifact_store
        self._registry = self._container.capability_registry
        self._mapper = CapabilityMapper(self._registry)
        self._validator = PlanValidator(
            self._registry,
            tool_registry=self._container.tool_registry,
        )
        # Phase 6.5: Human-in-the-loop
        from greenbook_agent_core.human.manager import HumanInteractionManager
        self._human_mgr = HumanInteractionManager()
        # Phase 6.6: Agent Memory
        if memory_manager is None:
            from greenbook_agent_core.memory.manager import MemoryManager

            memory_manager = MemoryManager()
        self._memory_mgr = memory_manager
        self._task_manager = task_manager
        self._paused_contexts: dict[str, tuple[RuntimeContext, str]] = {}
        # Detached executions are owned by the Runtime service, not by an
        # HTTP request.  This keeps long Creator calls alive after the 202
        # response and gives the API a read-only result registry for the
        # conversation projection.
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_results: dict[str, RuntimeResult] = {}
        # interaction_id → (ctx, checkpoint_name)

    @property
    def container(self) -> RuntimeContainer:
        """Return the composition root used by this Runtime service."""

        return self._container

    # ── Phase 6.1 entry: decomposition + routing ────────────────

    async def submit_plan(
        self,
        ctx: RuntimeContext,
        plan: TaskPlan,
        *,
        completion_callback: RuntimeCompletionCallback | None = None,
    ) -> RuntimeResult:
        """Submit a canonical GoalCompiler plan to Reliable Execution.

        This is the Agent Runtime submission boundary.  The plan has already
        been produced from a ``GoalTree``; this method performs no user
        user understanding, plan-source selection, or tool selection.  It allocates
        the normal ``PlanExecution`` and always publishes multi-step work to
        the configured queue.  Workers continue to own execution, retry,
        checkpoint, ledger, and evidence state.
        """

        if not isinstance(plan, TaskPlan):
            return self._fail(
                ctx,
                "TASK_PLAN_REQUIRED",
                "Reliable Execution submission requires a typed TaskPlan.",
            )
        return await self._execute_single(
            ctx,
            completion_callback=completion_callback,
            plan_override=plan,
            force_queue=True,
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
            input_data = payload.get("execution_input")
            if input_data is None:
                raise ValueError(
                    "execution_input is required; legacy Intent queue payloads are retired"
                )
            execution_input = ExecutionInput.model_validate(input_data)
            if not execution_input.steps:
                raise ValueError(
                    "execution_input.steps is required; legacy plan envelopes are retired"
                )
            executable = execution_input.to_executable_plan()
            task_context_data = {
                "task_id": execution_input.task_id,
                "goal": execution_input.goal,
                "constraints": execution_input.constraints,
                "target": execution_input.target,
                "artifact_refs": execution_input.artifact_refs,
            }
            target_data = task_context_data.get("target") or None
            target = (
                TargetContext(**target_data)
                if isinstance(target_data, dict)
                else None
            )
            task_context = TaskContext(
                task_id=str(task_context_data.get("task_id") or execution_input.task_id),
                goal=str(task_context_data.get("goal") or ""),
                # TaskContext is shared with the presentation adapter. The
                # Runtime itself consumes the canonical ExecutionInput below;
                # no user-semantic contract is reconstructed.
                execution_input=execution_input,
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
                conversation_id=str(execution_input.conversation_id or payload.get("conversation_id", "")),
                run_id=str(payload.get("run_id", "")),
                trace_id=str(payload.get("trace_id", message.trace_id)),
                task_id=str(execution_input.task_id),
                execution_id=message.execution_id,
                task_context=task_context,
                user_id=str(execution_input.user_id or payload.get("user_id", "")),
                tenant_id=str(execution_input.tenant_id or payload.get("tenant_id", "")),
                timezone=str(payload.get("timezone", "Asia/Shanghai")),
                user_message=execution_input.goal,
                conversation_history=[
                    dict(item) for item in (payload.get("conversation_history") or ())
                ],
                conversation_context=dict(payload.get("conversation_context") or {}),
                task_command=dict(payload.get("task_command") or {}),
                execution_input=execution_input,
                session=session,
                active_artifact_id=payload.get("active_artifact_id"),
                active_draft_id=payload.get("active_draft_id"),
                active_schedule_id=payload.get("active_schedule_id"),
                approval_granted=bool(payload.get("approval_granted", False)),
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
        plan_override: TaskPlan | None = None,
        force_queue: bool = False,
    ) -> RuntimeResult:
        t0 = time.monotonic()
        task_context = ctx.task_context
        execution_input = ctx.execution_input
        if (
            task_context is None
            or not task_context.task_id
            or not isinstance(execution_input, ExecutionInput)
        ):
            return self._fail(
                ctx,
                "EXECUTION_INPUT_REQUIRED",
                "Reliable Execution requires TaskContext plus a typed ExecutionInput.",
            )
        task_id = task_context.task_id

        # Phase 6.6: Recall agent memory
        self._recall_memories(ctx)

        # ── 1. Capabilities ─────────────────────────────────
        gc = str(
            getattr(execution_input, "goal_category", "")
            or "COMPOSITE"
        )
        if not self._mapper.capabilities_for_goal(gc):
            return self._fail(ctx, "NO_CAPABILITY", f"No capabilities for {gc}")

        # ── 1.3 Task Reference Resolution (Phase 6.2.2-B) ────
        # ── 1.5 Resource Resolution (Phase 5.6) ──────────────
        # ── 2. Plan ─────────────────────────────────────────
        if plan_override is not None:
            plan = plan_override.model_copy(deep=True)
            if not plan.task_id:
                plan.task_id = task_id
        elif executable_override is None:
            if isinstance(execution_input, ExecutionInput) and execution_input.steps:
                executable_override = execution_input.to_executable_plan()
                plan = TaskPlan(
                    plan_id=executable_override.plan_id,
                    task_id=task_id,
                    plan_source=executable_override.plan_source,
                    plan_version=executable_override.plan_version,
                    steps=[step.model_copy(deep=True) for step in executable_override.steps],
                )
            else:
                return self._fail(
                    ctx,
                    "EXECUTION_PLAN_REQUIRED",
                    "Reliable Execution accepts only a compiled TaskPlan or ExecutionInput steps.",
                )
        else:
            plan = TaskPlan(
                plan_id=executable_override.plan_id,
                task_id=task_id,
                plan_source=executable_override.plan_source,
                steps=[step.model_copy(deep=True) for step in executable_override.steps],
            )

        # Bind known arguments before validation/execution.  Runtime execution
        # still binds at the last boundary so upstream artifact IDs (for
        # example the generated draft_id) can be merged into later steps.
        argument_binder = ArgumentBinder(
            _tool_schemas_from_mcp(ctx.mcp),
            registry=self._registry,
            container=self._container,
            timezone=ctx.timezone,
        )
        argument_binder.bind_plan(
            plan,
            execution_input=execution_input,
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
        trace.plan_created(plan_source=plan.plan_source, step_count=len(plan.steps))

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
                "approval_granted": ctx.approval_granted,
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
                "data": inv_result.data,
                "provenance": list(inv_result.provenance),
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
                # Async completion is a continuation of the same step, not a
                # new execution. Reopen the durable RUNNING step so Worker
                # can replay the ToolRuntime result from its idempotency
                # ledger and apply the normal success/failure transition.
                execution = current_worker._repo.find_by_id(ctx.execution_id)
                async_step = next(
                    (
                        step for step in (execution.steps if execution else [])
                        if step.step_id == inv_ctx.step_id
                    ),
                    None,
                )
                if async_step is not None:
                    current_worker._state.recover_step(
                        ctx.execution_id,
                        async_step.step_execution_id,
                    )
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
                        plan_source=plan.plan_source,
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
            metrics_collector=self._metrics,
        )

        # ── 6. CapabilityExecutor (via invoke_fn → ToolRuntime) ──
        async def invoke_fn(inv_ctx: ToolInvocationContext) -> dict:
            inv_result = await tool_runtime.invoke(inv_ctx)
            tool_results[inv_ctx.step_id] = {
                "tool_name": inv_result.tool_name,
                "capability": inv_ctx.capability,
                "ok": inv_result.ok,
                "data": inv_result.data,
                "provenance": list(inv_result.provenance),
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
                "provenance": list(inv_result.provenance),
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
            execution_input=execution_input,
            user_message=ctx.user_message,
            timezone=ctx.timezone,
            active_draft_id=ctx.active_draft_id,
            active_schedule_id=ctx.active_schedule_id,
            tool_registry=self._container.tool_registry,
        )

        # ── 7. Worker (with trace) ──────────────────────────
        worker = ExecutionWorker(
            executor,
            repository=self._execution_repository,
            trace=trace,
            event_store=self._execution_event_store,
            checkpoint_store=self._execution_checkpoint_store,
            operation_tracker=self._operation_tracker,
            metrics_collector=self._metrics,
            retry_scheduler=self._retry_scheduler,
            replan_callback=self._replan_callback(ctx, task_id, execution_input),
            tool_catalog=self._available_tool_metadata(),
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
            _seed_step_constraints(
                execution,
                plan,
                getattr(worker, "_repo", None),
                execution_input=execution_input,
            )

        # Backfill execution_id for trace + executor
        ctx.execution_id = execution.execution_id
        trace.execution_id = execution.execution_id
        executor.bind_execution_id(execution.execution_id)
        trace_context = trace_context.for_execution(execution.execution_id)
        trace.bind_context(trace_context)
        worker.bind_trace_context(trace_context)
        if (force_queue or self._dispatch_mode == "queue") and not existing_execution_id:
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
                    plan=plan,
                    executable=executable,
                    policy_catalog=self._container.tool_registry,
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
                            ctx,
                            worker,
                            execution.execution_id,
                            tool_results=tool_results,
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
                            plan_source=plan.plan_source,
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
            return self._pause_for_approval(
                ctx,
                worker,
                execution.execution_id,
                tool_results=tool_results,
            )

        # ── 9. Artifacts via ArtifactStore ──────────────────
        return self._finish_execution(
            ctx=ctx, worker=worker, execution_id=execution.execution_id,
            outcome=outcome, task_id=task_id, t0=t0,
            trace=trace, collector=collector,
            plan_source=plan.plan_source,
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
        self,
        ctx: RuntimeContext,
        worker: Any,
        execution_id: str,
        *,
        tool_results: dict[str, dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        """Pause execution for user approval via HumanInteractionManager."""
        from greenbook_agent_core.human.models import InteractionType

        # A waiting execution is already a durable checkpoint. Materialize
        # completed step artifacts before publishing the approval result so
        # the API-side projection can bind resources (especially draft_id)
        # back to the Conversation.
        self._materialize_completed_artifacts(
            worker,
            execution_id,
            task_id=ctx.task_id or "",
            tool_results=tool_results,
        )
        presentation_artifacts = self._presentation_artifacts(
            execution_id,
            tool_results=tool_results,
        )
        draft_id = next(
            (
                str(item["resource_id"])
                for item in presentation_artifacts
                if item.get("resource_type") == "DRAFT"
                and item.get("resource_id")
            ),
            None,
        )
        artifact_ids = [
            str(item["artifact_id"])
            for item in presentation_artifacts
            if item.get("artifact_id")
        ]
        waiting_step = next(
            (
                step
                for step in (worker._repo.find_by_id(execution_id).steps if worker._repo.find_by_id(execution_id) else [])
                if step.status == StepStatus.WAITING_APPROVAL
            ),
            None,
        )
        approval_operation = str(
            getattr(waiting_step, "tool_name", "")
            or getattr(waiting_step, "capability", "")
            or "RUNTIME_APPROVAL"
        )
        approval_constraints = dict(
            getattr(waiting_step, "checkpoint_data", {}) or {}
        ).get("constraints") or {}
        approval_resource_id = draft_id or (
            str(approval_constraints.get("draft_id"))
            if isinstance(approval_constraints, dict)
            and approval_constraints.get("draft_id")
            else None
        )
        approval_question = (
            "创建发布排期前需要你的确认。是否继续？"
            if approval_operation == "publication.schedule"
            else "此操作需要你的确认。是否继续？"
        )
        req = self._human_mgr.pause(
            execution_id=execution_id, type=InteractionType.APPROVAL,
            question=approval_question,
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
            draft_id=draft_id,
            artifact_ids=artifact_ids,
            artifacts=presentation_artifacts,
            side_effect_committed=draft_id is not None,
            approval_data={
                "approval_id": req.interaction_id,
                "execution_id": execution_id,
                "operation": approval_operation,
                "resource_id": approval_resource_id,
                "description": approval_question,
                "payload": {
                    "interaction_id": req.interaction_id,
                    "tool_name": approval_operation,
                },
            },
            partial_results={
                "interaction_id": req.interaction_id,
                "artifact_ids": artifact_ids,
            },
        )

    def _pause_for_input(
        self, ctx: RuntimeContext, question: str, options: list[dict] | None = None,
    ) -> RuntimeResult:
        """Pause execution to request free-form user input."""
        from greenbook_agent_core.human.models import InteractionType
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
        from greenbook_agent_core.human.models import InteractionType
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
        from greenbook_agent_core.human.models import (
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
                from greenbook_agent_core.execution.state_manager import ExecutionStateManager
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

            from greenbook_agent_core.execution.worker import RunOutcome
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
                if ctx.execution_input:
                    existing = list(getattr(ctx.execution_input, "constraints", []) or [])
                    existing.append({"type": "USER_INPUT", "value": resp.content})
                    ctx.execution_input.constraints = existing
            return await self._execute_single(ctx)

        if selected_value:
            ctx.task_id = selected_value
            if ctx.execution_input:
                ctx.execution_input.target = {"task_id": selected_value}

        return await self._execute_single(ctx)

    def _materialize_completed_artifacts(
        self,
        worker: Any,
        execution_id: str,
        *,
        task_id: str,
        tool_results: dict[str, dict[str, Any]] | None = None,
        trace: Any = None,
    ) -> None:
        """Persist completed step artifacts exactly once.

        ExecutionWorker persists the canonical ``ArtifactHandle`` in the
        checkpoint. The API projection needs an ArtifactStore record, but a
        WAITING_APPROVAL result can be emitted before the normal terminal
        ``_finish_execution`` path. This bridge is intentionally idempotent
        by step id so approval resume cannot duplicate artifacts.
        """

        final = worker._repo.find_by_id(execution_id)
        creator = getattr(self._artifact_store, "create_from_result", None)
        finder = getattr(self._artifact_store, "find_by_execution", None)
        if final is None or not callable(creator) or not callable(finder):
            return

        existing_step_ids = {
            str(getattr(artifact, "step_id", ""))
            for artifact in finder(execution_id)
            if getattr(artifact, "step_id", "")
        }
        for step_ex in final.steps:
            step_id = str(getattr(step_ex, "step_id", "") or "")
            if (
                getattr(step_ex, "status", None) != StepStatus.COMPLETED
                or getattr(step_ex, "output_artifact", None) is None
                or not step_id
                or step_id in existing_step_ids
            ):
                continue
            raw_evidence = (tool_results or {}).get(step_id, {}).get("evidence")
            evidence = None
            if isinstance(raw_evidence, ExecutionEvidence):
                evidence = raw_evidence
            elif isinstance(raw_evidence, Mapping):
                try:
                    evidence = ExecutionEvidence.model_validate(raw_evidence)
                except Exception:
                    logger.warning(
                        "Ignoring malformed persisted tool evidence "
                        "execution_id=%s step_id=%s",
                        execution_id,
                        step_id,
                        exc_info=True,
                    )
            step_result = type(
                "_PersistedStepResult",
                (),
                {
                    "ok": True,
                    "artifact": step_ex.output_artifact,
                    "tool_name": (tool_results or {}).get(step_id, {}).get(
                        "tool_name", getattr(step_ex, "tool_name", "") or step_ex.capability
                    ),
                    "capability": step_ex.capability,
                    "tool_result": {
                        "data": (tool_results or {}).get(step_id, {}).get("data", {}),
                    },
                    "evidence": evidence,
                },
            )()
            try:
                artifact = creator(
                    step_result,
                    task_id=task_id,
                    execution_id=execution_id,
                    step_id=step_id,
                )
            except Exception:
                logger.exception(
                    "Failed to materialize artifact execution_id=%s step_id=%s",
                    execution_id,
                    step_id,
                )
                continue
            if artifact is not None:
                existing_step_ids.add(step_id)
                if trace is not None:
                    trace.artifact_created(step_ex, artifact)

    def _presentation_artifacts(
        self,
        execution_id: str,
        *,
        tool_results: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the body-free artifact projection plus local tool data."""

        finder = getattr(self._artifact_store, "find_by_execution", None)
        if not callable(finder):
            return []
        presentation: list[dict[str, Any]] = []
        for artifact in finder(execution_id):
            metadata = getattr(artifact, "metadata", {}) or {}
            payload = metadata.get("projection") if isinstance(metadata, dict) else {}
            if not isinstance(payload, dict):
                payload = {}
            # Artifact metadata intentionally remains body-free. The
            # RuntimeResult is the presentation boundary, so expose the
            # current tool result body while the execution process retains it.
            step_id = str(getattr(artifact, "step_id", "") or "")
            tool_data = (tool_results or {}).get(step_id, {}).get("data", {})
            data = dict(payload)
            if isinstance(tool_data, dict):
                data.update(tool_data)
            artifact_type = str(getattr(artifact, "artifact_type", "") or "")
            presentation.append({
                "artifact_id": str(getattr(artifact, "artifact_id", "") or ""),
                "artifact_type": artifact_type,
                "type": artifact_type,
                "resource_id": getattr(artifact, "resource_id", None),
                "resource_type": (
                    getattr(artifact, "resource_type", None)
                    or getattr(artifact, "resource_kind", None)
                ),
                "title": getattr(artifact, "title", None) or payload.get("title"),
                "summary": getattr(artifact, "summary", "") or payload.get("summary", ""),
                "status": getattr(artifact, "status", None) or payload.get("status"),
                "run_at": getattr(artifact, "run_at", None) or payload.get("run_at"),
                "timezone": getattr(artifact, "timezone", None) or payload.get("timezone"),
                "data": data,
                "step_id": step_id,
            })
        return presentation

    def _finish_execution(
        self, *, ctx: RuntimeContext, worker: Any, execution_id: str,
        outcome: Any, task_id: str, t0: float,
        trace: Any, collector: Any,
        plan_source: str = "",
        tool_results: dict[str, dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        """Collect artifacts + build result after Worker completes."""
        artifact_store = self._artifact_store
        final = worker._repo.find_by_id(execution_id)

        if outcome == RunOutcome.WAITING_HUMAN:
            self._materialize_completed_artifacts(
                worker,
                execution_id,
                task_id=task_id,
                tool_results=tool_results,
            )
            presentation_artifacts = self._presentation_artifacts(
                execution_id,
                tool_results=tool_results,
            )
            return RuntimeResult(
                success=False,
                status="WAITING_HUMAN",
                run_id=ctx.run_id,
                task_id=task_id,
                execution_id=execution_id,
                content="运行依据不足，需要人工确认后才能继续。",
                summary=ctx.user_message[:200],
                started_execution=True,
                execution_path="runtime",
                error_code="WAITING_HUMAN",
                error_message="Dynamic replanning requires human input.",
                artifacts=presentation_artifacts,
                trace_id=ctx.trace_id,
                steps=[
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
                ],
            )

        if outcome in {RunOutcome.PAUSED, RunOutcome.BLOCKED}:
            runtime_status = (
                "PAUSED"
                if outcome == RunOutcome.PAUSED
                else (
                    "CANCELLED"
                    if final is not None and final.status == ExecutionStatus.CANCELLED
                    else "FAILED"
                )
            )
            return RuntimeResult(
                success=False,
                status=runtime_status,
                run_id=ctx.run_id,
                task_id=task_id,
                execution_id=execution_id,
                content=(
                    "Execution paused at a durable checkpoint."
                    if runtime_status == "PAUSED"
                    else "Execution cancelled."
                ),
                summary=ctx.user_message[:200],
                started_execution=True,
                execution_path="runtime",
                steps=[
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
                ],
                trace_id=ctx.trace_id,
            )

        self._materialize_completed_artifacts(
            worker,
            execution_id,
            task_id=task_id,
            tool_results=tool_results,
            trace=trace,
        )

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
            and not step.checkpoint_data.get("superseded_by")
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

        presentation_artifacts = self._presentation_artifacts(
            execution_id,
            tool_results=tool_results,
        )

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
            plan_source=plan_source,
            step_count=len(final.steps) if final else 0,
            tool_count=sum(1 for s in (final.steps if final else [])
                           if s.status in (StepStatus.COMPLETED, StepStatus.FAILED,
                                            StepStatus.FAILED_RETRYABLE)
                           and s.capability not in ("ANALYZE_CONTENT_PATTERNS",
                                                     "VALIDATE_QUALITY")),
        )

        if self._metrics is not None:
            metrics_context = TraceContext(
                conversation_id=ctx.conversation_id,
                run_id=ctx.run_id,
                trace_id=ctx.trace_id,
                task_id=task_id,
                execution_id=execution_id,
            )
            self._metrics.record_execution(
                status="FAILED" if execution_failed else "COMPLETED",
                duration_ms=elapsed,
                context=metrics_context,
            )

        return RuntimeResult(
            success=not execution_failed,
            status="FAILED" if execution_failed else "COMPLETED",
            run_id=ctx.run_id, task_id=task_id,
            execution_id=execution_id,
            content=(
                f"执行失败：{failure_message}"
                if execution_failed
                else ""
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
            from greenbook_agent_core.memory.models import (
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
            from greenbook_agent_core.memory.strategy import (
                StrategyRetriever,
            )
            source = ctx.execution_input
            gc = getattr(source, "goal_category", "")
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
        plan_source: str = "",
    ) -> None:
        """Extract and save procedural memory from execution outcome."""
        try:
            from greenbook_agent_core.memory.extractor import (
                ProceduralMemoryExtractor,
            )
            source = ctx.execution_input
            gc = getattr(source, "goal_category", "")
            record = ProceduralMemoryExtractor.extract(
                user_id=ctx.user_id,
                goal_category=gc or "",
                plan_source=plan_source,
                status=status,
                tool_count=tool_count,
                step_count=step_count,
            )
            if record is not None:
                self._memory_mgr.remember(record)
        except Exception:
            logger.debug("Failed to record procedural memory", exc_info=True)

    def _available_tool_metadata(self) -> list[Any]:
        """Read the canonical MCP ToolMetadata projection for planning."""

        provider = getattr(self._container.tool_registry, "list_tool_metadata", None)
        if not callable(provider):
            return []
        try:
            values = provider()
        except Exception:
            logger.warning("Unable to load ToolMetadata for dynamic replanning", exc_info=True)
            return []
        if inspect.isawaitable(values) or not isinstance(values, Sequence):
            return []
        return list(values)

    def _replan_callback(
        self,
        ctx: RuntimeContext,
        task_id: str,
        execution_input: ExecutionInput,
    ) -> Callable[..., Awaitable[Any]]:
        """Compose DynamicPlanner at the API boundary for durable Worker use."""

        async def replan(execution: Any, step: Any, result: Any, observation: Mapping[str, Any]) -> Any:
            from greenbook_agent_core.planning.contracts import PlanningDecision

            raw_goal_tree = (execution_input.execution_metadata or {}).get("goal_tree")
            try:
                goal_tree = GoalTree.model_validate(raw_goal_tree)
            except (TypeError, ValueError):
                return PlanningDecision(
                    decision=PlanningDecisionType.ASK_HUMAN,
                    reason="The durable execution has no valid GoalTree snapshot for safe replanning.",
                )

            task = None
            if self._task_manager is not None:
                getter = getattr(self._task_manager, "get_task", None)
                if callable(getter):
                    try:
                        task = getter(
                            task_id,
                            user_id=ctx.user_id,
                            tenant_id=ctx.tenant_id,
                        )
                        if inspect.isawaitable(task):
                            task = await task
                    except Exception:
                        logger.warning(
                            "Could not load Task for dynamic replanning task_id=%s",
                            task_id,
                            exc_info=True,
                        )

            tool_result = dict(getattr(result, "tool_result", {}) or {})
            tool_result.update(
                {
                    "ok": bool(getattr(result, "ok", False)),
                    "tool_name": str(getattr(result, "tool_name", "") or ""),
                    "tool_arguments": dict(getattr(step, "arguments", {}) or {}),
                    "error_code": str(getattr(result, "error_code", "") or ""),
                    "error_message": str(getattr(result, "error_message", "") or ""),
                    "request_sent": getattr(result, "request_sent", None),
                }
            )
            current_observation = {
                **dict(observation),
                "current_task": {
                    "task_id": task_id,
                    "step_id": str(getattr(step, "step_id", "") or ""),
                    "tool_name": str(getattr(step, "tool_name", "") or ""),
                    "capability": str(getattr(step, "capability", "") or ""),
                },
                "last_result": tool_result,
            }
            execution_history = [
                {
                    "step_id": item.step_id,
                    "capability": item.capability,
                    "tool_name": item.tool_name,
                    "status": item.status.value,
                    "error_code": item.error_code,
                    "observation": item.checkpoint_data.get("last_observation", {}),
                }
                for item in execution.steps
            ]
            agent_state = AgentState(
                goal=goal_tree.root_goal,
                goal_tree=goal_tree,
                available_tools=self._available_tool_metadata(),
                conversation_context=dict(ctx.conversation_context or {}),
                context_snapshot_id=str(
                    (execution_input.execution_metadata or {}).get(
                        "context_snapshot_id", ""
                    )
                ),
            )
            decision = await DynamicPlanner(
                llm=ctx.llm,
                model=ctx.model,
            ).replan(
                goal_tree=goal_tree,
                agent_state=agent_state,
                task=task,
                tool_catalog=agent_state.available_tools,
                execution_history=execution_history,
                observations=[current_observation],
                context_snapshot=ctx.conversation_context,
            )
            if self._task_manager is not None and decision.decision != PlanningDecisionType.CONTINUE:
                recorder = getattr(self._task_manager, "record_replan", None)
                if callable(recorder):
                    try:
                        recorded = recorder(
                            task_id,
                            decision=decision.decision.value,
                            observation=current_observation,
                            reason=decision.reason,
                        )
                        if inspect.isawaitable(recorded):
                            await recorded
                    except Exception:
                        logger.warning(
                            "Task plan revision projection failed task_id=%s",
                            task_id,
                            exc_info=True,
                        )
            return decision

        return replan

    def _record_episodic(
        self, *, ctx: RuntimeContext, task_id: str, status: str,
        draft_id: str | None = None, schedule_id: str | None = None,
    ) -> None:
        """Record execution outcome as episodic memory."""
        try:
            source = ctx.execution_input
            gc = getattr(source, "goal_category", "")
            goal = getattr(source, "goal", ctx.user_message)
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


def _seed_step_constraints(
    execution: Any,
    plan: Any,
    repository: Any,
    *,
    execution_input: ExecutionInput | None = None,
) -> None:
    """Persist bound plan arguments in step checkpoints for Worker consumption."""

    if repository is None or not hasattr(repository, "save"):
        return

    constraints_by_step = {
        step.step_id: dict(step.constraints)
        for step in plan.steps
    }
    input_by_step = {
        item.step_id: item
        for item in (getattr(execution_input, "steps", ()) or ())
        if getattr(item, "step_id", "")
    }
    for step_execution in execution.steps:
        bound = constraints_by_step.get(step_execution.step_id)
        if bound is not None:
            step_execution.checkpoint_data["constraints"] = bound
            step_execution.arguments = dict(bound)
            step_execution.tool_name = str(
                next(
                    (
                        getattr(step, "tool_name", "")
                        for step in plan.steps
                        if step.step_id == step_execution.step_id
                    ),
                    "",
                )
                or step_execution.tool_name
            )
        resolved = input_by_step.get(step_execution.step_id)
        if resolved is not None:
            step_execution.tool_name = str(
                getattr(resolved, "tool_name", "") or step_execution.tool_name
            )
            step_execution.idempotency_key = str(
                getattr(resolved, "idempotency_key", "")
                or step_execution.idempotency_key
            )
            step_execution.execution_mode = str(
                getattr(resolved, "execution_mode", "")
                or step_execution.execution_mode
            )
            step_execution.policy_snapshot = dict(
                getattr(resolved, "policy_snapshot", {}) or {}
            )
    repository.save(execution)


def _execution_dispatch_payload(
    *,
    ctx: RuntimeContext,
    plan: Any,
    executable: Any,
    policy_catalog: Any | None = None,
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
    resolved = ctx.execution_input
    root_goal = str(
        getattr(resolved, "goal", "")
        or getattr(resolved, "goal_category", "")
        or "Resolved execution"
    )
    execution_input = ExecutionInput.from_executable_plan(
        task_id=ctx.task_id or str(getattr(task_context, "task_id", "")),
        plan=plan,
        executable=executable,
        conversation_id=ctx.conversation_id,
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        goal_id=str(getattr(resolved, "goal_id", "") or ""),
        goal=root_goal,
        goal_category=str(getattr(resolved, "goal_category", "COMPOSITE")),
        constraints=[dict(item) for item in (getattr(task_context, "constraints", ()) or ())],
        artifact_refs=[
            _safe_model_payload(item) for item in (getattr(task_context, "artifact_refs", ()) or ())
        ],
        target=_safe_model_payload(getattr(task_context, "target", None)) or None,
        execution_metadata={
            **dict(getattr(resolved, "execution_metadata", {}) or {}),
            "run_id": ctx.run_id,
            "trace_id": ctx.trace_id,
            "active_artifact_id": ctx.active_artifact_id,
            "active_draft_id": ctx.active_draft_id,
            "active_schedule_id": ctx.active_schedule_id,
        },
        trace_context=trace_context.model_dump(mode="json"),
        policy_catalog=policy_catalog,
    )
    execution_input.execution_input_id = ctx.execution_id or execution_input.execution_input_id
    return {
        "run_id": ctx.run_id,
        "trace_id": ctx.trace_id,
        "conversation_id": ctx.conversation_id,
        "user_id": ctx.user_id,
        "tenant_id": ctx.tenant_id,
        "task_id": ctx.task_id,
        "timezone": ctx.timezone,
        "active_artifact_id": ctx.active_artifact_id,
        "active_draft_id": ctx.active_draft_id,
        "active_schedule_id": ctx.active_schedule_id,
        "approval_granted": ctx.approval_granted,
        # Typed execution projection for queue inspectors.  It contains no
        # user message, Intent, or planner request.
        "task_context": {
            "task_id": task_context.task_id if task_context is not None else ctx.task_id,
            "goal": root_goal,
            "constraints": [dict(item) for item in (getattr(task_context, "constraints", ()) or ())],
            "target": _safe_model_payload(getattr(task_context, "target", None)) or None,
            "active_artifact_id": getattr(task_context, "active_artifact_id", None),
            "artifact_refs": [
                _safe_model_payload(item)
                for item in (getattr(task_context, "artifact_refs", ()) or ())
            ],
        } if task_context is not None else None,
        "execution_input": execution_input.model_dump(mode="json"),
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
