from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class DataProvenance(StrEnum):
    """Source class carried with tool evidence and downstream projections."""

    PERSONAL_DATA = "PERSONAL_DATA"
    COMMUNITY_DATA = "COMMUNITY_DATA"
    MODEL_INFERENCE = "MODEL_INFERENCE"


class ResourceRef(BaseModel):
    ref: str
    kind: str
    resource_id: str
    version: int | None = None
    # Optional read provenance metadata.  Existing callers may continue to
    # construct the compact ref/kind/id form used by write receipts.
    title: str | None = None
    label: str | None = None
    source: str | None = None
    tool: str | None = None

    @property
    def resource_type(self) -> str:
        """Compatibility name for callers that use resource_type terminology."""
        return self.kind


class OperationReceipt(BaseModel):
    """Evidence envelope for one business operation.

    A successful HTTP submission is not equivalent to a completed user
    operation.  Write tools attach this receipt so callers can distinguish a
    verified postcondition from an accepted-but-unverified downstream write.
    ``operation_id`` is normally the retry-stable idempotency key.
    """

    operation_id: str
    semantic_action: str
    task_id: str | None = None
    objective_id: str | None = None
    resource_ref: ResourceRef | None = None
    idempotency_key: str | None = None
    request_sent: bool | None = None
    downstream_accepted: bool = False
    side_effect_started: bool = False
    result_known: bool = False
    observed_state: dict[str, Any] | None = None
    verification_evidence: dict[str, Any] | None = None
    status: str = "PLANNED"


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
    operation_receipt: OperationReceipt | None = None

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
        state: dict[str, Any] | None = None,
    ) -> ToolResult[T]:
        return cls(
            ok=False,
            code=code,
            message=message,
            user_message=user_message,
            retryable=retryable,
            request_sent=request_sent,
            trace_id=trace_id,
            state=state,
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
        operation_receipt: OperationReceipt | None = None,
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
            operation_receipt=operation_receipt,
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
    def permanent_input(
        cls,
        code: str = "PERMANENT_INPUT",
        message: str = "",
        *,
        user_message: str = "The request contains invalid input.",
        state: dict[str, Any] | None = None,
    ) -> ToolResult[T]:
        return cls.failure(
            code,
            message,
            user_message,
            retryable=False,
            request_sent=False,
            state=state,
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
        cls,
        message: str = "",
        *,
        trace_id: str | None = None,
        state: dict[str, Any] | None = None,
        receipt_id: str | None = None,
        resource_refs: list[ResourceRef] | None = None,
        operation_receipt: OperationReceipt | None = None,
    ) -> ToolResult[T]:
        # ``RESULT_UNKNOWN`` is only valid when the delivery boundary is
        # ambiguous and a side effect may already have started.  Keep that
        # evidence attached to the result instead of relying on callers to
        # remember to add it to ``state``.
        resolved_state = dict(state or {})
        resolved_state.setdefault("side_effect_started", True)
        resolved_state.setdefault("side_effect_state", "POSSIBLE")
        resolved_state.setdefault("result_known", False)
        resolved_receipt = operation_receipt
        if operation_receipt is not None:
            resolved_receipt = operation_receipt.model_copy(
                update={
                    "side_effect_started": True,
                    "result_known": False,
                    "status": "RESULT_UNKNOWN",
                }
            )
        return cls(
            ok=False,
            code="RESULT_UNKNOWN",
            message=message,
            user_message=(
                "The request may have been submitted. "
                "Checking actual status — please do not repeat the operation."
            ),
            retryable=False,
            request_sent=None,
            trace_id=trace_id,
            state=resolved_state,
            receipt_id=receipt_id,
            resource_refs=resource_refs or [],
            operation_receipt=resolved_receipt,
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
            # An agent/runtime defect is not a transient transport failure.
            # Retrying it can repeat an already-started handler and hides the
            # original defect from the durable boundary.
            retryable=False,
            request_sent=False,
            trace_id=trace_id,
        )

    @classmethod
    def server_failure(
        cls, message: str = "", *, trace_id: str | None = None
    ) -> ToolResult[T]:
        return cls.failure(
            "SERVER_FAILURE",
            message or "Java returned an internal server failure",
            "社区服务暂时无法完成这项操作，请稍后再试。",
            retryable=False,
            request_sent=True,
            trace_id=trace_id,
        )
