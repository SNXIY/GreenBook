"""Execution gate used before a Worker step is started."""

from __future__ import annotations

from .models import ExecutionStatus
from .runtime_manager import RuntimeManager


class ExecutionBlockedError(RuntimeError):
    """Raised when an execution is not allowed to start or continue."""

    def __init__(self, execution_id: str, status: ExecutionStatus) -> None:
        self.execution_id = execution_id
        self.status = status
        super().__init__(
            f"Execution '{execution_id}' cannot execute while status={status.value}"
        )


class RuntimeGuard:
    """Read-only execution guard backed by RuntimeManager's state."""

    def __init__(self, runtime_manager: RuntimeManager) -> None:
        self._runtime = runtime_manager

    def can_execute(self, execution_id: str) -> bool:
        return self._runtime.get_execution(execution_id).status == ExecutionStatus.RUNNING

    def check_execution(self, execution_id: str) -> None:
        execution = self._runtime.get_execution(execution_id)
        if execution.status != ExecutionStatus.RUNNING:
            raise ExecutionBlockedError(execution_id, execution.status)


__all__ = ["ExecutionBlockedError", "RuntimeGuard"]
