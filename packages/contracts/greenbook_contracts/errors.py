from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    OK = "OK"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DRAFT_VERSION_CONFLICT = "DRAFT_VERSION_CONFLICT"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CREATOR_UNAVAILABLE = "CREATOR_UNAVAILABLE"
    JAVA_BACKEND_UNAVAILABLE = "JAVA_BACKEND_UNAVAILABLE"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
    RUN_FAILED = "RUN_FAILED"
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
