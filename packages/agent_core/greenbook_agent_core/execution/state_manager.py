"""ExecutionStateManager — state transitions, resume, approval handling.

Phase 3.3: state management only — no tool execution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from greenbook_agent_core.observability.context import TraceContext
from greenbook_agent_core.planning.contracts import TaskPlan
from greenbook_agent_core.planning.models import ExecutablePlan

from .event_store import ExecutionEventStore
from .events import EventType, ExecutionEvent
from .models import (
    ArtifactHandle,
    ExecutionControlState,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from .repository import ExecutionRepository

_default_event_store = ExecutionEventStore()


class ExecutionStateManager:
    """Manage PlanExecution lifecycle — init, advance, pause, resume."""

    def __init__(
        self,
        repository: ExecutionRepository | None = None,
        event_store: ExecutionEventStore | None = None,
    ) -> None:
        self._repo = repository or ExecutionRepository()
        self._event_store = event_store or _default_event_store
        self._trace_contexts: dict[str, TraceContext] = {}

    @property
    def event_store(self) -> ExecutionEventStore:
        return self._event_store

    def bind_trace_context(
        self,
        execution_id: str,
        context: TraceContext,
    ) -> None:
        """Associate observability metadata without changing Execution state."""

        self._trace_contexts[execution_id] = context.for_execution(execution_id)

    def trace_context(self, execution_id: str) -> TraceContext | None:
        """Return local context or recover it from a prior durable event."""

        context = self._trace_contexts.get(execution_id)
        if context is not None:
            return context
        for event in reversed(self._event_store.list_events(execution_id)):
            if event.trace_context is not None:
                self._trace_contexts[execution_id] = event.trace_context
                return event.trace_context
            raw_context = (event.payload or {}).get("trace_context")
            if raw_context is not None:
                try:
                    context = TraceContext.model_validate(raw_context)
                except (TypeError, ValueError):
                    continue
                self._trace_contexts[execution_id] = context
                return context
        return None

    # ── initialisation ───────────────────────────────────────────

    def init_execution(
        self,
        plan: TaskPlan,
        executable: ExecutablePlan,
        objective_id: str | None = None,
        *,
        execution_id: str | None = None,
        dispatch_payload: dict[str, object] | None = None,
    ) -> PlanExecution:
        """Create a PlanExecution from a validated ExecutablePlan."""
        initial_checkpoint = (
            {"dispatch_payload": dict(dispatch_payload)}
            if dispatch_payload
            else {}
        )
        execution = PlanExecution(
            execution_id=execution_id or str(uuid.uuid4()),
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            status=ExecutionStatus.PENDING,
            objective_id=objective_id,
            requires_approval=executable.requires_approval,
            has_side_effects=executable.has_side_effects,
            steps=[
                StepExecution(
                    step_id=s.step_id,
                    goal_id=str(getattr(s, "goal_id", "") or "") or None,
                    capability=s.capability,
                    tool_name=str(getattr(s, "tool_name", "") or ""),
                    arguments=dict(getattr(s, "constraints", {}) or {}),
                    idempotency_key=(
                        f"{plan.task_id}:{plan.plan_id}:{s.step_id}"
                    ),
                    execution_mode="QUEUE",
                    policy_snapshot={},
                    ordinal=s.ordinal,
                    status=StepStatus.PENDING,
                    input_artifact_types=list(s.input_artifact_types),
                    output_artifact_type=s.output_artifact_type,
                    depends_on=list(s.depends_on),
                    checkpoint_data=(
                        dict(initial_checkpoint) if index == 0 else {}
                    ),
                )
                for index, s in enumerate(plan.steps)
            ],
        )
        return self._repo.save(execution)

    # ── step transitions ────────────────────────────────────────

    def start_step(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution:
        """PENDING → RUNNING."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status != StepStatus.PENDING:
            raise _invalid_transition(step, StepStatus.PENDING, StepStatus.RUNNING)
        return self._update_and_return(
            execution_id,
            step_execution_id,
            expected_status=StepStatus.PENDING,
            status=StepStatus.RUNNING,
            started_at=_now(),
        )

    def complete_step(
        self,
        execution_id: str,
        step_execution_id: str,
        output_artifact: ArtifactHandle | None = None,
    ) -> StepExecution:
        """RUNNING → COMPLETED."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status != StepStatus.RUNNING:
            raise _invalid_transition(step, StepStatus.RUNNING, StepStatus.COMPLETED)
        fields: dict[str, object] = {
            "status": StepStatus.COMPLETED,
            "completed_at": _now(),
        }
        if output_artifact is not None:
            fields["output_artifact"] = output_artifact
        result = self._update_and_return(
            execution_id,
            step_execution_id,
            expected_status=StepStatus.RUNNING,
            **fields,
        )
        self._update_execution_status(execution_id)
        return result

    def reconcile_step_succeeded(
        self,
        execution_id: str,
        step_execution_id: str,
        *,
        operation_id: str = "",
    ) -> StepExecution:
        """Mark an externally confirmed operation as completed.

        This is intentionally separate from ``complete_step``: reconciliation
        proves an external side effect after Runtime uncertainty, so it must
        not pretend that the local Worker just executed the tool.
        """

        step = self._require_step(execution_id, step_execution_id)
        if step.status == StepStatus.COMPLETED:
            # A crash may happen after the step commit but before the parent
            # Execution status is recomputed. Re-open only this durable
            # reconciliation path so a stale WAITING_HUMAN marker cannot keep
            # an already verified operation non-terminal forever.
            self._prepare_reconciled_execution(execution_id)
            self._update_execution_status(execution_id)
            return step
        if step.status == StepStatus.SKIPPED:
            raise _invalid_transition(step, "non-SKIPPED", StepStatus.COMPLETED)
        result = self._update_and_return(
            execution_id,
            step_execution_id,
            status=StepStatus.COMPLETED,
            error_code="",
            error_message="",
            completed_at=_now(),
        )
        self._prepare_reconciled_execution(execution_id)
        self._update_execution_status(execution_id)
        self._emit(
            execution_id,
            EventType.STEP_RECONCILIATION_SUCCEEDED,
            step_id=step.step_id,
            payload={"operation_id": operation_id},
        )
        return result

    def reconcile_step_failed(
        self,
        execution_id: str,
        step_execution_id: str,
        *,
        error_code: str,
        error_message: str,
        operation_id: str = "",
    ) -> StepExecution:
        """Record a confirmed external failure without scheduling a retry."""

        step = self._require_step(execution_id, step_execution_id)
        if step.status == StepStatus.COMPLETED:
            return step
        result = self._update_and_return(
            execution_id,
            step_execution_id,
            status=StepStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            completed_at=_now(),
        )
        self._prepare_reconciled_execution(execution_id)
        self._update_execution_status(execution_id)
        self._emit(
            execution_id,
            EventType.STEP_RECONCILIATION_FAILED,
            step_id=step.step_id,
            payload={
                "operation_id": operation_id,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        return result

    def mark_reconciliation_required(
        self,
        execution_id: str,
        step_execution_id: str,
        *,
        operation_id: str = "",
    ) -> PlanExecution:
        """Keep the step fact unchanged and route the execution to a human."""

        ex = self._require_execution(execution_id)
        if ex.status in (ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED):
            return ex
        ex.status = ExecutionStatus.WAITING_HUMAN
        ex.updated_at = _now()
        result = self._repo.save(ex)
        self._emit(
            execution_id,
            EventType.EXECUTION_RECONCILIATION_REQUIRED,
            step_id=self._require_step(execution_id, step_execution_id).step_id,
            payload={"operation_id": operation_id},
        )
        return result

    def wait_for_human(
        self,
        execution_id: str,
        *,
        step_execution_id: str = "",
        reason: str = "Runtime evidence requires human input.",
        payload: dict[str, object] | None = None,
    ) -> PlanExecution:
        """Route a safe-but-undecidable execution to a human checkpoint."""

        ex = self._require_execution(execution_id)
        if ex.status in (ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED):
            return ex
        ex.status = ExecutionStatus.WAITING_HUMAN
        ex.updated_at = _now()
        result = self._repo.save(ex)
        event_payload = {"reason": reason}
        if payload:
            event_payload.update(payload)
        self._emit(
            execution_id,
            EventType.EXECUTION_WAITING_HUMAN,
            step_id=(
                self._require_step(execution_id, step_execution_id).step_id
                if step_execution_id
                else None
            ),
            payload=event_payload,
        )
        return result

    def fail_step(
        self,
        execution_id: str,
        step_execution_id: str,
        error_code: str = "",
        error_message: str = "",
        *,
        permanent: bool = False,
    ) -> StepExecution:
        """RUNNING or FAILED_RETRYABLE → FAILED_RETRYABLE or FAILED.

        When *permanent* is True the step is marked FAILED immediately
        (no retries).  Otherwise the retry counter is checked.
        """
        step = self._require_step(execution_id, step_execution_id)
        if step.status not in (StepStatus.RUNNING, StepStatus.FAILED_RETRYABLE):
            raise _invalid_transition(step, f"{StepStatus.RUNNING}/{StepStatus.FAILED_RETRYABLE}", "FAILED*")

        retry_count = step.retry_count + 1
        if permanent or retry_count >= step.max_retries:
            new_status = StepStatus.FAILED
        else:
            new_status = StepStatus.FAILED_RETRYABLE

        result = self._update_and_return(
            execution_id, step_execution_id,
            status=new_status,
            retry_count=retry_count,
            error_code=error_code,
            error_message=error_message,
            completed_at=_now(),
        )
        self._update_execution_status(execution_id)
        return result

    def retry_step(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution:
        """Reset one failed step for an explicit later Worker pass."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status not in (StepStatus.FAILED, StepStatus.FAILED_RETRYABLE):
            raise _invalid_transition(step, "FAILED*", StepStatus.PENDING)
        result = self._update_and_return(
            execution_id,
            step_execution_id,
            status=StepStatus.PENDING,
            error_code="",
            error_message="",
            completed_at="",
        )
        ex = self._require_execution(execution_id)
        if ex.status == ExecutionStatus.FAILED:
            ex.status = ExecutionStatus.RUNNING
            ex.updated_at = _now()
            self._repo.save(ex)
        elif ex.status not in (ExecutionStatus.PAUSED, ExecutionStatus.WAITING_HUMAN):
            ex.updated_at = _now()
            self._repo.save(ex)
        return result

    def recover_execution(self, execution_id: str) -> PlanExecution:
        """Normalize interrupted/retryable steps after a process restart."""
        ex = self._require_execution(execution_id)
        for step in ex.steps:
            if step.status in (StepStatus.RUNNING, StepStatus.FAILED_RETRYABLE):
                self._update_and_return(
                    execution_id,
                    step.step_execution_id,
                    status=StepStatus.PENDING,
                    error_code="" if step.status == StepStatus.FAILED_RETRYABLE else step.error_code,
                    error_message="" if step.status == StepStatus.FAILED_RETRYABLE else step.error_message,
                    started_at="" if step.status == StepStatus.RUNNING else step.started_at,
                )
        return self._require_execution(execution_id)

    def recover_step(self, execution_id: str, step_execution_id: str) -> StepExecution:
        """Reset an interrupted RUNNING step after a process restart."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status != StepStatus.RUNNING:
            return step
        return self._update_and_return(
            execution_id,
            step_execution_id,
            expected_status=StepStatus.RUNNING,
            status=StepStatus.PENDING,
            started_at="",
        )

    def pause_for_approval(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution:
        """RUNNING → WAITING_APPROVAL."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status != StepStatus.RUNNING:
            raise _invalid_transition(step, StepStatus.RUNNING,
                                      StepStatus.WAITING_APPROVAL)
        result = self._update_and_return(
            execution_id, step_execution_id,
            expected_status=StepStatus.RUNNING,
            status=StepStatus.WAITING_APPROVAL,
        )
        self._emit(
            execution_id,
            EventType.APPROVAL_REQUIRED,
            step_id=step.step_id,
        )
        self._update_execution_status(execution_id)
        return result

    def approve_and_resume(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution:
        """WAITING_APPROVAL → RUNNING (resume after approval)."""
        step = self._require_step(execution_id, step_execution_id)
        if step.status != StepStatus.WAITING_APPROVAL:
            raise _invalid_transition(step, StepStatus.WAITING_APPROVAL,
                                      StepStatus.RUNNING)
        result = self._update_and_return(
            execution_id, step_execution_id,
            expected_status=StepStatus.WAITING_APPROVAL,
            status=StepStatus.PENDING,
            started_at="",
        )
        self._update_execution_status(execution_id)
        return result

    # ── execution-level transitions ──────────────────────────────

    def start_execution(self, execution_id: str) -> PlanExecution:
        """PENDING → RUNNING."""
        ex = self._require_execution(execution_id)
        if ex.status != ExecutionStatus.PENDING:
            raise _invalid_transition(ex, ExecutionStatus.PENDING,
                                      ExecutionStatus.RUNNING)
        ex.status = ExecutionStatus.RUNNING
        ex.updated_at = _now()
        result = self._repo.save(ex)
        self._emit(execution_id, EventType.EXECUTION_STARTED)
        return result

    def get_execution(self, execution_id: str) -> PlanExecution:
        """Return a copy of the current execution state."""
        return self._require_execution(execution_id)

    def list_steps(self, execution_id: str) -> list[StepExecution]:
        """Return copies of all steps in plan order."""
        execution = self._require_execution(execution_id)
        return sorted(
            [step.model_copy(deep=True) for step in execution.steps],
            key=lambda step: step.ordinal,
        )

    def list_executions(self) -> list[PlanExecution]:
        """Return all persisted execution state snapshots."""
        return [execution.model_copy(deep=True) for execution in self._repo.list_all()]

    def pause_execution(self, execution_id: str, *, reason: str = "User requested pause") -> PlanExecution:
        """Request a safe-boundary pause; Worker confirms it after checkpointing."""
        ex = self._require_execution(execution_id)
        if ex.control_state in {
            ExecutionControlState.PAUSING,
            ExecutionControlState.PAUSED,
        }:
            return ex
        if ex.status not in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
            raise ValueError(
                f"Invalid transition: {ex.status.value} -> {ExecutionStatus.PAUSED.value}; "
                "expected PENDING or RUNNING"
            )
        now = _now()
        ex.control_state = ExecutionControlState.PAUSING
        ex.control_reason = reason.strip() or "User requested pause"
        ex.control_requested_at = now
        ex.control_updated_at = now
        ex.updated_at = now
        result = self._repo.save(ex)
        self._emit(
            execution_id,
            EventType.EXECUTION_PAUSE_REQUESTED,
            payload={"reason": ex.control_reason},
        )
        return result

    def confirm_pause(
        self,
        execution_id: str,
        *,
        checkpoint_id: int | str | None = None,
    ) -> PlanExecution:
        """Persist the Worker-confirmed pause after a durable checkpoint exists."""

        ex = self._require_execution(execution_id)
        if ex.control_state == ExecutionControlState.PAUSED:
            return ex
        if ex.control_state != ExecutionControlState.PAUSING:
            raise ValueError(
                f"Execution {execution_id} is not waiting to pause: {ex.control_state.value}"
            )
        now = _now()
        ex.control_state = ExecutionControlState.PAUSED
        ex.status = ExecutionStatus.PAUSED
        ex.control_updated_at = now
        ex.updated_at = now
        result = self._repo.save(ex)
        self._emit(
            execution_id,
            EventType.EXECUTION_PAUSED,
            payload={
                "reason": ex.control_reason,
                "checkpoint_id": checkpoint_id,
            },
        )
        return result

    def confirm_resume(self, execution_id: str) -> PlanExecution:
        """Confirm that a Worker reclaimed a resumed execution."""

        ex = self._require_execution(execution_id)
        if ex.control_state == ExecutionControlState.RUNNING:
            return ex
        if ex.control_state != ExecutionControlState.RESUMING:
            raise ValueError(
                f"Execution {execution_id} is not resuming: {ex.control_state.value}"
            )
        now = _now()
        ex.control_state = ExecutionControlState.RUNNING
        ex.status = ExecutionStatus.RUNNING
        ex.control_updated_at = now
        ex.updated_at = now
        result = self._repo.save(ex)
        self._emit(execution_id, EventType.EXECUTION_RESUMED)
        return result

    def cancel_execution(
        self,
        execution_id: str,
        *,
        reason: str = "User requested cancellation",
    ) -> PlanExecution:
        ex = self._require_execution(execution_id)
        if ex.is_terminal:
            return ex
        now = _now()
        ex.control_state = ExecutionControlState.CANCELLED
        ex.control_reason = reason.strip() or "User requested cancellation"
        ex.control_requested_at = now
        ex.control_updated_at = now
        ex.status = ExecutionStatus.CANCELLED
        for step in ex.steps:
            if step.status in {StepStatus.PENDING, StepStatus.WAITING_APPROVAL}:
                step.status = StepStatus.SKIPPED
                step.error_code = "EXECUTION_CANCELLED"
                step.error_message = ex.control_reason
                step.completed_at = now
                step.version += 1
        ex.updated_at = now
        result = self._repo.save(ex)
        self._emit(
            execution_id,
            EventType.EXECUTION_CANCELLED,
            payload={"reason": ex.control_reason},
        )
        return result

    def fail_execution(
        self,
        execution_id: str,
        *,
        error_code: str = "EXECUTION_FAILED",
        error_message: str = "Execution cannot make progress",
    ) -> PlanExecution:
        """Persist an execution-level failure when no step can progress."""
        ex = self._require_execution(execution_id)
        if ex.is_terminal:
            return ex
        ex.status = ExecutionStatus.FAILED
        ex.updated_at = _now()
        result = self._repo.save(ex)
        self._emit(
            execution_id,
            EventType.EXECUTION_FAILED,
            payload={"error_code": error_code, "error_message": error_message},
        )
        return result

    # ── resume ──────────────────────────────────────────────────

    def resume_execution(
        self,
        execution_id: str,
        *,
        retryable_step_ids: set[str] | None = None,
        running_step_ids: set[str] | None = None,
    ) -> PlanExecution:
        """Prepare an execution for resumption.

        - COMPLETED steps → left as-is (skipped)
        - FAILED_RETRYABLE steps → reset to PENDING (will retry)
        - WAITING_APPROVAL steps → left as-is
        - RUNNING steps (crash) → reset to PENDING
        """
        ex = self._require_execution(execution_id)
        if ex.is_terminal:
            return ex

        if ex.status == ExecutionStatus.WAITING_HUMAN:
            raise ValueError(
                f"Execution {execution_id} is waiting for human reconciliation; "
                "use the explicit retry, reconciliation, or approval flow"
            )

        # A user pause is distinct from crash recovery. Preserve step state
        # and only reopen the execution; Worker remains responsible for the
        # next execution pass.
        if ex.status == ExecutionStatus.PAUSED:
            if ex.control_state != ExecutionControlState.PAUSED:
                raise ValueError(
                    f"Execution {execution_id} has inconsistent pause state: "
                    f"{ex.control_state.value}"
                )
            now = _now()
            ex.control_state = ExecutionControlState.RESUMING
            ex.status = ExecutionStatus.RUNNING
            ex.control_requested_at = now
            ex.control_updated_at = now
            ex.updated_at = now
            result = self._repo.save(ex)
            self._emit(execution_id, EventType.EXECUTION_RESUME_REQUESTED)
            return result

        for step in ex.steps:
            if step.status == StepStatus.FAILED_RETRYABLE:
                if (
                    retryable_step_ids is not None
                    and step.step_execution_id not in retryable_step_ids
                ):
                    continue
                step.status = StepStatus.PENDING
                step.error_code = ""
                step.error_message = ""
                step.version += 1
            elif step.status == StepStatus.RUNNING:
                if (
                    running_step_ids is not None
                    and step.step_execution_id not in running_step_ids
                ):
                    continue
                # Process crashed mid-step — reset for retry
                step.status = StepStatus.PENDING
                step.version += 1
            elif step.status == StepStatus.COMPLETED:
                # Already done — do nothing (idempotent)
                pass

        ex.status = ExecutionStatus.RUNNING
        ex.updated_at = _now()
        return self._repo.save(ex)

    def inject_artifact(
        self,
        execution_id: str,
        step_execution_id: str,
        artifact: ArtifactHandle,
    ) -> StepExecution:
        """Add an input artifact to a step (for cross-task references)."""
        # Work directly on the store object to avoid copy-on-read issues
        ex = self._repo.find_by_id(execution_id)
        if ex is None:
            raise ValueError(f"Execution not found: {execution_id}")
        step = next(
            (s for s in ex.steps if s.step_execution_id == step_execution_id),
            None,
        )
        if step is None:
            raise ValueError(f"Step not found: {step_execution_id}")
        existing_ids = {a.artifact_id for a in step.input_artifacts}
        if artifact.artifact_id not in existing_ids:
            step.input_artifacts.append(artifact)
            step.version += 1
            self._repo.save(ex)
        return step.model_copy(deep=True)

    # ── helpers ─────────────────────────────────────────────────

    def _require_execution(self, execution_id: str) -> PlanExecution:
        ex = self._repo.find_by_id(execution_id)
        if ex is None:
            raise ValueError(f"Execution not found: {execution_id}")
        return ex

    def _require_step(
        self,
        execution_id: str,
        step_execution_id: str,
    ) -> StepExecution:
        step = self._repo.find_step(execution_id, step_execution_id)
        if step is None:
            raise ValueError(
                f"Step {step_execution_id} not found in execution "
                f"{execution_id}"
            )
        return step

    def _update_and_return(
        self,
        execution_id: str,
        step_execution_id: str,
        *,
        expected_status: StepStatus | str | None = None,
        **fields: object,
    ) -> StepExecution:
        expected = (
            expected_status.value
            if isinstance(expected_status, StepStatus)
            else expected_status
        )
        result = self._repo.update_step(
            execution_id,
            step_execution_id,
            expected_status=expected,
            **fields,
        )
        if result is None:
            raise ValueError(f"Failed to update step {step_execution_id}")
        if expected_status is not None:
            # CAS guard: verify the stored status actually moved to the
            # requested value.  A concurrent writer that transitioned first
            # must surface as a conflict, never as a silent success (which
            # would let two workers run the same step).
            target = fields.get("status")
            target_value = (
                target.value if hasattr(target, "value") else target
            )
            current = (
                result.status.value
                if hasattr(result.status, "value")
                else str(result.status)
            )
            if target_value is not None and current != target_value:
                raise _StepTransitionConflictError(
                    step_execution_id, current, target_value
                )
        return result

    def _prepare_reconciled_execution(self, execution_id: str) -> None:
        """Let authoritative reconciliation recompute a waiting parent.

        ``WAITING_HUMAN`` is also used as the safe intermediate state for a
        lost external acknowledgement. Once the read-only Java check has
        settled the operation, the normal terminal-state reducer must run;
        ordinary clarification/approval paths never call this helper.
        """
        ex = self._require_execution(execution_id)
        if ex.status != ExecutionStatus.WAITING_HUMAN:
            return
        ex.status = ExecutionStatus.RUNNING
        ex.updated_at = _now()
        self._repo.save(ex)

    def _update_execution_status(self, execution_id: str) -> None:
        ex = self._require_execution(execution_id)
        if (
            ex.status == ExecutionStatus.CANCELLED
            or ex.control_state == ExecutionControlState.CANCELLED
        ):
            ex.status = ExecutionStatus.CANCELLED
            ex.updated_at = _now()
            self._repo.save(ex)
            return
        if ex.status == ExecutionStatus.WAITING_HUMAN:
            ex.updated_at = _now()
            self._repo.save(ex)
            return
        any_waiting = any(
            s.status == StepStatus.WAITING_APPROVAL for s in ex.steps
        )
        any_failed = any(
            s.status == StepStatus.FAILED
            and not s.checkpoint_data.get("superseded_by")
            for s in ex.steps
        )
        any_retryable = any(
            s.status == StepStatus.FAILED_RETRYABLE
            for s in ex.steps
        )
        all_terminal = all(
            s.status in (
                StepStatus.COMPLETED,
                StepStatus.SKIPPED,
                StepStatus.FAILED_RETRYABLE,
                StepStatus.FAILED,
            )
            for s in ex.steps
        )

        if ex.status == ExecutionStatus.PAUSED and not any_failed and not all_terminal:
            ex.updated_at = _now()
            self._repo.save(ex)
            return

        previous_status = ex.status
        if any_waiting:
            ex.status = ExecutionStatus.WAITING_APPROVAL
        elif any_failed:
            ex.status = ExecutionStatus.FAILED
        elif any_retryable:
            ex.status = ExecutionStatus.RUNNING
        elif all_terminal:
            ex.status = ExecutionStatus.COMPLETED
            ex.completed_at = _now()
        else:
            ex.status = ExecutionStatus.RUNNING
        # If the last runnable step completed or failed while a pause request
        # was in flight, terminal execution state wins: there is no remaining
        # continuation to checkpoint or resume.
        if (
            ex.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}
            and ex.control_state
            in {ExecutionControlState.PAUSING, ExecutionControlState.RESUMING}
        ):
            ex.control_state = ExecutionControlState.RUNNING
            ex.control_updated_at = _now()
        ex.updated_at = _now()
        self._repo.save(ex)
        if previous_status != ex.status:
            if ex.status == ExecutionStatus.COMPLETED:
                self._emit(execution_id, EventType.EXECUTION_COMPLETED)
            elif ex.status == ExecutionStatus.FAILED:
                self._emit(execution_id, EventType.EXECUTION_FAILED)

    def _emit(
        self,
        execution_id: str,
        event_type: EventType,
        *,
        step_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        context = self.trace_context(execution_id)
        event_context = context.for_step(step_id) if context and step_id else context
        event_payload = dict(payload or {})
        if event_context is not None:
            event_payload.setdefault(
                "trace_context",
                event_context.model_dump(mode="json"),
            )
        self._event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=event_type,
                step_id=step_id,
                payload=event_payload,
                trace_context=event_context,
            )
        )


# ── helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(UTC).isoformat()


def _invalid_transition(
    obj: StepExecution | PlanExecution,
    expected: object,
    target: object,
) -> ValueError:
    name = getattr(obj, "step_execution_id", getattr(obj, "execution_id", "?"))
    current = getattr(obj, "status", "?")
    return ValueError(
        f"Invalid transition for {name}: {current} → {target} "
        f"(expected current={expected})"
    )


class _StepTransitionConflictError(RuntimeError):
    """Raised when a step CAS transition loses a concurrent writer."""

    def __init__(self, step_execution_id: str, current: str, target: str) -> None:
        super().__init__(
            f"Step {step_execution_id} was concurrently transitioned "
            f"({current} != {target})"
        )
