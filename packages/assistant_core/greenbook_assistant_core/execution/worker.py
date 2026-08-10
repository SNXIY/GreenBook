"""ExecutionWorker — drives an ExecutablePlan to completion.

Phase 4.1: sequential DAG execution with pause/resume/retry.
Phase 5.2: injects upstream artifacts into downstream step constraints.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
from greenbook_assistant_core.planning.models import ExecutablePlan
from greenbook_assistant_core.observability.context import TraceContext

from .capability_executor import CapabilityExecutor
from .events import EventType, ExecutionEvent
from .exceptions import ExecutionBlockedError
from .failure_decision import (
    FailureDecisionEngine,
    FailurePolicyContext,
    RecoveryAction,
    normalize_failure_payload,
)
from .evidence import ExecutionEvidence
from .invocation import ExecutionResult
from .models import ExecutionStatus, PlanExecution, StepExecution, StepStatus
from .operation_tracking import ExternalOperationTracker
from .recovery import RecoveryPolicy
from .retry_decision import RetryDecisionEngine, evidence_from_failure
from .retry_manager import RetryManager
from .repository import ExecutionRepository
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


class RunOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    WAITING_ASYNC = "WAITING_ASYNC"
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
        )
        self._operation_tracker = operation_tracker or ExternalOperationTracker()
        self._scheduler = StepScheduler()
        self._trace = trace
        self._trace_context = trace_context

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

        # Resume any retryable or crashed steps
        if ex.status not in (
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.PAUSED,
        ):
            ex = self._retry_manager.resume_execution(execution_id)

        # Main loop — sequential (Phase 4.1: no parallel execution)
        while True:
            ex = self._state._require_execution(execution_id)

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

    # ── single-step execution ────────────────────────────────────

    async def _execute_one_step(
        self,
        ex: PlanExecution,
        step_ex: StepExecution,
    ) -> RunOutcome:
        """Execute one StepExecution → update state → return outcome."""
        execution_id = ex.execution_id
        sid = step_ex.step_execution_id
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
        if step_ex.status == StepStatus.PENDING:
            self._state.start_step(execution_id, sid)
        elif step_ex.status != StepStatus.RUNNING:
            return RunOutcome.WAITING_ASYNC

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

        plan_step = PlanStep(
            step_id=step_ex.step_id,
            capability=step_ex.capability,
            ordinal=step_ex.ordinal,
            input_artifact_types=list(step_ex.input_artifact_types),
            output_artifact_type=step_ex.output_artifact_type,
            constraints=constraints,
        )

        # 3. Execute
        result: ExecutionResult = await self._executor.execute_step(plan_step)

        # 4. Handle result
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
            return RunOutcome.WAITING_ASYNC
        if result.ok:
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
                },
                evidence=evidence,
                operation_id=operation.operation_id if operation is not None else "",
            )
            self._state.complete_step(
                execution_id, sid,
                output_artifact=result.artifact,
            )
            if self._trace is not None:
                self._trace.step_completed(step_ex)
        elif result.approval_required:
            self._state.pause_for_approval(execution_id, sid)
            return RunOutcome.WAITING_APPROVAL
        else:
            failure = self._failure_from_result(result)
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
                return RunOutcome.PAUSED

            if retry_decision.allowed:
                self._append_event(
                    EventType.STEP_FAILED,
                    execution_id=execution_id,
                    step_id=step_ex.step_id,
                    payload={
                        "step_execution_id": sid,
                        "retryable": True,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                        "failure_category": decision.category.value,
                        "recovery_action": decision.action.value,
                        "recovery_reason": decision.reason,
                        "retry_decision": retry_decision.model_dump(mode="json"),
                        "tool_name": result.tool_name,
                    },
                    evidence=evidence,
                    operation_id=operation.operation_id if operation is not None else "",
                )
                failed_step = self._state.fail_step(
                    execution_id, sid,
                    error_code=result.error_code,
                    error_message=result.error_message,
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
                ex_store = self._repo.find_by_id(execution_id)
                if ex_store is not None:
                    skipped = self._scheduler.mark_skipped_downstream(
                        ex_store, step_ex.step_id)
                    if skipped:
                        self._repo.save(ex_store)
                        self._state._update_execution_status(execution_id)

        return RunOutcome.COMPLETED  # step-level done

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
            template_name=executable.template_name,
            steps=executable.steps,
        )
        return self._state.init_execution(plan, executable)
