from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    OK = "OK"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DRAFT_VERSION_CONFLICT = "DRAFT_VERSION_CONFLICT"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    REQUEST_NOT_SENT = "REQUEST_NOT_SENT"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class GreenBookError(Exception):
    """Base error for all GreenBook failures."""

    def __init__(
        self,
        code: ErrorCode,
        message: str = "",
        *,
        retryable: bool = False,
        request_sent: bool = False,
        trace_id: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.request_sent = request_sent
        self.trace_id = trace_id
        super().__init__(message or code.value)
