"""User control adapter for the canonical PlanExecution lifecycle."""

from __future__ import annotations

from typing import Any

from greenbook_assistant_core.planning.models import ExecutablePlan
from greenbook_assistant_core.orchestration.models import TaskPlan

from .checkpoint import ExecutionCheckpoint
from .events import EventType, ExecutionEvent
from .models import PlanExecution, StepExecution
from .state_manager import ExecutionStateManager


class RuntimeManager:
    """Expose user-facing execution controls without owning execution state."""

    def __init__(
        self,
        state_manager: ExecutionStateManager | None = None,
        checkpoint_store: Any | None = None,
    ) -> None:
        self._state = state_manager or ExecutionStateManager()
        self._checkpoints: dict[str, ExecutionCheckpoint] = {}
        self._checkpoint_store = checkpoint_store

    def create_execution(
        self,
        plan: TaskPlan,
        executable: ExecutablePlan,
    ) -> PlanExecution:
        execution = self._state.init_execution(plan, executable)
        self._state.event_store.append(
            ExecutionEvent(
                execution_id=execution.execution_id,
                event_type=EventType.EXECUTION_CREATED,
                payload={"task_id": execution.task_id, "plan_id": execution.plan_id},
            )
        )
        return execution

    def get_execution(self, execution_id: str) -> PlanExecution:
        return self._state.get_execution(execution_id)

    def list_executions(self) -> list[PlanExecution]:
        """Return execution snapshots for an authorized API projection."""
        return self._state.list_executions()

    def start_execution(self, execution_id: str) -> PlanExecution:
        """Start a newly created execution through the state manager."""
        return self._state.start_execution(execution_id)

    def pause_execution(self, execution_id: str) -> PlanExecution:
        return self._state.pause_execution(execution_id)

    def resume_execution(self, execution_id: str) -> PlanExecution:
        # User-facing resume must pass the same evidence-aware retry gate as
        # the Worker, while the low-level StateManager remains compatible for
        # legacy callers that explicitly own state transitions.
        from .retry_manager import RetryManager

        return RetryManager(
            state_manager=self._state,
            runtime_manager=self,
        ).resume_execution(execution_id)

    def cancel_execution(self, execution_id: str) -> PlanExecution:
        return self._state.cancel_execution(execution_id)

    def list_steps(self, execution_id: str) -> list[StepExecution]:
        return self._state.list_steps(execution_id)

    def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        self._state.get_execution(execution_id)
        return self._state.event_store.list_events(execution_id)

    @property
    def event_store(self) -> Any:
        return self._state.event_store

    def save_checkpoint(
        self,
        execution_id: str,
        snapshot: dict[str, Any] | None = None,
    ) -> ExecutionCheckpoint:
        execution = self._state.get_execution(execution_id)
        completed = [
            step.step_id for step in self._state.list_steps(execution_id)
            if step.status.value == "COMPLETED"
        ]
        current = next(
            (
                step.step_id for step in self._state.list_steps(execution_id)
                if step.status.value in {"RUNNING", "PENDING", "WAITING_APPROVAL"}
            ),
            "",
        )
        checkpoint = ExecutionCheckpoint(
            execution_id=execution.execution_id,
            completed_steps=completed,
            current_step=current,
            snapshot=dict(snapshot or {}),
        )
        self._checkpoints[execution_id] = checkpoint
        if self._checkpoint_store is not None:
            self._checkpoint_store.save(checkpoint)
        return checkpoint

    def restore_checkpoint(self, execution_id: str) -> ExecutionCheckpoint | None:
        """Return the snapshot; PlanExecution remains the source of truth."""
        self._state.get_execution(execution_id)
        if self._checkpoint_store is not None:
            return self._checkpoint_store.latest(execution_id)
        return self._checkpoints.get(execution_id)


__all__ = ["RuntimeManager"]
