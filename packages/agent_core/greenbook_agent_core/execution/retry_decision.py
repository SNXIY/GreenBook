"""Evidence-aware retry decisions and persisted failure evidence lookup.

The existing ``RecoveryPolicy`` is intentionally left as a compatibility
policy for older callers.  This module is the single safety gate for new
retry/recovery entry points.  It never performs a retry, sleeps, calls an
external service, or mutates Execution state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from greenbook_contracts import (
    ExternalAgentFailure,
    SideEffectState,
)
from pydantic import BaseModel, ConfigDict, Field

from .events import EventType
from .evidence import ExecutionEvidence
from .failure_decision import FailureCategory, FailureClassifier
from .models import StepExecution


class RetryContext(BaseModel):
    """Immutable runtime inputs used by ``RetryDecisionEngine``."""

    model_config = ConfigDict(frozen=True)

    attempt: int = Field(default=1, ge=1)
    retry_budget: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    execution_deadline: datetime | None = None
    capability: str = ""
    tool_name: str | None = None
    source: str = "retry"
    user_requested_retry: bool = False
    contract_retry_allowed: bool | None = None
    backoff_seconds: float = Field(default=0.0, ge=0.0)
    max_backoff_seconds: float = Field(default=300.0, ge=0.0)


class RetryDecision(BaseModel):
    """A pure, auditable authorization result for one retry attempt."""

    model_config = ConfigDict(frozen=True)

    allowed: bool = False
    reason: str
    retry_after: datetime | None = None
    max_attempts: int = Field(default=1, ge=1)
    backoff: float = Field(default=0.0, ge=0.0)
    requires_reconciliation: bool = False
    requires_user_confirmation: bool = False
    evidence_requirements: tuple[str, ...] = ()

    category: FailureCategory
    raw_error_code: str
    attempt: int = Field(default=1, ge=1)
    retry_budget: int = Field(default=0, ge=0)
    operation_id: str | None = None


class FailureEvidenceSnapshot(BaseModel):
    """Failure plus the Evidence recovered from the latest execution event."""

    model_config = ConfigDict(frozen=True)

    failure: ExternalAgentFailure
    evidence: ExecutionEvidence | None = None
    event_id: str | None = None


class RetryDecisionEngine:
    """Apply the common fail-closed retry safety matrix."""

    _TRANSIENT_CATEGORIES = frozenset({
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.TIMEOUT,
        FailureCategory.NETWORK_ERROR,
        FailureCategory.RATE_LIMIT,
    })

    def __init__(
        self,
        classifier: FailureClassifier | None = None,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._classifier = classifier or FailureClassifier()
        self._now = now_factory or (lambda: datetime.now(UTC))

    def decide(
        self,
        failure: ExternalAgentFailure,
        context: RetryContext | None = None,
        *,
        evidence: ExecutionEvidence | None = None,
    ) -> RetryDecision:
        """Return a retry authorization without performing any side effect."""

        ctx = context or RetryContext()
        resolved_evidence = evidence or evidence_from_failure(failure)
        classification = self._classifier.classify(failure)
        operation_id = (
            resolved_evidence.operation_id if resolved_evidence is not None else None
        )
        base = {
            "category": classification.category,
            "raw_error_code": classification.raw_error_code,
            "attempt": ctx.attempt,
            "retry_budget": ctx.retry_budget,
            "max_attempts": ctx.max_attempts,
            "operation_id": operation_id,
        }

        if resolved_evidence is None:
            return RetryDecision(
                **base,
                reason=(
                    "Retry denied: request delivery and side-effect evidence are "
                    "missing; automatic replay is fail-closed."
                ),
                requires_reconciliation=True,
                evidence_requirements=("request_sent", "side_effect_state"),
            )

        request_sent = resolved_evidence.request_sent
        side_effect_state = resolved_evidence.side_effect_state
        if (
            request_sent is not False
            or side_effect_state not in {
                SideEffectState.NONE,
                SideEffectState.NOT_STARTED,
            }
        ):
            requirements = self._ambiguous_requirements(
                request_sent,
                side_effect_state,
            )
            return RetryDecision(
                **base,
                reason=(
                    "Retry denied: the external delivery boundary is not proven "
                    "safe; reconcile the operation before replay."
                ),
                requires_reconciliation=True,
                evidence_requirements=requirements,
            )

        if classification.category not in self._TRANSIENT_CATEGORIES:
            return RetryDecision(
                **base,
                reason=(
                    f"Retry denied: {classification.category.value} is not a "
                    "transient retry category."
                ),
            )

        if not failure.retryable or not classification.retryable:
            return RetryDecision(
                **base,
                reason=(
                    "Retry denied: the failure fact is not marked as a safe "
                    "retry candidate."
                ),
            )

        if classification.requires_reconciliation:
            return RetryDecision(
                **base,
                reason=(
                    "Retry denied: the failure classification requires "
                    "reconciliation before replay."
                ),
                requires_reconciliation=True,
                evidence_requirements=("side_effect_state",),
            )

        if ctx.contract_retry_allowed is False:
            return RetryDecision(
                **base,
                reason="Retry denied: the tool contract does not allow replay.",
                evidence_requirements=("tool_contract.retry_policy",),
            )

        if ctx.retry_budget <= 0 or ctx.attempt > ctx.max_attempts:
            return RetryDecision(
                **base,
                reason="Retry denied: the attempt budget is exhausted.",
            )

        backoff = self._backoff(ctx)
        retry_after = self._now() + timedelta(seconds=backoff) if backoff else None
        if (
            retry_after is not None
            and ctx.execution_deadline is not None
            and retry_after > ctx.execution_deadline
        ):
            return RetryDecision(
                **base,
                reason="Retry denied: the computed retry time exceeds the execution deadline.",
                backoff=backoff,
                retry_after=retry_after,
            )

        return RetryDecision(
            **base,
            allowed=True,
            reason=(
                "Retry allowed: the request was explicitly not sent, no side effect "
                "was observed, and the transient failure remains within budget."
            ),
            backoff=backoff,
            retry_after=retry_after,
        )

    def decide_for_step(
        self,
        failure: ExternalAgentFailure,
        step: StepExecution,
        *,
        evidence: ExecutionEvidence | None = None,
        source: str = "retry",
        user_requested_retry: bool = False,
        execution_deadline: datetime | None = None,
        tool_name: str | None = None,
        contract_retry_allowed: bool | None = None,
        backoff_seconds: float = 0.0,
        max_backoff_seconds: float = 300.0,
    ) -> RetryDecision:
        """Build the common context from a persisted StepExecution."""

        return self.decide(
            failure,
            RetryContext(
                attempt=step.retry_count + 1,
                retry_budget=max(0, step.max_retries - step.retry_count),
                max_attempts=max(1, step.max_retries),
                execution_deadline=execution_deadline,
                capability=step.capability,
                tool_name=tool_name,
                source=source,
                user_requested_retry=user_requested_retry,
                contract_retry_allowed=contract_retry_allowed,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
            ),
            evidence=evidence,
        )

    def _backoff(self, context: RetryContext) -> float:
        if context.backoff_seconds <= 0.0:
            return 0.0
        exponential = context.backoff_seconds * (2 ** max(0, context.attempt - 1))
        return min(exponential, context.max_backoff_seconds)

    @staticmethod
    def _ambiguous_requirements(
        request_sent: bool | None,
        side_effect_state: SideEffectState,
    ) -> tuple[str, ...]:
        requirements: list[str] = []
        if request_sent is not False:
            requirements.append("request_sent")
        if side_effect_state not in {
            SideEffectState.NONE,
            SideEffectState.NOT_STARTED,
        }:
            requirements.append("side_effect_state")
        if not requirements:
            requirements.append("authoritative_external_ledger")
        return tuple(requirements)


class RetryEvidenceResolver:
    """Recover the latest failure fact from the canonical execution events."""

    def __init__(self, event_store: Any) -> None:
        self._event_store = event_store

    def resolve(
        self,
        execution_id: str,
        step: StepExecution,
    ) -> FailureEvidenceSnapshot:
        event = self._latest_failure_event(execution_id, step)
        payload = dict(event.payload) if event is not None else {}
        raw_evidence = payload.get("evidence")
        evidence = self._parse_evidence(raw_evidence)
        error_code = str(payload.get("error_code") or step.error_code or "UNKNOWN_ERROR")
        error_message = str(payload.get("error_message") or step.error_message or "")
        request_sent = (
            evidence.request_sent
            if evidence is not None
            else payload.get("request_sent")
        )
        if not isinstance(request_sent, bool) and request_sent is not None:
            request_sent = None
        state = payload.get("state")
        if not isinstance(state, dict):
            state = {}
        if evidence is not None:
            state.setdefault("side_effect_state", evidence.side_effect_state.value)
        elif payload.get("side_effect_state") is not None:
            state.setdefault("side_effect_state", payload["side_effect_state"])
        raw: dict[str, Any] = {
            "ok": False,
            "code": error_code,
            "message": error_message,
            "user_message": error_message,
            "retryable": bool(payload.get("retryable", False)),
            "request_sent": request_sent,
            "state": state or None,
        }
        if raw_evidence is not None:
            raw["evidence"] = raw_evidence
        _, failure = _normalize_snapshot(raw, error_code, error_message, request_sent)
        return FailureEvidenceSnapshot(
            failure=failure,
            evidence=evidence,
            event_id=event.event_id if event is not None else None,
        )

    def _latest_failure_event(self, execution_id: str, step: StepExecution):
        events = self._event_store.list_events(execution_id)
        for event in reversed(events):
            if event.event_type != EventType.STEP_FAILED:
                continue
            if event.step_id != step.step_id:
                continue
            payload = event.payload or {}
            if (
                payload.get("step_execution_id")
                and payload.get("step_execution_id") != step.step_execution_id
            ):
                continue
            return event
        return None

    @staticmethod
    def _parse_evidence(raw: Any) -> ExecutionEvidence | None:
        if raw is None:
            return None
        try:
            return ExecutionEvidence.model_validate(raw)
        except (TypeError, ValueError):
            return None


def evidence_from_failure(
    failure: ExternalAgentFailure,
) -> ExecutionEvidence | None:
    """Parse the contract-safe Evidence mapping, if one was supplied."""

    if not failure.evidence:
        return None
    try:
        return ExecutionEvidence.model_validate(failure.evidence)
    except (TypeError, ValueError):
        return None


def _normalize_snapshot(
    payload: Mapping[str, Any],
    error_code: str,
    error_message: str,
    request_sent: bool | None,
) -> tuple[dict[str, Any], ExternalAgentFailure]:
    """Normalize an event payload without collapsing unknown delivery."""

    raw = dict(payload)
    raw["request_sent"] = request_sent
    return _normalizer(raw, error_code, error_message, request_sent)


def _normalizer(
    payload: Mapping[str, Any],
    error_code: str,
    error_message: str,
    request_sent: bool | None,
) -> tuple[dict[str, Any], ExternalAgentFailure]:
    from .failure_decision import normalize_failure_payload

    return normalize_failure_payload(
        payload,
        error_code=error_code,
        error_message=error_message,
        retryable=bool(payload.get("retryable", False)),
        request_sent=request_sent,
    )[0:2]


__all__ = [
    "FailureEvidenceSnapshot",
    "RetryContext",
    "RetryDecision",
    "RetryDecisionEngine",
    "RetryEvidenceResolver",
    "evidence_from_failure",
]
