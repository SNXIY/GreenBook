"""Error-code driven policy for step recovery."""

from __future__ import annotations

from collections.abc import Iterable

from .models import StepExecution, StepStatus


class RecoveryPolicy:
    """Decide whether a failed step may be retried.

    The default set contains transport/runtime failures only. Applications
    can provide their own error-code vocabulary without changing execution
    state transitions.
    """

    DEFAULT_RETRYABLE_CODES = frozenset({
        "TIMEOUT",
        "NETWORK_ERROR",
        "RATE_LIMIT",
        "TEMPORARY_UNAVAILABLE",
    })

    def __init__(self, retryable_codes: Iterable[str] | None = None) -> None:
        self._retryable_codes = frozenset(
            code.upper() for code in (
                retryable_codes
                if retryable_codes is not None
                else self.DEFAULT_RETRYABLE_CODES
            )
        )

    def is_retryable_error(self, error_code: str) -> bool:
        return error_code.upper() in self._retryable_codes

    def max_retry_count(self, step_execution: StepExecution) -> int:
        return max(0, step_execution.max_retries)

    def can_retry(self, step_execution: StepExecution) -> bool:
        return (
            step_execution.status in (StepStatus.FAILED, StepStatus.FAILED_RETRYABLE)
            and self.is_retryable_error(step_execution.error_code)
            and step_execution.retry_count < self.max_retry_count(step_execution)
        )

    def can_retry_failure(self, step_execution: StepExecution, error_code: str) -> bool:
        """Evaluate a failure before StateManager records it."""
        return (
            self.is_retryable_error(error_code)
            and step_execution.retry_count < self.max_retry_count(step_execution)
        )


__all__ = ["RecoveryPolicy"]
