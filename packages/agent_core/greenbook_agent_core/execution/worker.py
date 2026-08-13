"""ExecutionWorker — drives an ExecutablePlan to completion.

Phase 4.1: sequential DAG execution with pause/resume/retry.
Phase 5.2: injects upstream artifacts into downstream step constraints.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from greenbook_agent_core.observability.context import TraceContext
from greenbook_agent_core.observability.metrics import MetricsCollector
from greenbook_agent_core.planning.contracts import (
    PlanningDecisionType,
    PlanStep,
    TaskPlan,
)
from greenbook_agent_core.planning.models import ExecutablePlan

from .capability_executor import CapabilityExecutor
from .events import EventType, ExecutionEvent
from .evidence import ExecutionEvidence
from .exceptions import ExecutionBlockedError
from .failure_decision import (
    FailureDecisionEngine,
    FailurePolicyContext,
    RecoveryAction,
    normalize_failure_payload,
)
from .invocation import ExecutionResult
from .models import (
    ArtifactHandle,
    ExecutionControlState,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from .observation import observation_evidence
from .operation_tracking import ExternalOperationTracker
from .recovery import RecoveryPolicy
from .repository import ExecutionRepository
from .retry_decision import RetryDecisionEngine, evidence_from_failure
from .retry_manager import RetryManager
from .retry_scheduler import RetryScheduler
from .runtime_guard import (
    ExecutionBlockedError as RuntimeGuardBlockedError,
)
from .runtime_guard import (
    RuntimeGuard,
)
from .runtime_manager import RuntimeManager
from .scheduler import StepScheduler
from .state_manager import ExecutionStateManager

logger = logging.getLogger(__name__)


def _argument_key_for_resource(kind: Any) -> str | None:
    """Map a referenced resource kind to its canonical tool argument."""

    normalized = str(kind or "").strip().upper()
    return {
        "POST": "post_id",
        "DRAFT": "draft_id",
        "SCHEDULE": "schedule_id",
        "COMMENT": "parent_comment_id",
        "CREATOR_TASK": "strategy_task_id",
        "CREATOR_ARTIFACT": "strategy_artifact_id",
    }.get(normalized)


class RunOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    WAITING_ASYNC = "WAITING_ASYNC"
    WAITING_HUMAN = "WAITING_HUMAN"
    # Backward-compatible name for callers compiled against Phase 11.x.
    # Runtime code must use WAITING_ASYNC so STALLED cannot hide failures.
    STALLED = "STALLED"


class ExecutionWorker:
    """Drive an ExecutablePlan through its capability steps.

    Does NOT own the retry loop (the caller decides whether to re-invoke
    after WAITING_APPROVAL or FAILED_RETRYABLE).  A single call to
    ``run()`` advances the execution as far as it can go in one pass.
    """

    def __init__(
        self,
        executor: CapabilityExecutor,
        repository: ExecutionRepository | None = None,
        trace: Any = None,  # AgentTrace | None
        event_store: Any = None,
        checkpoint_store: Any = None,
        failure_decision_engine: FailureDecisionEngine | None = None,
        operation_tracker: ExternalOperationTracker | None = None,
        trace_context: TraceContext | None = None,
        metrics_collector: MetricsCollector | None = None,
        retry_scheduler: RetryScheduler | None = None,
        replan_callback: Callable[..., Any] | None = None,
        tool_catalog: Sequence[Any] = (),
    ) -> None:
        self._executor = executor
        self._repo = repository or ExecutionRepository()
        self._state = ExecutionStateManager(self._repo, event_store=event_store)
        self._runtime = RuntimeManager(
            self._state,
            checkpoint_store=checkpoint_store,
        )
        self._runtime_guard = RuntimeGuard(self._runtime)
        self._recovery_policy = RecoveryPolicy()
        self._failure_decision_engine = failure_decision_engine or FailureDecisionEngine(
            retry_eligibility=self._recovery_policy.is_retryable_error,
        )
        self._retry_decision_engine = RetryDecisionEngine(
            classifier=self._failure_decision_engine.classifier,
        )
        self._retry_manager = RetryManager(
            state_manager=self._state,
            runtime_manager=self._runtime,
            decision_engine=self._retry_decision_engine,
            metrics_collector=metrics_collector,
        )
        self._operation_tracker = operation_tracker or ExternalOperationTracker()
        self._scheduler = StepScheduler()
        self._trace = trace
        self._trace_context = trace_context
        self._metrics = metrics_collector
        self._retry_scheduler = retry_scheduler
        self._replan_callback = replan_callback
        self._tool_catalog = tuple(tool_catalog)

    def bind_trace_context(self, context: TraceContext) -> None:
        """Bind correlation metadata after the canonical Execution exists."""

        self._trace_context = context
        self._state.bind_trace_context(context.execution_id, context)
        binder = getattr(self._executor, "bind_trace_context", None)
        if callable(binder):
            binder(context)

    def _append_event(
        self,
        event_type: EventType,
        *,
        execution_id: str,
        step_id: str | None = None,
        payload: dict[str, Any] | None = None,
        evidence: ExecutionEvidence | None = None,
        operation_id: str = "",
    ) -> None:
        context = self._trace_context or self._state.trace_context(execution_id)
        if context is not None:
            context = context.for_execution(execution_id)
            if step_id:
                context = context.for_step(step_id)
            if evidence is not None:
                if evidence.invocation_id:
                    context = context.for_invocation(
                        evidence.invocation_id,
                        tool_call_id=evidence.tool_call_id,
                        operation_id=operation_id or evidence.operation_id,
                    )
                else:
                    context = context.with_updates(
                        tool_call_id=evidence.tool_call_id,
                        operation_id=operation_id or evidence.operation_id,
                    )
            elif operation_id:
                context = context.for_operation(operation_id)
        event_payload = dict(payload or {})
        if evidence is not None:
            event_payload.setdefault("evidence", self._evidence_payload(evidence))
        if context is not None:
            event_payload.setdefault(
                "trace_context",
                context.model_dump(mode="json"),
            )
        self._state.event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=event_type,
                step_id=step_id,
                payload=event_payload,
                trace_context=context,
            )
        )

    # ── main entry ───────────────────────────────────────────────

    async def run(self, execution_id: str) -> RunOutcome:
        """Advance *execution_id* as far as possible in one pass.

        Returns the reason the pass stopped.
        """
        ex = self._state._require_execution(execution_id)

        # Ensure execution is started
        if ex.status == ExecutionStatus.PENDING:
            self._state.start_execution(execution_id)

        control_outcome = self._honor_control(execution_id)
        if control_outcome is not None:
            return control_outcome

        # Resume any retryable or crashed steps
        if ex.status not in (
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.PAUSED,
        ):
            ex = self._retry_manager.resume_execution(execution_id)

        # Main loop — sequential (Phase 4.1: no parallel execution)
        while True:
            ex = self._state._require_execution(execution_id)

            control_outcome = self._honor_control(execution_id)
            if control_outcome is not None:
                return control_outcome

            # Terminal?
            if ex.is_terminal:
                if ex.status == ExecutionStatus.COMPLETED:
                    return RunOutcome.COMPLETED
                if ex.status == ExecutionStatus.CANCELLED:
                    return RunOutcome.BLOCKED
                return RunOutcome.FAILED

            # Approval pending?
            if ex.status == ExecutionStatus.WAITING_APPROVAL:
                return RunOutcome.WAITING_APPROVAL
            if ex.status == ExecutionStatus.WAITING_HUMAN:
                return RunOutcome.WAITING_HUMAN

            # Find next ready step
            ready = self._scheduler.get_ready_steps(ex)
            if not ready:
                self._state._update_execution_status(execution_id)
                ex = self._state._require_execution(execution_id)
                if ex.is_terminal:
                    if ex.status == ExecutionStatus.COMPLETED:
                        return RunOutcome.COMPLETED
                    return RunOutcome.FAILED
                if any(s.status == StepStatus.RUNNING for s in ex.steps):
                    return RunOutcome.WAITING_ASYNC
                # A non-terminal execution with no ready step and no running
                # step is a broken dependency/state graph, not a long task.
                # Surface it as a failure instead of leaving RUNNING forever.
                self._state.fail_execution(
                    execution_id,
                    error_code="EXECUTION_STALLED",
                    error_message="No ready step remains for this execution",
                )
                return RunOutcome.FAILED

            # Execute the first ready step (sequential)
            step_ex = ready[0]
            outcome = await self._execute_one_step(ex, step_ex)
            if outcome == RunOutcome.WAITING_APPROVAL:
                return RunOutcome.WAITING_APPROVAL
            if outcome == RunOutcome.WAITING_ASYNC:
                return RunOutcome.WAITING_ASYNC
            if outcome in (RunOutcome.PAUSED, RunOutcome.BLOCKED):
                return outcome

    def _honor_control(self, execution_id: str) -> RunOutcome | None:
        """Stop only at a step boundary and checkpoint before confirming pause."""

        execution = self._state._require_execution(execution_id)
        if (
            execution.control_state == ExecutionControlState.CANCELLED
            or execution.status == ExecutionStatus.CANCELLED
        ):
            return RunOutcome.BLOCKED
        if execution.control_state == ExecutionControlState.PAUSED:
            return RunOutcome.PAUSED
        if execution.control_state == ExecutionControlState.PAUSING:
            checkpoint = self._runtime.save_checkpoint(
                execution_id,
                snapshot={
                    "control_state": ExecutionControlState.PAUSED.value,
                    "reason": execution.control_reason,
                },
            )
            self._append_event(
                EventType.EXECUTION_CHECKPOINT_SAVED,
                execution_id=execution_id,
                payload={
                    "current_step": checkpoint.current_step,
                    "completed_steps": checkpoint.completed_steps,
                    "reason": execution.control_reason,
                },
            )
            self._state.confirm_pause(execution_id)
            return RunOutcome.PAUSED
        if execution.control_state == ExecutionControlState.RESUMING:
            self._state.confirm_resume(execution_id)
        return None

    # ── single-step execution ────────────────────────────────────

    async def _execute_one_step(
        self,
        ex: PlanExecution,
        step_ex: StepExecution,
    ) -> RunOutcome:
        """Execute one StepExecution → update state → return outcome."""
        execution_id = ex.execution_id
        sid = step_ex.step_execution_id
        step_started = time.monotonic()
        try:
            self._runtime_guard.check_execution(execution_id)
        except (RuntimeGuardBlockedError, ExecutionBlockedError) as blocked:
            status = getattr(blocked, "current_status", None)
            if status is None:
                status = getattr(blocked, "status", None)
            if status == ExecutionStatus.PAUSED:
                return RunOutcome.PAUSED
            return RunOutcome.BLOCKED

        # 1. Claim + start (skip if already RUNNING — post-approval)
        replayed = self._replay_checkpointed_success(ex, step_ex)
        if replayed:
            return RunOutcome.COMPLETED
        if step_ex.status == StepStatus.PENDING:
            self._state.start_step(execution_id, sid)
        elif step_ex.status != StepStatus.RUNNING:
            return RunOutcome.WAITING_ASYNC

        # ``ExecutionStateManager`` deliberately returns copies from the
        # repository.  Refresh both objects after the claim so that later
        # checkpoint writes and a dynamic-plan mutation cannot save the
        # pre-claim PENDING snapshot over the durable RUNNING state.
        ex = self._state.get_execution(execution_id)
        step_ex = next(
            (
                candidate
                for candidate in ex.steps
                if candidate.step_execution_id == sid
            ),
            None,
        )
        if step_ex is None:
            raise ValueError(f"Step {sid} disappeared after claim")

        self._append_event(
            EventType.STEP_STARTED,
            execution_id=execution_id,
            step_id=step_ex.step_id,
            payload={"step_execution_id": sid},
        )

        # Trace: STEP_STARTED
        if self._trace is not None:
            self._trace.step_started(step_ex)

        # 2. Build PlanStep — inject transitive upstream artifacts into constraints
        constraints: dict[str, Any] = dict(
            step_ex.checkpoint_data.get("constraints", {})
        )
        # Walk ALL transitive ancestors (not just direct depends_on)
        visited: set[str] = set()
        queue: list[str] = list(step_ex.depends_on)
        while queue:
            dep_id = queue.pop(0)
            if dep_id in visited:
                continue
            visited.add(dep_id)
            upstream = next((s for s in ex.steps if s.step_id == dep_id), None)
            if upstream:
                queue.extend(upstream.depends_on)
                if upstream.output_artifact:
                    art = upstream.output_artifact
                    if art.artifact_type == "DRAFT" and art.resource_id:
                        constraints.setdefault("draft_id", art.resource_id)
                    elif art.artifact_type == "SCHEDULE" and art.resource_id:
                        constraints.setdefault("schedule_id", art.resource_id)
                    for ref in art.resource_refs:
                        key = _argument_key_for_resource(ref.get("kind"))
                        resource_id = str(ref.get("resource_id") or "")
                        if key and resource_id:
                            constraints.setdefault(key, resource_id)

        plan_step = PlanStep(
            step_id=step_ex.step_id,
            capability=step_ex.capability,
            tool_name=step_ex.tool_name,
            ordinal=step_ex.ordinal,
            input_artifact_types=list(step_ex.input_artifact_types),
            output_artifact_type=step_ex.output_artifact_type,
            constraints={**dict(step_ex.arguments or {}), **constraints},
        )

        # 3. Execute
        result: ExecutionResult = await self._executor.execute_step(plan_step)

        # 4. Handle result
        observation = self._observation_for_result(result)
        if result.pending:
            # The tool has acknowledged a long-running task.  Keep the step
            # RUNNING and let the Runtime continuation resume this Worker
            # when ToolRuntime receives the completion callback.
            evidence = self._evidence_from_result(
                result,
                None,
                execution_id=execution_id,
                step_id=step_ex.step_id,
            )
            if evidence is not None:
                self._operation_tracker.observe_pending(
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    tool_name=result.tool_name,
                    evidence=evidence,
                )
            self._record_step_metrics(step_ex, "PENDING", step_started)
            return RunOutcome.WAITING_ASYNC
        if result.ok:
            # Commit the successful tool result into the durable step snapshot
            # before the lifecycle transition.  If the process dies in this
            # narrow window, recovery can reuse the result and never replay a
            # side-effecting tool.
            step_ex.checkpoint_data["completed_tool_result"] = dict(
                result.tool_result or {}
            )
            step_ex.checkpoint_data["idempotency_key"] = (
                step_ex.idempotency_key or ""
            )
            step_ex.checkpoint_data["last_observation"] = observation
            if result.artifact is not None:
                step_ex.checkpoint_data["output_artifact"] = result.artifact.model_dump(
                    mode="json"
                )
            self._repo.save(self._state._require_execution(execution_id))
            evidence = self._evidence_from_result(
                result,
                None,
                execution_id=execution_id,
                step_id=step_ex.step_id,
            )
            operation = None
            if evidence is not None:
                operation = self._operation_tracker.observe_success(
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    tool_name=result.tool_name,
                    evidence=evidence,
                )
            if step_ex.retry_count > 0:
                if self._metrics is not None:
                    retry_context = self._trace_context
                    if retry_context is not None:
                        retry_context = retry_context.for_step(step_ex.step_id)
                    self._metrics.record_retry(success=True, context=retry_context)
                self._append_event(
                    EventType.STEP_RETRY_COMPLETED,
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    payload={
                        "step_execution_id": sid,
                        "retry_count": step_ex.retry_count,
                        "tool_name": result.tool_name,
                    },
                    evidence=evidence,
                    operation_id=operation.operation_id if operation is not None else "",
                )
            self._append_event(
                EventType.STEP_COMPLETED,
                execution_id=execution_id,
                step_id=step_ex.step_id,
                payload={
                    "step_execution_id": sid,
                    "tool_name": result.tool_name,
                    "observation": observation,
                },
                evidence=evidence,
                operation_id=operation.operation_id if operation is not None else "",
            )
            reaction = await self._request_replan(
                ex,
                step_ex,
                result,
                observation,
            ) if observation.get("result_status") == "EMPTY" else None
            if reaction is not None and reaction.decision in {
                PlanningDecisionType.SELECT_ALTERNATIVE_TOOL,
                PlanningDecisionType.RETRY_WITH_NEW_ARGS,
            }:
                self._apply_replan(
                    ex,
                    step_ex,
                    reaction,
                    observation,
                    current_step_completed=True,
                )
            self._state.complete_step(
                execution_id, sid,
                output_artifact=result.artifact,
            )
            if self._trace is not None:
                self._trace.step_completed(step_ex)
            self._record_step_metrics(step_ex, "COMPLETED", step_started)
            if reaction is not None and reaction.decision == PlanningDecisionType.ASK_HUMAN:
                self._state.wait_for_human(
                    execution_id,
                    step_execution_id=sid,
                    reason=reaction.reason or "Runtime evidence requires human input.",
                    payload={"observation": observation},
                )
                return RunOutcome.WAITING_HUMAN
        elif result.approval_required:
            self._state.pause_for_approval(execution_id, sid)
            return RunOutcome.WAITING_APPROVAL
        else:
            failure = self._failure_from_result(result)
            step_ex.checkpoint_data["last_observation"] = observation
            self._repo.save(self._state._require_execution(execution_id))
            decision = self._failure_decision_engine.decide(
                failure,
                FailurePolicyContext(
                    attempt=step_ex.retry_count + 1,
                    retry_budget=max(
                        0,
                        step_ex.max_retries - step_ex.retry_count,
                    ),
                    capability=step_ex.capability,
                    tool_name=result.tool_name or None,
                    has_side_effect=failure.side_effect_state.value in {
                        "POSSIBLE",
                        "UNKNOWN",
                        "CONFIRMED",
                    },
                    idempotent=bool(failure.idempotency_key),
                    idempotency_key=failure.idempotency_key,
                    supports_reconciliation=bool(failure.receipt_id),
                ),
            )
            evidence = self._evidence_from_result(
                result,
                failure,
                execution_id=execution_id,
                step_id=step_ex.step_id,
            )
            operation = None
            if evidence is not None:
                operation = self._operation_tracker.observe_failure(
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    tool_name=result.tool_name,
                    evidence=evidence,
                    failure=failure,
                )
            retry_decision = self._retry_decision_engine.decide_for_step(
                failure,
                step_ex,
                evidence=evidence,
                source="worker_failure",
            )

            if decision.action == RecoveryAction.REQUEST_USER_INPUT:
                self._state.pause_execution(execution_id)
                self._record_step_metrics(step_ex, "FAILED", step_started)
                return RunOutcome.PAUSED

            if retry_decision.allowed:
                failed_step = self._state.fail_step(
                    execution_id, sid,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
                retry_task = None
                if (
                    failed_step.status == StepStatus.FAILED_RETRYABLE
                    and self._retry_scheduler is not None
                ):
                    retry_task = self._retry_scheduler.schedule_decision(
                        execution_id=execution_id,
                        step_id=step_ex.step_id,
                        decision=retry_decision,
                        reason=result.error_code,
                    )
                self._append_event(
                    EventType.STEP_FAILED,
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    payload={
                        "step_execution_id": sid,
                        "retryable": failed_step.status == StepStatus.FAILED_RETRYABLE,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "failure_category": decision.category.value,
                        "recovery_action": decision.action.value,
                        "recovery_reason": decision.reason,
                        "retry_decision": retry_decision.model_dump(mode="json"),
                        "retry_task_id": retry_task.task_id if retry_task else None,
                        "tool_name": result.tool_name,
                        "observation": observation,
                    },
                    evidence=evidence,
                    operation_id=operation.operation_id if operation is not None else "",
                )
                if failed_step.status == StepStatus.FAILED:
                    self._append_event(
                        EventType.STEP_RETRY_EXHAUSTED,
                        execution_id=execution_id,
                        step_id=step_ex.step_id,
                        payload={
                            "step_execution_id": sid,
                            "retry_count": failed_step.retry_count,
                            "reason": result.error_code,
                            "tool_name": result.tool_name,
                        },
                        evidence=evidence,
                        operation_id=operation.operation_id if operation is not None else "",
                    )
                    # Exhausting the ordinary retry budget is itself runtime
                    # evidence.  Give DynamicPlanner one opportunity to
                    # choose a safe read-only alternative before the
                    # execution is finalized as failed.
                    ex = self._state.get_execution(execution_id)
                    step_ex = next(
                        (
                            candidate
                            for candidate in ex.steps
                            if candidate.step_execution_id == sid
                        ),
                        step_ex,
                    )
                    reaction = await self._request_replan(
                        ex,
                        step_ex,
                        result,
                        observation,
                    )
                    if reaction is not None and reaction.decision in {
                        PlanningDecisionType.SELECT_ALTERNATIVE_TOOL,
                        PlanningDecisionType.RETRY_WITH_NEW_ARGS,
                    }:
                        self._apply_replan(
                            ex,
                            step_ex,
                            reaction,
                            observation,
                            current_step_completed=False,
                        )
                        self._record_step_metrics(step_ex, "FAILED_REPLANNED", step_started)
                        return RunOutcome.COMPLETED
                    if reaction is not None and reaction.decision == PlanningDecisionType.ASK_HUMAN:
                        self._state.wait_for_human(
                            execution_id,
                            step_execution_id=sid,
                            reason=reaction.reason or "Runtime failure requires human input.",
                            payload={"observation": observation},
                        )
                        self._record_step_metrics(step_ex, "FAILED_WAITING_HUMAN", step_started)
                        return RunOutcome.WAITING_HUMAN
            else:
                # Permanent failure - mark downstream SKIPPED.
                if self._trace is not None:
                    self._trace.step_failed(step_ex, result.error_message)
                self._append_event(
                    EventType.STEP_FAILED,
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    payload={
                        "step_execution_id": sid,
                        "retryable": False,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "failure_category": decision.category.value,
                        "recovery_action": decision.action.value,
                        "recovery_reason": decision.reason,
                        "retry_decision": retry_decision.model_dump(mode="json"),
                        "tool_name": result.tool_name,
                        "observation": observation,
                    },
                    evidence=evidence,
                    operation_id=operation.operation_id if operation is not None else "",
                )
                self._state.fail_step(
                    execution_id, sid,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    permanent=True,
                )
                # ``fail_step`` also persists a new snapshot.  Re-read it
                # before appending a replacement step, otherwise saving the
                # old execution object would resurrect the failed step as
                # RUNNING and lose the failure transition.
                ex = self._state.get_execution(execution_id)
                step_ex = next(
                    (
                        candidate
                        for candidate in ex.steps
                        if candidate.step_execution_id == sid
                    ),
                    step_ex,
                )
                reaction = await self._request_replan(
                    ex,
                    step_ex,
                    result,
                    observation,
                )
                if reaction is not None and reaction.decision in {
                    PlanningDecisionType.SELECT_ALTERNATIVE_TOOL,
                    PlanningDecisionType.RETRY_WITH_NEW_ARGS,
                }:
                    self._apply_replan(
                        ex,
                        step_ex,
                        reaction,
                        observation,
                        current_step_completed=False,
                    )
                    self._record_step_metrics(step_ex, "FAILED_REPLANNED", step_started)
                    return RunOutcome.COMPLETED
                if reaction is not None and reaction.decision == PlanningDecisionType.ASK_HUMAN:
                    self._state.wait_for_human(
                        execution_id,
                        step_execution_id=sid,
                        reason=reaction.reason or "Runtime failure requires human input.",
                        payload={"observation": observation},
                    )
                    self._record_step_metrics(step_ex, "FAILED_WAITING_HUMAN", step_started)
                    return RunOutcome.WAITING_HUMAN
                ex_store = self._repo.find_by_id(execution_id)
                if ex_store is not None:
                    skipped = self._scheduler.mark_skipped_downstream(
                        ex_store, step_ex.step_id)
                    if skipped:
                        self._repo.save(ex_store)
                        self._state._update_execution_status(execution_id)
                self._record_step_metrics(step_ex, "FAILED_RETRYABLE", step_started)

            if not retry_decision.allowed:
                self._record_step_metrics(step_ex, "FAILED", step_started)

        return RunOutcome.COMPLETED  # step-level done

    def _observation_for_result(self, result: ExecutionResult) -> dict[str, Any]:
        payload = dict(result.tool_result or {})
        payload.update(
            {
                "ok": result.ok,
                "tool_name": result.tool_name,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "request_sent": result.request_sent,
            }
        )
        return observation_evidence(
            payload,
            available_tools=self._tool_catalog,
            failed_tool=result.tool_name,
        )

    async def _request_replan(
        self,
        execution: PlanExecution,
        step: StepExecution,
        result: ExecutionResult,
        observation: Mapping[str, Any],
    ) -> Any | None:
        if self._replan_callback is None:
            return None
        try:
            decision = self._replan_callback(
                execution,
                step,
                result,
                dict(observation),
            )
            if inspect.isawaitable(decision):
                decision = await decision
            return decision
        except Exception:
            logger.exception(
                "Dynamic replanning failed execution_id=%s step_id=%s",
                execution.execution_id,
                step.step_id,
            )
            from greenbook_agent_core.planning.contracts import PlanningDecision

            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason="The runtime could not safely evaluate the next plan step.",
            )

    def _apply_replan(
        self,
        execution: PlanExecution,
        step: StepExecution,
        decision: Any,
        observation: Mapping[str, Any],
        *,
        current_step_completed: bool,
    ) -> None:
        """Apply only a validated read-only alternative at the Worker boundary."""

        from greenbook_agent_core.planning.contracts import PlanningDecisionType

        # The caller may have just transitioned the source step (claim,
        # completion, or failure).  Always mutate the latest durable snapshot
        # instead of a stale scheduler copy.
        execution = self._repo.find_by_id(execution.execution_id) or execution

        tool_name = str(decision.tool_name or "")
        if decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS and not tool_name:
            tool_name = step.tool_name
        metadata = next(
            (item for item in self._tool_catalog if str(getattr(item, "name", "")) == tool_name),
            None,
        )
        policy = getattr(metadata, "policy", None)
        side_effect = getattr(policy, "side_effect", None)
        if (
            metadata is None
            or bool(getattr(policy, "requires_approval", False))
            or bool(getattr(side_effect, "has_side_effect", False))
            or bool(getattr(side_effect, "destructive", False))
        ):
            self._state.wait_for_human(
                execution.execution_id,
                step_execution_id=step.step_execution_id,
                reason="The proposed alternative is not a safe read-only operation.",
                payload={"observation": dict(observation), "tool_name": tool_name},
            )
            return

        registry = getattr(self._executor, "_registry", None)
        capability = getattr(metadata, "capabilities", ())
        capability_name = str(capability[0] if capability else "")
        capability_model = (
            registry.find_by_tool(tool_name)
            if registry is not None and hasattr(registry, "find_by_tool")
            else None
        )
        if capability_model is not None:
            capability_name = capability_model.name
        if not capability_name:
            self._state.wait_for_human(
                execution.execution_id,
                step_execution_id=step.step_execution_id,
                reason="The planner selected a tool without a known capability contract.",
                payload={"observation": dict(observation), "tool_name": tool_name},
            )
            return

        output_artifact_type = str(
            getattr(capability_model, "output_artifact_type", "") or ""
        )
        new_step_id = f"replan-{step.step_id}-{uuid.uuid4().hex[:10]}"
        source_step = next(
            (
                candidate
                for candidate in execution.steps
                if candidate.step_execution_id == step.step_execution_id
            ),
            step,
        )
        source_step.checkpoint_data["superseded_by"] = new_step_id
        source_step.checkpoint_data["supersession_reason"] = decision.reason
        dependency_ids = [step.step_id] if current_step_completed else list(step.depends_on)
        replacement = StepExecution(
            execution_id=execution.execution_id,
            step_id=new_step_id,
            capability=capability_name,
            tool_name=tool_name,
            arguments=dict(decision.arguments or {}),
            idempotency_key=f"{execution.task_id}:{execution.plan_id}:{new_step_id}",
            execution_mode=step.execution_mode,
            policy_snapshot=(
                policy.model_dump(mode="json") if hasattr(policy, "model_dump") else {}
            ),
            ordinal=max((item.ordinal for item in execution.steps), default=-1) + 1,
            input_artifact_types=list(getattr(capability_model, "inputs", None).required)
            if capability_model is not None and getattr(capability_model, "inputs", None) is not None
            else [],
            output_artifact_type=output_artifact_type,
            depends_on=dependency_ids,
            max_retries=max(1, int(getattr(getattr(policy, "retry_policy", None), "max_attempts", 1))),
            checkpoint_data={
                "constraints": dict(decision.arguments or {}),
                "last_observation": dict(observation),
                "replan": {
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "source_step_id": step.step_id,
                },
            },
        )
        for candidate in execution.steps:
            if candidate.status == StepStatus.PENDING and step.step_id in candidate.depends_on:
                candidate.depends_on = [
                    new_step_id if dependency == step.step_id else dependency
                    for dependency in candidate.depends_on
                ]
        execution.steps.append(replacement)
        execution.steps.sort(key=lambda item: item.ordinal)
        if execution.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}:
            execution.status = ExecutionStatus.RUNNING
            execution.completed_at = ""
        execution.updated_at = _now()
        execution.version += 1
        self._repo.save(execution)
        self._append_event(
            EventType.EXECUTION_PLAN_REVISED,
            execution_id=execution.execution_id,
            step_id=step.step_id,
            payload={
                "decision": decision.decision.value,
                "reason": decision.reason,
                "observation": dict(observation),
                "previous_step_id": step.step_id,
                "replacement_step_id": new_step_id,
                "replacement_tool": tool_name,
                "plan_step_ids": [item.step_id for item in execution.steps],
            },
        )

    def _record_step_metrics(
        self,
        step: StepExecution,
        status: str,
        started_at: float,
    ) -> None:
        if self._metrics is None:
            return
        context = self._trace_context
        if context is not None:
            context = context.for_step(step.step_id)
        self._metrics.record_step(
            status=status,
            latency_ms=(time.monotonic() - started_at) * 1000.0,
            context=context,
        )

    # ── resume after approval ────────────────────────────────────

    @staticmethod
    def _failure_from_result(result: ExecutionResult):
        """Recover a failure fact for legacy callers without the envelope."""

        if result.external_failure is not None:
            return result.external_failure

        _, failure = normalize_failure_payload(
            result.tool_result,
            error_code=result.error_code,
            error_message=result.error_message,
            retryable=result.retryable,
            request_sent=result.request_sent,
        )
        return failure

    @staticmethod
    def _evidence_from_result(
        result: ExecutionResult,
        failure,
        *,
        execution_id: str,
        step_id: str,
    ) -> ExecutionEvidence | None:
        evidence = result.evidence or (
            evidence_from_failure(failure) if failure is not None else None
        )
        if evidence is None:
            return None
        updates: dict[str, str] = {}
        if evidence.execution_id is None:
            updates["execution_id"] = execution_id
        if evidence.step_id is None:
            updates["step_id"] = step_id
        return evidence.model_copy(update=updates) if updates else evidence

    @staticmethod
    def _evidence_payload(evidence: ExecutionEvidence | None) -> dict[str, Any] | None:
        if evidence is None:
            return None
        return evidence.model_dump(mode="json")

    async def resume_after_approval(self, execution_id: str) -> RunOutcome:
        """Resume a WAITING_APPROVAL execution after user approves."""
        ex = self._state._require_execution(execution_id)
        if ex.status != ExecutionStatus.WAITING_APPROVAL:
            return RunOutcome.FAILED

        # Find the WAITING_APPROVAL step
        waiting = next(
            (s for s in ex.steps if s.status == StepStatus.WAITING_APPROVAL),
            None,
        )
        if waiting is None:
            return RunOutcome.FAILED

        # TODO: Phase 4.2 — check approval decision from DB
        # For now, auto-approve (test-friendly)
        self._state.approve_and_resume(execution_id, waiting.step_execution_id)

        # Execute the resumed step
        step_ex = self._state._repo.find_step(execution_id, waiting.step_execution_id)
        if step_ex is None:
            return RunOutcome.FAILED
        return await self._execute_one_step(
            self._state._require_execution(execution_id),
            step_ex,
        )

    def _replay_checkpointed_success(
        self,
        execution: PlanExecution,
        step: StepExecution,
    ) -> bool:
        """Finalize a tool result durably recorded before a process crash."""

        cached = step.checkpoint_data.get("completed_tool_result")
        if not isinstance(cached, dict) or not bool(cached.get("ok", True)):
            return False
        if step.status == StepStatus.COMPLETED:
            return True
        if step.status == StepStatus.PENDING:
            self._state.start_step(execution.execution_id, step.step_execution_id)
        if step.status not in {StepStatus.RUNNING, StepStatus.PENDING}:
            return False
        artifact = step.checkpoint_data.get("output_artifact")
        output_artifact = None
        if isinstance(artifact, dict):
            output_artifact = ArtifactHandle.model_validate(artifact)
        self._state.complete_step(
            execution.execution_id,
            step.step_execution_id,
            output_artifact=output_artifact,
        )
        self._append_event(
            EventType.STEP_COMPLETED,
            execution_id=execution.execution_id,
            step_id=step.step_id,
            payload={"replayed": True, "idempotency_key": step.idempotency_key},
        )
        return True

    # ── helpers ─────────────────────────────────────────────────

    def init_from_plan(
        self,
        executable: ExecutablePlan,
        task_id: str = "",
    ) -> PlanExecution:
        """Create + persist a PlanExecution from an ExecutablePlan."""
        plan = TaskPlan(
            plan_id=executable.plan_id,
            task_id=task_id,
            plan_source=executable.plan_source,
            steps=executable.steps,
        )
        return self._state.init_execution(plan, executable)


def _now() -> str:
    return datetime.now(UTC).isoformat()
