"""Process-restart recovery for persisted PlanExecution state."""

from __future__ import annotations

from .models import ExecutionStatus, PlanExecution, StepStatus
from .recovery import RecoveryPolicy
from .retry_decision import RetryDecisionEngine
from .retry_manager import RetryManager
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
        decision_engine: RetryDecisionEngine | None = None,
    ) -> None:
        self._state = state_manager
        self._policy = policy or RecoveryPolicy()
        self._decision_engine = decision_engine or RetryDecisionEngine()

    def restore_execution(self, execution_id: str) -> PlanExecution:
        execution = self._state.get_execution(execution_id)
        should_scan = execution.status in self.RECOVERABLE_EXECUTION_STATUSES or any(
            step.status == StepStatus.FAILED_RETRYABLE for step in execution.steps
        )
        if not should_scan:
            return execution

        retry_manager = RetryManager(
            state_manager=self._state,
            policy=self._policy,
            decision_engine=self._decision_engine,
        )
        for step in self._state.list_steps(execution_id):
            if step.status == StepStatus.FAILED_RETRYABLE:
                decision = retry_manager.decision_for_step(
                    execution_id,
                    step.step_id,
                    source="process_recovery",
                )
                if decision.allowed:
                    self._state.retry_step(execution_id, step.step_execution_id)
            elif step.status == StepStatus.RUNNING:
                decision = retry_manager.decision_for_step(
                    execution_id,
                    step.step_id,
                    source="process_recovery_crash",
                )
                if decision.allowed:
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
