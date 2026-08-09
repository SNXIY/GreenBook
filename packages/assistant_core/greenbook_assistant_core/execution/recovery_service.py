"""Process-restart recovery for persisted PlanExecution state."""

from __future__ import annotations

from .models import ExecutionStatus, PlanExecution, StepStatus
from .recovery import RecoveryPolicy
from .state_manager import ExecutionStateManager


class ExecutionRecoveryService:
    RECOVERABLE_EXECUTION_STATUSES = frozenset({
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAITING_HUMAN,
        ExecutionStatus.PAUSED,
    })

    def __init__(
        self,
        state_manager: ExecutionStateManager,
        policy: RecoveryPolicy | None = None,
    ) -> None:
        self._state = state_manager
        self._policy = policy or RecoveryPolicy()

    def restore_execution(self, execution_id: str) -> PlanExecution:
        execution = self._state.get_execution(execution_id)
        should_scan = execution.status in self.RECOVERABLE_EXECUTION_STATUSES or any(
            step.status == StepStatus.FAILED_RETRYABLE for step in execution.steps
        )
        if not should_scan:
            return execution

        for step in self._state.list_steps(execution_id):
            if step.status == StepStatus.FAILED_RETRYABLE:
                if self._policy.can_retry(step):
                    self._state.retry_step(execution_id, step.step_execution_id)
            elif step.status == StepStatus.RUNNING:
                self._state.recover_step(execution_id, step.step_execution_id)
        return self._state.get_execution(execution_id)

    def recover_all(self) -> list[PlanExecution]:
        executions = self._state.list_executions()
        restored: list[PlanExecution] = []
        for execution in executions:
            if (
                execution.status in self.RECOVERABLE_EXECUTION_STATUSES
                or any(step.status == StepStatus.FAILED_RETRYABLE
                       for step in execution.steps)
            ):
                restored.append(self.restore_execution(execution.execution_id))
        return restored


__all__ = ["ExecutionRecoveryService"]
