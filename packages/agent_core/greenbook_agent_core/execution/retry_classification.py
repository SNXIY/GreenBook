"""Deterministic retry classification for failed Runtime operations.

Distinguishes the three cases a worker must not conflate:

  * SAFE_RETRY — the request was provably not sent (or is a read / a
    precondition failure), so re-running cannot duplicate a side effect.
  * RESULT_UNKNOWN — a write request may have reached Java; a normal retry is
    forbidden.  The operation must be reconciled against authoritative state.
  * PERMANENT_FAILURE — schema, permission, 404/409, etc.  Never retry.
"""

from __future__ import annotations

from typing import Any

from .operation_tracking import OperationStatus, RetryClassification

# Explicit business failures that are never retryable.
_PERMANENT_MARKERS = (
    "SCHEMA",
    "PERMISSION",
    "POLICY_DENIED",
    "NOT_FOUND",
    "BAD_REQUEST",
    "UNSUPPORTED",
    "INVALID",
)
_PERMANENT_CODES = {"404", "409", "400", "403", "422"}

# These are deterministic validation/business/runtime outcomes.  Keep them
# out of the transport retry path even when a legacy caller supplies
# request_sent=False or invokes this helper for a read operation.
_NON_RETRYABLE_CODES = frozenset({
    "VALIDATION_ERROR",
    "INVALID_ARGUMENT",
    "INVALID_REQUEST",
    "BAD_REQUEST",
    "BUSINESS_REJECTED",
    "BUSINESS_RULE_REJECTED",
    "INTERNAL_ERROR",
    "TOOL_EXECUTION_FAILED",
    "RUN_FAILED",
    "MODEL_REQUEST_FAILED",
    "CONTRACT_MISMATCH",
    "AUTH_FAILURE",
    "AUTHENTICATION_FAILED",
    "PERMISSION_DENIED",
    "AUTHORIZATION_DENIED",
    "FORBIDDEN",
    "CONFLICT",
    "STATE_CONFLICT",
})


def classify_retry(
    *,
    is_write: bool = True,
    side_effect_started: bool | None = None,
    request_sent: bool | None = None,
    side_effect_state: Any = None,
    status: Any = None,
    error_code: str = "",
) -> RetryClassification:
    """Classify a failed operation into SAFE_RETRY / RESULT_UNKNOWN / PERMANENT_FAILURE."""

    code = str(error_code or "").upper()

    # A NOT_FOUND returned by reconciliation can mean that the operation
    # idempotency record never existed.  With explicit no-send evidence this
    # is a safe replay boundary.  A normal Java 404 with request_sent=True
    # remains a permanent business rejection below.
    safe_not_found = (
        status in {OperationStatus.NOT_FOUND, "NOT_FOUND"}
        and request_sent is False
        and not bool(side_effect_started)
        and not _side_effect_state_started(side_effect_state)
    )
    if safe_not_found:
        return RetryClassification.SAFE_RETRY

    if (
        code in _PERMANENT_CODES
        or code in _NON_RETRYABLE_CODES
        or any(marker in code for marker in _PERMANENT_MARKERS)
    ):
        return RetryClassification.PERMANENT_FAILURE

    if not is_write:
        return RetryClassification.SAFE_RETRY

    started = bool(side_effect_started)
    if not started:
        started = _side_effect_state_started(side_effect_state)
    # A request was sent (or may have been): the write may have landed in Java.
    may_have_sent = request_sent is not False and request_sent is not None
    if started or may_have_sent:
        return RetryClassification.RESULT_UNKNOWN

    if status in {OperationStatus.NOT_FOUND, "NOT_FOUND"}:
        return RetryClassification.PERMANENT_FAILURE

    return RetryClassification.SAFE_RETRY


def is_permanent(status: Any = None, error_code: str = "") -> bool:
    return classify_retry(status=status, error_code=error_code) == RetryClassification.PERMANENT_FAILURE


def is_safe_retry(
    *,
    is_write: bool = True,
    side_effect_started: bool | None = None,
    request_sent: bool | None = None,
    error_code: str = "",
) -> bool:
    return (
        classify_retry(
            is_write=is_write,
            side_effect_started=side_effect_started,
            request_sent=request_sent,
            error_code=error_code,
        )
        == RetryClassification.SAFE_RETRY
    )


def _side_effect_state_started(side_effect_state: Any) -> bool:
    if side_effect_state is None:
        return False
    value = getattr(side_effect_state, "value", side_effect_state)
    normalized = str(value).strip().upper()
    return normalized in {"STARTED", "COMMITTED", "UNKNOWN", "IN_PROGRESS"}


__all__ = [
    "classify_retry",
    "is_permanent",
    "is_safe_retry",
]
