"""Explicit step retry and recovery orchestration."""

from __future__ import annotations

from .events import EventType, ExecutionEvent
from .models import StepExecution
from .recovery import RecoveryPolicy
from .runtime_manager import RuntimeManager
from .state_manager import ExecutionStateManager


class RetryManager:
    """Prepare failed steps for a later Worker.run() pass."""

    def __init__(
        self,
        state_manager: ExecutionStateManager | None = None,
        policy: RecoveryPolicy | None = None,
        runtime_manager: RuntimeManager | None = None,
    ) -> None:
        self._state = state_manager or ExecutionStateManager()
        self._policy = policy or RecoveryPolicy()
        self._runtime = runtime_manager or RuntimeManager(self._state)

    def retry_step(self, execution_id: str, step_id: str) -> StepExecution:
        step = self._find_step(execution_id, step_id)
        reason = step.error_code
        self._emit(
            execution_id,
            EventType.STEP_RETRY_REQUESTED,
            step,
            {"retry_count": step.retry_count, "reason": reason},
        )

        if not self._policy.can_retry(step):
            if self._policy.is_retryable_error(step.error_code):
                self._emit(
                    execution_id,
                    EventType.STEP_RETRY_EXHAUSTED,
                    step,
                    {"retry_count": step.retry_count, "reason": reason},
                )
            return step

        result = self._state.retry_step(execution_id, step.step_execution_id)
        self._runtime.save_checkpoint(execution_id)
        self._emit(
            execution_id,
            EventType.STEP_RETRY_STARTED,
            result,
            {"retry_count": result.retry_count, "reason": reason},
        )
        return result

    def get_checkpoint(self, execution_id: str):
        return self._runtime.restore_checkpoint(execution_id)

    def _find_step(self, execution_id: str, step_id: str) -> StepExecution:
        for step in self._state.list_steps(execution_id):
            if step.step_id == step_id or step.step_execution_id == step_id:
                return step
        raise ValueError(f"Step {step_id} not found in execution {execution_id}")

    def _emit(
        self,
        execution_id: str,
        event_type: EventType,
        step: StepExecution,
        payload: dict[str, object],
    ) -> None:
        self._state.event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=event_type,
                step_id=step.step_id,
                payload={"step_execution_id": step.step_execution_id, **payload},
            )
        )


__all__ = ["RetryManager"]
