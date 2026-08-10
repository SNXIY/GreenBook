"""ExecutionStateManager — state transitions, resume, approval handling.

Phase 3.3: state management only — no tool execution.
"""

from __future__ import annotations

from datetime import UTC, datetime

from greenbook_assistant_core.orchestration.models import TaskPlan
from greenbook_assistant_core.planning.models import ExecutablePlan

from .models import (
    ArtifactHandle,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from .event_store import ExecutionEventStore
from .events import EventType, ExecutionEvent
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

    @property
    def event_store(self) -> ExecutionEventStore:
        return self._event_store

    # ── initialisation ───────────────────────────────────────────

    def init_execution(
        self,
        plan: TaskPlan,
        executable: ExecutablePlan,
    ) -> PlanExecution:
        """Create a PlanExecution from a validated ExecutablePlan."""
        execution = PlanExecution(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            status=ExecutionStatus.PENDING,
            requires_approval=executable.requires_approval,
            has_side_effects=executable.has_side_effects,
            steps=[
                StepExecution(
                    step_id=s.step_id,
                    capability=s.capability,
                    ordinal=s.ordinal,
                    status=StepStatus.PENDING,
                    input_artifact_types=list(s.input_artifact_types),
                    output_artifact_type=s.output_artifact_type,
                    depends_on=list(s.depends_on),
                )
                for s in plan.steps
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
        return self._update_and_return(execution_id, step_execution_id,
                                       status=StepStatus.RUNNING,
                                       started_at=_now())

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
        result = self._update_and_return(execution_id, step_execution_id, **fields)
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
        result = self._update_and_return(execution_id, step_execution_id,
                                         status=StepStatus.RUNNING)
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

    def pause_execution(self, execution_id: str) -> PlanExecution:
        """RUNNING → PAUSED without changing any step or stopping a worker."""
        ex = self._require_execution(execution_id)
        if ex.status != ExecutionStatus.RUNNING:
            raise _invalid_transition(ex, ExecutionStatus.RUNNING,
                                      ExecutionStatus.PAUSED)
        ex.status = ExecutionStatus.PAUSED
        ex.updated_at = _now()
        result = self._repo.save(ex)
        self._emit(execution_id, EventType.EXECUTION_PAUSED)
        return result

    def cancel_execution(self, execution_id: str) -> PlanExecution:
        ex = self._require_execution(execution_id)
        if ex.is_terminal:
            return ex
        ex.status = ExecutionStatus.CANCELLED
        ex.updated_at = _now()
        result = self._repo.save(ex)
        self._emit(execution_id, EventType.EXECUTION_CANCELLED)
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

        # A user pause is distinct from crash recovery. Preserve step state
        # and only reopen the execution; Worker remains responsible for the
        # next execution pass.
        if ex.status == ExecutionStatus.PAUSED:
            ex.status = ExecutionStatus.RUNNING
            ex.updated_at = _now()
            result = self._repo.save(ex)
            self._emit(execution_id, EventType.EXECUTION_RESUMED)
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
        **fields: object,
    ) -> StepExecution:
        result = self._repo.update_step(
            execution_id, step_execution_id, **fields
        )
        if result is None:
            raise ValueError(f"Failed to update step {step_execution_id}")
        return result

    def _update_execution_status(self, execution_id: str) -> None:
        ex = self._require_execution(execution_id)
        any_waiting = any(
            s.status == StepStatus.WAITING_APPROVAL for s in ex.steps
        )
        any_failed = any(
            s.status == StepStatus.FAILED
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
        self._event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=event_type,
                step_id=step_id,
                payload=payload or {},
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
