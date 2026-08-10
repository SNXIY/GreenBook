"""Failure classification and policy decisions for the execution boundary.

This module is deliberately side-effect free.  It converts the failure fact
produced by the external-failure contract into a decision that a Worker can
consume.  Phase 10-D only emits ``FAIL_FAST`` or ``REQUEST_USER_INPUT``;
retry execution, dependency waiting, and reconciliation remain later
runtime responsibilities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from greenbook_contracts import (
    ExternalAgentFailure,
    SideEffectState,
    ToolResult,
    normalize_external_failure,
)
from greenbook_contracts import (
    RecoveryAction as ExternalRecoveryAction,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FailureCategory(StrEnum):
    """Stable root-cause categories used by the runtime policy."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AUTH_FAILURE = "AUTH_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK_ERROR = "NETWORK_ERROR"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    SIDE_EFFECT_UNKNOWN = "SIDE_EFFECT_UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    STATE_CONFLICT = "STATE_CONFLICT"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    UNKNOWN = "UNKNOWN"


class RecoveryAction(StrEnum):
    """Actions currently understood by the Phase 10-D Worker boundary."""

    FAIL_FAST = "FAIL_FAST"
    REQUEST_USER_INPUT = "REQUEST_USER_INPUT"


class FailureClassification(BaseModel):
    """The answer to: ``what kind of failure is this?``"""

    model_config = ConfigDict(frozen=True)

    category: FailureCategory
    raw_error_code: str
    dependency: str
    retryable: bool
    recovery_action: ExternalRecoveryAction
    side_effect_risk: SideEffectState
    request_sent: bool | None
    requires_reconciliation: bool = False
    requires_human: bool = False
    rationale: str = ""


class FailurePolicyContext(BaseModel):
    """Execution-time inputs for the policy, separate from failure facts."""

    model_config = ConfigDict(frozen=True)

    attempt: int = Field(default=1, ge=1)
    retry_budget: int = Field(default=0, ge=0)
    execution_deadline: datetime | None = None
    capability: str = ""
    tool_name: str | None = None
    has_side_effect: bool = False
    idempotent: bool = False
    idempotency_key: str | None = None
    supports_reconciliation: bool = False
    source: str = "execution_worker"
    user_requested_retry: bool = False
    user_input_allowed: bool = False
    policy_version: str = "phase-10-d.v1"


class RecoveryDecision(BaseModel):
    """The policy result consumed by the Worker.

    ``retry_allowed`` is an eligibility signal only.  Phase 10-D does not
    invoke a retry, schedule backoff, or mutate a retry/waiting state.
    """

    model_config = ConfigDict(frozen=True)

    category: FailureCategory
    action: RecoveryAction
    retry_allowed: bool = False
    wait_allowed: bool = False
    reconciliation_required: bool = False
    human_required: bool = False
    reason: str
    raw_error_code: str
    user_visible_message: str = ""
    classification: FailureClassification


class FailureClassifier:
    """Classify an ``ExternalAgentFailure`` without performing any action."""

    _INVALID_ARGUMENT_CODES = frozenset({
        "INVALID_ARGUMENT",
        "VALIDATION_ERROR",
        "INVALID_TOOL_ARGUMENT",
        "TOOL_ARGUMENT_VALIDATION_FAILED",
        "PRE_EXECUTION_VALIDATION_FAILED",
        "MISSING_REQUIRED_FIELD",
    })
    _AUTH_CODES = frozenset({
        "AUTH_FAILURE",
        "AUTHENTICATION_FAILED",
        "AUTHENTICATION_REQUIRED",
        "UNAUTHORIZED",
        "TOKEN_EXPIRED",
        "INVALID_TOKEN",
    })
    _PERMISSION_CODES = frozenset({
        "PERMISSION_DENIED",
        "AUTHORIZATION_DENIED",
        "FORBIDDEN",
    })
    _DEPENDENCY_CODES = frozenset({
        "DEPENDENCY_UNAVAILABLE",
        "JAVA_BACKEND_UNAVAILABLE",
        "CREATOR_UNAVAILABLE",
        "MCP_UNAVAILABLE",
        "TEMPORARY_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
    })
    _TIMEOUT_CODES = frozenset({
        "TIMEOUT",
        "CREATOR_TIMEOUT",
        "MCP_TIMEOUT",
        "MODEL_TIMEOUT",
        "LLM_TIMEOUT",
    })
    _RATE_LIMIT_CODES = frozenset({
        "RATE_LIMIT",
        "TOO_MANY_REQUESTS",
    })
    _NETWORK_CODES = frozenset({
        "NETWORK_ERROR",
        "REQUEST_NOT_SENT",
        "CONNECTION_ERROR",
        "DNS_ERROR",
    })
    _CONTRACT_CODES = frozenset({
        "CONTRACT_MISMATCH",
        "TOOL_OUTPUT_VALIDATION_FAILED",
        "OUTPUT_SCHEMA_MISMATCH",
        "SCHEMA_MISMATCH",
    })
    _SIDE_EFFECT_CODES = frozenset({
        "RESULT_UNKNOWN",
        "UNKNOWN_RESULT",
        "SIDE_EFFECT_UNKNOWN",
    })
    _NOT_FOUND_CODES = frozenset({
        "NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        "TASK_NOT_FOUND",
    })
    _STATE_CONFLICT_CODES = frozenset({
        "STATE_CONFLICT",
        "CONFLICT",
        "DRAFT_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
    })
    _BUSINESS_CODES = frozenset({"BUSINESS_REJECTED", "BUSINESS_RULE_REJECTED"})

    def classify(self, failure: ExternalAgentFailure) -> FailureClassification:
        """Return a stable classification while preserving the raw code."""

        raw_code = str(failure.error_code or "UNKNOWN_ERROR")
        code = raw_code.strip().upper()
        category = self._category_for(code)
        risk = failure.side_effect_state
        reconciliation_required = (
            category == FailureCategory.SIDE_EFFECT_UNKNOWN
            or risk in {
                SideEffectState.POSSIBLE,
                SideEffectState.UNKNOWN,
                SideEffectState.CONFIRMED,
            }
        )
        transient = category in {
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            FailureCategory.TIMEOUT,
            FailureCategory.RATE_LIMIT,
            FailureCategory.NETWORK_ERROR,
        }
        safe_boundary = risk in {
            SideEffectState.NONE,
            SideEffectState.NOT_STARTED,
        }
        retryable = bool(failure.retryable) and transient and safe_boundary
        requires_human = category in {
            FailureCategory.INVALID_ARGUMENT,
            FailureCategory.AUTH_FAILURE,
            FailureCategory.PERMISSION_DENIED,
            FailureCategory.CONTRACT_MISMATCH,
            FailureCategory.UNKNOWN,
        }

        if reconciliation_required:
            rationale = (
                "The failure may have crossed the external boundary; "
                "automatic replay is unsafe until the result is reconciled."
            )
        elif retryable:
            rationale = (
                "The failure is transient and the normalizer reports no "
                "known external side effect."
            )
        else:
            rationale = "The failure is not eligible for automatic replay."

        return FailureClassification(
            category=category,
            raw_error_code=raw_code,
            dependency=failure.dependency,
            retryable=retryable,
            recovery_action=self._recovery_action(category, failure),
            side_effect_risk=risk,
            request_sent=failure.request_sent,
            requires_reconciliation=reconciliation_required,
            requires_human=requires_human,
            rationale=rationale,
        )

    @classmethod
    def _category_for(cls, code: str) -> FailureCategory:
        if code in cls._AUTH_CODES:
            return FailureCategory.AUTH_FAILURE
        if code in cls._PERMISSION_CODES:
            return FailureCategory.PERMISSION_DENIED
        if code in cls._INVALID_ARGUMENT_CODES:
            return FailureCategory.INVALID_ARGUMENT
        if code in cls._CONTRACT_CODES:
            return FailureCategory.CONTRACT_MISMATCH
        if code in cls._SIDE_EFFECT_CODES:
            return FailureCategory.SIDE_EFFECT_UNKNOWN
        if code in cls._DEPENDENCY_CODES:
            return FailureCategory.DEPENDENCY_UNAVAILABLE
        if code in cls._TIMEOUT_CODES:
            return FailureCategory.TIMEOUT
        if code in cls._RATE_LIMIT_CODES:
            return FailureCategory.RATE_LIMIT
        if code in cls._NETWORK_CODES:
            return FailureCategory.NETWORK_ERROR
        if code in cls._NOT_FOUND_CODES:
            return FailureCategory.NOT_FOUND
        if code in cls._STATE_CONFLICT_CODES:
            return FailureCategory.STATE_CONFLICT
        if code in cls._BUSINESS_CODES:
            return FailureCategory.BUSINESS_REJECTED
        return FailureCategory.UNKNOWN

    @staticmethod
    def _recovery_action(
        category: FailureCategory,
        failure: ExternalAgentFailure,
    ) -> ExternalRecoveryAction:
        if category == FailureCategory.AUTH_FAILURE:
            return ExternalRecoveryAction.REAUTH
        if category == FailureCategory.RATE_LIMIT:
            return ExternalRecoveryAction.WAIT_DEPENDENCY
        if category == FailureCategory.SIDE_EFFECT_UNKNOWN:
            return ExternalRecoveryAction.RECONCILE
        if failure.side_effect_state in {
            SideEffectState.POSSIBLE,
            SideEffectState.UNKNOWN,
            SideEffectState.CONFIRMED,
        }:
            return ExternalRecoveryAction.RECONCILE
        if category in {
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            FailureCategory.TIMEOUT,
            FailureCategory.NETWORK_ERROR,
        } and failure.retryable:
            return ExternalRecoveryAction.RETRY
        return ExternalRecoveryAction.FAIL


class FailurePolicy:
    """Apply execution context to a classification and emit a decision.

    The optional ``retry_eligibility`` callback is a compatibility seam for
    the existing explicit retry gate.  It only sets an advisory flag on the
    decision; this policy never invokes retry logic.
    """

    _USER_INPUT_CATEGORIES = frozenset({
        FailureCategory.INVALID_ARGUMENT,
        FailureCategory.AUTH_FAILURE,
        FailureCategory.PERMISSION_DENIED,
    })

    def __init__(
        self,
        retry_eligibility: Callable[[str], bool] | None = None,
    ) -> None:
        self._retry_eligibility = retry_eligibility or (lambda _code: False)

    def decide(
        self,
        classification: FailureClassification,
        context: FailurePolicyContext,
        *,
        user_visible_message: str = "",
    ) -> RecoveryDecision:
        request_input = (
            context.user_input_allowed
            and classification.category in self._USER_INPUT_CATEGORIES
        )
        reconciliation_required = classification.requires_reconciliation

        legacy_retry_eligible = (
            classification.retryable
            and not reconciliation_required
            and context.retry_budget > 0
            and self._retry_eligibility(classification.raw_error_code)
        )

        if request_input:
            action = RecoveryAction.REQUEST_USER_INPUT
            reason = (
                f"{classification.category.value} requires user-provided "
                "correction before the step can continue."
            )
        else:
            action = RecoveryAction.FAIL_FAST
            if reconciliation_required:
                reason = (
                    "FAIL_FAST for this execution pass: the failure requires "
                    "reconciliation before any replay is considered."
                )
            elif legacy_retry_eligible:
                reason = (
                    "FAIL_FAST for this execution pass: the existing retry "
                    "eligibility gate remains advisory; Phase 10-D does not "
                    "execute retry."
                )
            else:
                reason = (
                    f"FAIL_FAST: {classification.category.value} is not "
                    "handled by the Phase 10-D recovery actions."
                )

        return RecoveryDecision(
            category=classification.category,
            action=action,
            retry_allowed=legacy_retry_eligible,
            wait_allowed=False,
            reconciliation_required=reconciliation_required,
            human_required=(request_input or classification.requires_human),
            reason=reason,
            raw_error_code=classification.raw_error_code,
            user_visible_message=user_visible_message,
            classification=classification,
        )


class FailureDecisionEngine:
    """Compose normalization facts, classification, and policy decisions."""

    def __init__(
        self,
        classifier: FailureClassifier | None = None,
        policy: FailurePolicy | None = None,
        *,
        retry_eligibility: Callable[[str], bool] | None = None,
    ) -> None:
        self.classifier = classifier or FailureClassifier()
        self.policy = policy or FailurePolicy(
            retry_eligibility=retry_eligibility,
        )

    def decide(
        self,
        failure: ExternalAgentFailure,
        context: FailurePolicyContext | None = None,
    ) -> RecoveryDecision:
        policy_context = context or FailurePolicyContext()
        classification = self.classifier.classify(failure)
        return self.policy.decide(
            classification,
            policy_context,
            user_visible_message=failure.user_visible_message,
        )


def normalize_failure_payload(
    payload: Mapping[str, Any] | None = None,
    *,
    error_code: str = "UNKNOWN_ERROR",
    error_message: str = "",
    retryable: bool = False,
    request_sent: bool | None = False,
) -> tuple[dict[str, Any], ExternalAgentFailure]:
    """Build a loss-minimising failure fact from a transient tool payload.

    This adapter is intentionally at the execution boundary.  It preserves
    ``request_sent=None`` and ``state`` while a payload is still available;
    it is not a persistence format and does not alter execution state.
    """

    raw = dict(payload or {})
    raw["ok"] = False
    raw["code"] = str(raw.get("code") or error_code or "UNKNOWN_ERROR")
    raw["message"] = str(raw.get("message") or error_message or "")
    raw["user_message"] = str(
        raw.get("user_message") or raw["message"] or "External dependency failed"
    )
    raw["retryable"] = bool(raw.get("retryable", retryable))
    if "request_sent" not in raw:
        raw["request_sent"] = request_sent
    if not isinstance(raw.get("state"), dict):
        raw["state"] = None

    try:
        result = ToolResult.model_validate(raw)
        failure = normalize_external_failure(result)
    except (ValidationError, ValueError, TypeError):
        # A malformed side-effect hint must fail closed.  Preserve the code
        # and message, but use the conservative unknown-delivery evidence.
        raw["request_sent"] = None
        raw["state"] = None
        result = ToolResult.model_validate(raw)
        failure = normalize_external_failure(result)

    return result.model_dump(mode="python"), failure


__all__ = [
    "FailureCategory",
    "FailureClassification",
    "FailureClassifier",
    "FailureDecisionEngine",
    "FailurePolicy",
    "FailurePolicyContext",
    "RecoveryAction",
    "RecoveryDecision",
    "normalize_failure_payload",
]
