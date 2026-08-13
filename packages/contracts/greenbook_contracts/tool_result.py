from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class DataProvenance(StrEnum):
    """Source class carried with tool evidence and downstream projections."""

    PERSONAL_DATA = "PERSONAL_DATA"
    COMMUNITY_DATA = "COMMUNITY_DATA"
    CREATOR_RESEARCH = "CREATOR_RESEARCH"
    MODEL_INFERENCE = "MODEL_INFERENCE"


class ResourceRef(BaseModel):
    ref: str
    kind: str
    resource_id: str
    version: int | None = None


class ToolResult[T](BaseModel):
    """Unified result envelope for every MCP tool and Java API call."""

    ok: bool
    code: str = Field(default="OK")
    message: str = Field(default="")
    user_message: str = Field(default="")
    retryable: bool = Field(default=False)
    # ``None`` is reserved for a write whose delivery state is unknown.  The
    # default remains ``False`` for backwards compatibility with existing
    # client-side failures; callers that cannot prove whether a request was
    # sent must pass ``None`` explicitly.
    request_sent: bool | None = Field(default=False)
    state: dict[str, Any] | None = Field(default=None)
    data: T | None = Field(default=None)
    provenance: list[DataProvenance] = Field(default_factory=list)
    trace_id: str | None = Field(default=None)
    receipt_id: str | None = Field(default=None)
    resource_refs: list[ResourceRef] = Field(default_factory=list)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        user_message: str,
        *,
        retryable: bool = False,
        request_sent: bool | None = False,
        trace_id: str | None = None,
    ) -> ToolResult[T]:
        return cls(
            ok=False,
            code=code,
            message=message,
            user_message=user_message,
            retryable=retryable,
            request_sent=request_sent,
            trace_id=trace_id,
        )

    @classmethod
    def creator_unavailable(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> ToolResult[T]:
        return cls.failure(
            "CREATOR_UNAVAILABLE",
            message or "Creator Service is unavailable",
            "创作服务暂时不可用，尚未保存草稿，可以安全重试。",
            retryable=True,
            trace_id=trace_id,
        )

    @classmethod
    def java_backend_unavailable(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> ToolResult[T]:
        return cls.failure(
            "JAVA_BACKEND_UNAVAILABLE",
            message or "Java backend is unavailable",
            "社区服务暂时不可用，尚未确认本次操作结果。",
            retryable=True,
            trace_id=trace_id,
        )

    @classmethod
    def tool_execution_failed(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> ToolResult[T]:
        return cls.failure(
            "TOOL_EXECUTION_FAILED",
            message or "Tool execution failed",
            "工具执行失败，请稍后重试。",
            retryable=False,
            trace_id=trace_id,
        )

    # ── Success ──────────────────────────────────────────

    @classmethod
    def success(
        cls,
        data: T,
        *,
        trace_id: str | None = None,
        receipt_id: str | None = None,
        resource_refs: list[ResourceRef] | None = None,
        provenance: list[DataProvenance] | None = None,
    ) -> ToolResult[T]:
        return cls(
            ok=True,
            code="OK",
            data=data,
            request_sent=True,
            provenance=provenance or [],
            trace_id=trace_id,
            receipt_id=receipt_id,
            resource_refs=resource_refs or [],
        )

    # ── Client-side failures (request not sent) ──────────

    @classmethod
    def dependency_unavailable(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> ToolResult[T]:
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
    def validation_error(cls, message: str = "", *, user_message: str = "") -> ToolResult[T]:
        return cls(
            ok=False,
            code="VALIDATION_ERROR",
            message=message,
            user_message=user_message or "The request contains invalid parameters.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def authentication_required(cls, message: str = "") -> ToolResult[T]:
        return cls(
            ok=False,
            code="AUTHENTICATION_REQUIRED",
            message=message,
            user_message="Authentication is required. Please log in and try again.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def permission_denied(cls, message: str = "") -> ToolResult[T]:
        return cls(
            ok=False,
            code="PERMISSION_DENIED",
            message=message,
            user_message="You do not have permission to perform this action.",
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def business_rejected(cls, message: str = "", *, user_message: str = "") -> ToolResult[T]:
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
    def not_found(cls, message: str = "") -> ToolResult[T]:
        return cls(
            ok=False,
            code="NOT_FOUND",
            message=message,
            user_message="The requested resource was not found.",
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def conflict(cls, message: str = "", *, user_message: str = "") -> ToolResult[T]:
        return cls(
            ok=False,
            code="CONFLICT",
            message=message,
            user_message=user_message or "This request conflicts with the current state.",
            retryable=False,
            request_sent=True,
        )

    @classmethod
    def draft_version_conflict(cls, message: str = "") -> ToolResult[T]:
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
    def idempotency_conflict(cls, message: str = "") -> ToolResult[T]:
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
    def timeout(cls, message: str = "") -> ToolResult[T]:
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
    ) -> ToolResult[T]:
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
    def request_not_sent(cls, message: str = "") -> ToolResult[T]:
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
    ) -> ToolResult[T]:
        return cls(
            ok=False,
            code="INTERNAL_ERROR",
            message=message,
            user_message="An internal error occurred. Please try again later.",
            retryable=True,
            request_sent=False,
            trace_id=trace_id,
        )
