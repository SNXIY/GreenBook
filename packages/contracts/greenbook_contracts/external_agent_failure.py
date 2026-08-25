"""Contracts for normalising failures returned by external agents.

This module is deliberately policy-light.  It turns a ``ToolResult`` into a
stable failure fact for later Runtime recovery code; it never retries a
request, changes Execution state, or calls a downstream service.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .tool_result import ToolResult


class SideEffectState(StrEnum):
    """What is known about a possible external side effect."""

    NONE = "NONE"
    NOT_STARTED = "NOT_STARTED"
    POSSIBLE = "POSSIBLE"
    UNKNOWN = "UNKNOWN"
    # Kept for downstream reconciliation records even though Phase 9-B tests
    # focus on the four uncertainty states above.
    CONFIRMED = "CONFIRMED"


class RecoveryAction(StrEnum):
    """A recommendation for a later recovery coordinator."""

    RETRY = "RETRY"
    WAIT_DEPENDENCY = "WAIT_DEPENDENCY"
    RECONCILE = "RECONCILE"
    REAUTH = "REAUTH"
    FAIL = "FAIL"


class ExternalAgentFailure(BaseModel):
    """Normalised, side-effect-aware failure fact.

    ``error_code`` deliberately contains the original ``ToolResult.code``;
    callers may classify it using the normalised code without losing the
    source vocabulary used by a downstream service.
    """

    model_config = ConfigDict(frozen=True)

    dependency: str
    error_code: str
    retryable: bool
    user_visible_message: str
    recovery_action: RecoveryAction

    request_sent: bool | None = None
    side_effect_state: SideEffectState = SideEffectState.UNKNOWN
    message: str = ""
    phase: str | None = None
    trace_id: str | None = None
    receipt_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Serialized ExecutionEvidence when the Runtime boundary provided one.
    # The contracts package stores it as a mapping to avoid importing the
    # Agent Runtime execution package and creating a dependency cycle.
    evidence: dict[str, Any] | None = None


# Canonical external failures plus aliases emitted by existing clients.
_CLASSIFICATIONS: dict[str, tuple[str, RecoveryAction, bool]] = {
    "JAVA_BACKEND_UNAVAILABLE": ("java", RecoveryAction.RETRY, False),
    "MCP_TIMEOUT": ("mcp", RecoveryAction.RETRY, False),
    "MODEL_TIMEOUT": ("model", RecoveryAction.RETRY, False),
    "RATE_LIMIT": ("", RecoveryAction.WAIT_DEPENDENCY, False),
    "AUTH_FAILURE": ("", RecoveryAction.REAUTH, True),
    "AUTHENTICATION_FAILED": ("", RecoveryAction.REAUTH, True),
    "AUTHENTICATION_REQUIRED": ("", RecoveryAction.REAUTH, True),
    "UNAUTHORIZED": ("", RecoveryAction.REAUTH, True),
    "TOO_MANY_REQUESTS": ("", RecoveryAction.WAIT_DEPENDENCY, False),
    "LLM_TIMEOUT": ("model", RecoveryAction.RETRY, False),
}

# These codes are authoritative negative responses.  They describe a known
# rejection/no-op, not an ambiguous delivery boundary.  Keep the downstream
# vocabulary in ``error_code``; these sets only make the recovery decision
# deterministic at this shared contract boundary.
_KNOWN_NON_APPLYING_CODES = frozenset({
    "VALIDATION_ERROR",
    "INVALID_ARGUMENT",
    "INVALID_REQUEST",
    "BAD_REQUEST",
    "INVALID_TOOL_ARGUMENT",
    "TOOL_ARGUMENT_VALIDATION_FAILED",
    "PRE_EXECUTION_VALIDATION_FAILED",
    "MISSING_REQUIRED_FIELD",
    "FIELD_TOO_LONG",
    "INVALID_DRAFT_METADATA",
    "PERMANENT_INPUT",
    "AUTH_FAILURE",
    "AUTHENTICATION_FAILED",
    "AUTHENTICATION_REQUIRED",
    "UNAUTHORIZED",
    "TOKEN_EXPIRED",
    "INVALID_TOKEN",
    "PERMISSION_DENIED",
    "AUTHORIZATION_DENIED",
    "FORBIDDEN",
    "NOT_FOUND",
    "ARTIFACT_NOT_FOUND",
    "TASK_NOT_FOUND",
    "DRAFT_NOT_FOUND",
    "SCHEDULE_NOT_FOUND",
    "CONFLICT",
    "STATE_CONFLICT",
    "DRAFT_VERSION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "VERSION_CONFLICT",
    "BUSINESS_REJECTED",
    "BUSINESS_RULE_REJECTED",
    # Runtime/agent failures are terminal diagnostics by default.  They may
    # opt into reconciliation only when the adapter supplies explicit
    # POSSIBLE/CONFIRMED side-effect evidence.
    "INTERNAL_ERROR",
    "TOOL_EXECUTION_FAILED",
    "RUN_FAILED",
    "MODEL_REQUEST_FAILED",
})

_AUTH_CODES = frozenset({
    "AUTH_FAILURE",
    "AUTHENTICATION_FAILED",
    "AUTHENTICATION_REQUIRED",
    "UNAUTHORIZED",
    "TOKEN_EXPIRED",
    "INVALID_TOKEN",
})


def _state_value(result: ToolResult[Any], key: str) -> Any:
    state = result.state
    if not isinstance(state, dict):
        return None
    return state.get(key)


def _evidence_mapping(evidence: Any) -> dict[str, Any]:
    if isinstance(evidence, dict):
        return dict(evidence)
    model_dump = getattr(evidence, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dict(dumped)
    return {}


def _evidence_value(evidence: Any, key: str) -> Any:
    return _evidence_mapping(evidence).get(key)


def _resolve_side_effect_state(
    result: ToolResult[Any],
    explicit: SideEffectState | str | None,
    evidence: Any = None,
    *,
    code: str = "",
) -> SideEffectState:
    raw = explicit
    if raw is None:
        raw = _evidence_value(evidence, "side_effect_state")
    if raw is None:
        raw = _state_value(result, "side_effect_state")
    if raw is not None:
        try:
            return SideEffectState(str(raw).upper())
        except ValueError as exc:
            raise ValueError(f"Unknown side_effect_state: {raw!r}") from exc

    # A known validation/business/auth response is a completed observation
    # that the requested mutation was not applied.  ``request_sent=True``
    # only proves that Java saw the request; it does not make a known 4xx
    # rejection a RESULT_UNKNOWN operation.
    if code in _KNOWN_NON_APPLYING_CODES:
        return SideEffectState.NOT_STARTED

    started = _evidence_value(evidence, "side_effect_started")
    if started is None:
        started = _state_value(result, "side_effect_started")
    if started is True:
        return SideEffectState.POSSIBLE
    if started is False:
        return SideEffectState.NOT_STARTED

    if result.request_sent is None:
        return SideEffectState.UNKNOWN
    if result.request_sent:
        return SideEffectState.POSSIBLE
    return SideEffectState.NOT_STARTED


def _resolve_dependency(
    result: ToolResult[Any],
    code: str,
    explicit: str | None,
) -> str:
    hinted = explicit or _state_value(result, "dependency")
    if hinted:
        return str(hinted)

    dependency = _CLASSIFICATIONS.get(code, ("", RecoveryAction.FAIL, False))[0]
    if dependency:
        return dependency
    if code in {"AUTH_FAILURE", "AUTHENTICATION_FAILED", "AUTHENTICATION_REQUIRED", "UNAUTHORIZED"}:
        return "identity"
    if code in {"RATE_LIMIT", "TOO_MANY_REQUESTS"}:
        return "external"
    return "unknown"


def _recovery_action(
    result: ToolResult[Any],
    code: str,
    side_effect_state: SideEffectState,
) -> RecoveryAction:
    classification = _CLASSIFICATIONS.get(code)
    if classification is not None:
        _, action, auth_failure = classification
        if auth_failure:
            return RecoveryAction.REAUTH
        if action == RecoveryAction.WAIT_DEPENDENCY:
            return action

    if code in _AUTH_CODES:
        return RecoveryAction.REAUTH
    if code in {
        "INTERNAL_ERROR",
        "TOOL_EXECUTION_FAILED",
        "RUN_FAILED",
        "MODEL_REQUEST_FAILED",
    }:
        if side_effect_state in {
            SideEffectState.POSSIBLE,
            SideEffectState.CONFIRMED,
        }:
            return RecoveryAction.RECONCILE
        return RecoveryAction.FAIL
    if code in _KNOWN_NON_APPLYING_CODES:
        return RecoveryAction.FAIL

    # A possible/unknown write must be reconciled before any replay, even if
    # the downstream result advertised retryable=True.
    if side_effect_state in {
        SideEffectState.POSSIBLE,
        SideEffectState.UNKNOWN,
        SideEffectState.CONFIRMED,
    }:
        return RecoveryAction.RECONCILE

    if result.retryable:
        return RecoveryAction.RETRY
    return RecoveryAction.FAIL


def normalize_external_failure(
    result: ToolResult[Any],
    *,
    dependency: str | None = None,
    side_effect_state: SideEffectState | str | None = None,
    evidence: Any = None,
) -> ExternalAgentFailure:
    """Convert one failed ``ToolResult`` into an immutable failure fact.

    The function is pure: it only reads *result* and optional classification
    hints.  It raises for successful results because a success is not an
    external failure and must not be silently reclassified.
    """

    if result.ok:
        raise ValueError("Cannot normalise a successful ToolResult")

    evidence_data = _evidence_mapping(evidence)
    original_code = str(
        evidence_data.get("error_code")
        or result.code
        or "UNKNOWN_ERROR"
    )
    code = original_code.strip().upper()
    effect = _resolve_side_effect_state(
        result,
        side_effect_state,
        evidence,
        code=code,
    )
    action = _recovery_action(result, code, effect)
    classification = _CLASSIFICATIONS.get(code)

    # Authentication failures are deterministic until a new credential is
    # supplied; never preserve an unsafe retryable=True hint for them.
    retryable = (
        False
        if code in _KNOWN_NON_APPLYING_CODES
        or (classification and classification[2])
        else bool(result.retryable)
    )
    state = result.state if isinstance(result.state, dict) else {}
    metadata = dict(state)
    if evidence_data:
        metadata["evidence"] = evidence_data

    request_sent = evidence_data.get("request_sent", result.request_sent)
    trace_id = (
        evidence_data.get("trace_id")
        if "trace_id" in evidence_data
        else result.trace_id
    )
    receipt_id = (
        evidence_data.get("receipt_id")
        if "receipt_id" in evidence_data
        else result.receipt_id
    )
    external_key = evidence_data.get("external_idempotency_key")
    if external_key is None:
        external_key = evidence_data.get("idempotency_key")
    if external_key is None and state.get("idempotency_key") is not None:
        external_key = state["idempotency_key"]
    phase = (
        str(evidence_data["phase"])
        if evidence_data.get("phase") is not None
        else str(state["phase"]) if state.get("phase") is not None else None
    )

    return ExternalAgentFailure(
        dependency=_resolve_dependency(result, code, dependency),
        error_code=original_code,
        retryable=retryable,
        user_visible_message=(
            result.user_message or result.message or "External dependency failed"
        ),
        recovery_action=action,
        request_sent=request_sent,
        side_effect_state=effect,
        message=result.message,
        phase=phase,
        trace_id=trace_id,
        receipt_id=receipt_id,
        idempotency_key=str(external_key) if external_key is not None else None,
        metadata=metadata,
        evidence=evidence_data or None,
    )


class FailureNormalizer:
    """Named façade for callers that prefer a normalizer object."""

    @staticmethod
    def normalize(
        result: ToolResult[Any],
        *,
        dependency: str | None = None,
        side_effect_state: SideEffectState | str | None = None,
        evidence: Any = None,
    ) -> ExternalAgentFailure:
        return normalize_external_failure(
            result,
            dependency=dependency,
            side_effect_state=side_effect_state,
            evidence=evidence,
        )


__all__ = [
    "ExternalAgentFailure",
    "FailureNormalizer",
    "RecoveryAction",
    "SideEffectState",
    "normalize_external_failure",
]
