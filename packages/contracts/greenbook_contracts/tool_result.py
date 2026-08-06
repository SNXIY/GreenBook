from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ResourceRef(BaseModel):
    ref: str
    kind: str
    resource_id: str
    version: int | None = None


class ToolResult(BaseModel, Generic[T]):
    """Unified result envelope for every MCP tool and Java API call."""

    ok: bool
    code: str = Field(default="OK")
    message: str = Field(default="")
    user_message: str = Field(default="")
    retryable: bool = Field(default=False)
    request_sent: bool = Field(default=False)
    state: dict[str, Any] | None = Field(default=None)
    data: T | None = Field(default=None)
    trace_id: str | None = Field(default=None)
    receipt_id: str | None = Field(default=None)
    resource_refs: list[ResourceRef] = Field(default_factory=list)

    # ── Success ──────────────────────────────────────────

    @classmethod
    def success(
        cls,
        data: T,
        *,
        trace_id: str | None = None,
        receipt_id: str | None = None,
        resource_refs: list[ResourceRef] | None = None,
    ) -> "ToolResult[T]":
        return cls(
            ok=True,
            code="OK",
            data=data,
            request_sent=True,
            trace_id=trace_id,
            receipt_id=receipt_id,
            resource_refs=resource_refs or [],
        )

    # ── Client-side failures (request not sent) ──────────

    @classmethod
    def dependency_unavailable(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> "ToolResult[T]":
        return cls(
            ok=False,
            code="DEPENDENCY_UNAVAILABLE",
            message=message,
            user_message=(
                "The service is temporarily unavailable. "
                "No action was submitted. You may safely retry."
            ),
            retryable=True,
            request_sent=False,
            trace_id=trace_id,
        )

    @classmethod
    def validation_error(cls, message: str = "", *, user_message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="VALIDATION_ERROR",
            message=message,
            user_message=user_message or "The request contains invalid parameters.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def authentication_required(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="AUTHENTICATION_REQUIRED",
            message=message,
            user_message="Authentication is required. Please log in and try again.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def permission_denied(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="PERMISSION_DENIED",
            message=message,
            user_message="You do not have permission to perform this action.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def business_rejected(cls, message: str = "", *, user_message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="BUSINESS_REJECTED",
            message=message,
            user_message=user_message or "This operation was rejected by business rules.",
            retryable=False,
            request_sent=True,
        )

    # ── Server-side / ambiguous failures ─────────────────

    @classmethod
    def not_found(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="NOT_FOUND",
            message=message,
            user_message="The requested resource was not found.",
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def conflict(cls, message: str = "", *, user_message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="CONFLICT",
            message=message,
            user_message=user_message or "This request conflicts with the current state.",
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def draft_version_conflict(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="DRAFT_VERSION_CONFLICT",
            message=message,
            user_message=(
                "The draft has been modified since you last viewed it. "
                "Please review the latest version before making changes."
            ),
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def idempotency_conflict(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="IDEMPOTENCY_CONFLICT",
            message=message,
            user_message=(
                "A request with this key has already been processed. "
                "If you intended a new operation, please try again."
            ),
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def timeout(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="TIMEOUT",
            message=message,
            user_message="The operation timed out. Please try again.",
            retryable=True,
            request_sent=True,
        )

    @classmethod
    def result_unknown(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> "ToolResult[T]":
        return cls(
            ok=False,
            code="RESULT_UNKNOWN",
            message=message,
            user_message=(
                "The request may have been submitted. "
                "Checking actual status — please do not repeat the operation."
            ),
            retryable=False,
            request_sent=True,
            trace_id=trace_id,
        )

    @classmethod
    def request_not_sent(cls, message: str = "") -> "ToolResult[T]":
        return cls(
            ok=False,
            code="REQUEST_NOT_SENT",
            message=message,
            user_message="The request was not sent. You may safely retry.",
            retryable=True,
            request_sent=False,
        )

    @classmethod
    def internal_error(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> "ToolResult[T]":
        return cls(
            ok=False,
            code="INTERNAL_ERROR",
            message=message,
            user_message="An internal error occurred. Please try again later.",
            retryable=True,
            request_sent=False,
            trace_id=trace_id,
        )
