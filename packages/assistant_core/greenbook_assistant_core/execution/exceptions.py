"""Execution control exceptions."""

from __future__ import annotations

from typing import Any


class ExecutionBlockedError(RuntimeError):
    """Raised when execution is blocked by its current runtime status."""

    def __init__(self, execution_id: str, current_status: Any) -> None:
        self.execution_id = execution_id
        self.current_status = current_status
        super().__init__(
            f"Execution '{execution_id}' is blocked in status "
            f"{getattr(current_status, 'value', current_status)}"
        )
