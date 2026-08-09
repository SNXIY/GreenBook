from __future__ import annotations

from typing import Any


class CreatorToolError(RuntimeError):
    code = "CREATOR_TOOL_ERROR"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        call_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.call_id = call_id
        self.details = details or {}


class CreatorToolNotFoundError(CreatorToolError):
    code = "TOOL_NOT_FOUND"


class CreatorToolAuthorizationError(CreatorToolError):
    code = "TOOL_NOT_AUTHORIZED"


class CreatorToolBudgetError(CreatorToolError):
    code = "TOOL_BUDGET_EXHAUSTED"


class CreatorToolValidationError(CreatorToolError):
    code = "TOOL_ARGUMENT_INVALID"


class CreatorToolTimeoutError(CreatorToolError):
    code = "TOOL_TIMEOUT"
    retryable = True


class CreatorToolResultTooLargeError(CreatorToolError):
    code = "TOOL_RESULT_TOO_LARGE"


class CreatorToolExecutionError(CreatorToolError):
    code = "TOOL_EXECUTION_FAILED"
    retryable = True


class CreatorToolAuditError(CreatorToolError):
    code = "TOOL_AUDIT_UNAVAILABLE"
    retryable = True
