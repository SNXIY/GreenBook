import hashlib
import json
from typing import Any, Literal

from agents.moderation.state import ModerationState
from moderation.schemas import (
    AgentDecision,
    ConditionTruthValue,
    EvidenceClaim,
    EvidenceItem,
    EvidenceLedger,
    EvidenceSourceType,
    EvidenceStance,
    ModerationAction,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyApplicability,
    PolicyConditionEvaluation,
    PolicyConditionKind,
    PolicyEngineDisposition,
    PolicyEngineResult,
    PolicyEvidence,
    PolicyGradeResult,
    ReasoningTier,
    RiskClassification,
    RiskType,
)

from .dependencies import ModerationDependencies


class PolicyEngineNodes:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies

    async def build_evidence_ledger(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        classification = RiskClassification.model_validate(state["classification"])
        claims = [
            *_classification_claims(state["normalized_content"], classification),
            *_signal_claims(state),
            *_context_claims(state),
            *_collection_claims(state),
        ]
        conditions, grade_claims = _policy_condition_evaluations(state, decision)
        claims.extend(grade_claims)
        claims = list({claim.claim_id: claim for claim in claims}.values())[:200]
        unresolved = [
            evaluation.condition
            for evaluation in conditions
            if evaluation.value == ConditionTruthValue.UNKNOWN
        ]
        if classification.risk_type == RiskType.NORMAL:
            complete = bool(claims) and not _context_is_incomplete(state)
        else:
            complete = bool(decision.matched_policies) and not unresolved
        ledger = EvidenceLedger(
            claims=claims,
            policy_conditions=conditions,
            complete=complete,
            unresolved_conditions=list(dict.fromkeys(unresolved))[:100],
        )
        return {"evidence_ledger": ledger.model_dump(mode="json")}

    async def apply_policy_engine(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        ledger = EvidenceLedger.model_validate(state["evidence_ledger"])
        result = _evaluate_policy_decision(
            state,
            decision,
            ledger,
            min_semantic_confidence=(
                self.dependencies.policy_rag_config.grader_min_confidence
            ),
        )
        checked = decision.model_copy(
            update={
                "evidence_ledger": ledger,
                "policy_engine": result,
                "evidence_complete": (
                    decision.evidence_complete and result.decision_supported
                ),
            }
        )
        requires_human = bool(
            state.get("requires_human_review", False)
            or not result.auto_action_eligible
        )
        return {
            "agent_decision": checked.model_dump(mode="json"),
            "policy_engine_result": result.model_dump(mode="json"),
            "evidence_complete": checked.evidence_complete,
            "requires_human_review": requires_human,
        }

    def route_after_policy_engine(
        self,
        state: ModerationState,
    ) -> Literal["finalize", "review"]:
        if not state.get("adaptive_cascade_enabled", False):
            return "review"
        tier = ReasoningTier(state.get("reasoning_tier", ReasoningTier.LEGACY.value))
        result = PolicyEngineResult.model_validate(state["policy_engine_result"])
        can_finalize = (
            result.auto_action_eligible
            and state.get("evidence_check_passed", False)
            and not state.get("requires_human_review", False)
        )
        if tier in {ReasoningTier.FAST, ReasoningTier.STANDARD} and can_finalize:
            return "finalize"
        # When semantic Evidence Reviewer is off, DEEP may also finalize once the
        # deterministic Policy Engine has fully supported the Judge action.
        if (
            tier == ReasoningTier.DEEP
            and can_finalize
            and not self.dependencies.evidence_reviewer_config.enabled
        ):
            return "finalize"
        return "review"


def _classification_claims(
    content: str,
    classification: RiskClassification,
) -> list[EvidenceClaim]:
    if not classification.indicators:
        return [
            _claim(
                claim=(
                    f"The classifier found no explicit {classification.risk_type.value} "
                    "indicator in the current content."
                ),
                source_type=EvidenceSourceType.CONTENT,
                source_id="current-content:classification",
                stance=(
                    EvidenceStance.SUPPORTS
                    if classification.risk_type == RiskType.NORMAL
                    else EvidenceStance.NEUTRAL
                ),
                confidence=classification.confidence,
                provenance=["risk_classifier"],
            )
        ]

    claims = []
    for index, indicator in enumerate(classification.indicators):
        start, end = _find_span(content, indicator)
        evidence_text = content[start:end] if start is not None and end is not None else None
        claims.append(
            _claim(
                claim=(
                    f"Classifier evidence supports the "
                    f"{classification.risk_type.value} hypothesis: {indicator}"
                ),
                source_type=EvidenceSourceType.CONTENT,
                source_id=f"current-content:indicator:{index}",
                stance=EvidenceStance.SUPPORTS,
                confidence=classification.confidence,
                evidence_text=evidence_text,
                span_start=start,
                span_end=end,
                provenance=["risk_classifier"],
            )
        )
    return claims


def _signal_claims(state: ModerationState) -> list[EvidenceClaim]:
    claims = []
    for index, value in enumerate(state.get("signals", [])):
        signal = ModerationSignalEvidence.model_validate(value)
        details = json.dumps(
            signal.details,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        claims.append(
            _claim(
                claim=f"Verified signal {signal.signal_type.value}: {details[:1500]}",
                source_type=EvidenceSourceType.SIGNAL,
                source_id=f"signal:{index}:{signal.signal_type.value}",
                stance=EvidenceStance.NEUTRAL,
                confidence=signal.score,
                provenance=[signal.source.value],
            )
        )
    return claims


def _context_claims(state: ModerationState) -> list[EvidenceClaim]:
    value = state.get("context_evidence")
    if not value:
        return []
    context = ModerationContextEvidence.model_validate(value)
    records = [
        *([context.current] if context.current else []),
        *([context.parent_comment] if context.parent_comment else []),
        *context.conversation_context,
    ]
    claims = [
        _claim(
            claim=f"Verified context record {record.content_id} is available.",
            source_type=EvidenceSourceType.CONTEXT,
            source_id=record.content_id,
            stance=EvidenceStance.NEUTRAL,
            confidence=1.0,
            evidence_text=record.content[:1000] or None,
            provenance=["community_context"],
        )
        for record in records[:20]
    ]
    if not context.complete:
        claims.append(
            _claim(
                claim="Required community context is incomplete.",
                source_type=EvidenceSourceType.CONTEXT,
                source_id="community-context:incomplete",
                stance=EvidenceStance.NEUTRAL,
                confidence=1.0,
                provenance=context.errors or ["community_context"],
            )
        )
    return claims


def _collection_claims(state: ModerationState) -> list[EvidenceClaim]:
    summary = state.get("evidence_summary")
    if not isinstance(summary, dict):
        return []
    result = summary.get("collection_result")
    if not isinstance(result, dict):
        return []
    claims = []
    for index, value in enumerate(result.get("collected_evidence", [])):
        try:
            item = EvidenceItem.model_validate(value)
        except (TypeError, ValueError):
            continue
        claims.append(
            _claim(
                claim=item.summary,
                source_type=EvidenceSourceType.TOOL,
                source_id=f"{item.source}:{index}",
                stance=EvidenceStance.SUPPORTS,
                confidence=item.confidence,
                evidence_text=item.quote,
                policy_id=_optional_policy_id(item.policy_id),
                provenance=[item.source],
            )
        )
    return claims


def _policy_condition_evaluations(
    state: ModerationState,
    decision: AgentDecision,
) -> tuple[list[PolicyConditionEvaluation], list[EvidenceClaim]]:
    grade_value = state.get("policy_grade_result")
    grade_result = PolicyGradeResult.model_validate(grade_value) if grade_value else None
    grades = (
        {item.policy_id: item for item in grade_result.item_grades}
        if grade_result is not None
        else {}
    )
    evaluations: list[PolicyConditionEvaluation] = []
    claims: list[EvidenceClaim] = []

    for policy in decision.matched_policies:
        grade = grades.get(policy.policy_id)
        matched = grade.matched_conditions if grade else []
        missing = grade.missing_conditions if grade else []
        triggered = grade.exclusion_conditions_triggered if grade else []
        for index, condition in enumerate(policy.applicability_conditions):
            value, reason = _applicability_value(condition, grade, matched, missing)
            claim_ids = []
            if value == ConditionTruthValue.TRUE and grade is not None:
                claim = _condition_claim(
                    policy,
                    condition,
                    grade.confidence,
                    index=index,
                    kind=PolicyConditionKind.APPLICABILITY,
                )
                claims.append(claim)
                claim_ids.append(claim.claim_id)
            evaluations.append(
                PolicyConditionEvaluation(
                    policy_id=policy.policy_id,
                    condition_kind=PolicyConditionKind.APPLICABILITY,
                    condition=condition,
                    value=value,
                    evidence_claim_ids=claim_ids,
                    confidence=grade.confidence if grade else 0.0,
                    reason=reason,
                )
            )
        for index, condition in enumerate(policy.exclusion_conditions):
            value, reason = _exclusion_value(condition, grade, triggered)
            claim_ids = []
            if value == ConditionTruthValue.TRUE and grade is not None:
                claim = _condition_claim(
                    policy,
                    condition,
                    grade.confidence,
                    index=index,
                    kind=PolicyConditionKind.EXCLUSION,
                )
                claims.append(claim)
                claim_ids.append(claim.claim_id)
            evaluations.append(
                PolicyConditionEvaluation(
                    policy_id=policy.policy_id,
                    condition_kind=PolicyConditionKind.EXCLUSION,
                    condition=condition,
                    value=value,
                    evidence_claim_ids=claim_ids,
                    confidence=grade.confidence if grade else 0.0,
                    reason=reason,
                )
            )
    return evaluations[:200], claims[:200]


def _evaluate_policy_decision(
    state: ModerationState,
    decision: AgentDecision,
    ledger: EvidenceLedger,
    *,
    min_semantic_confidence: float,
) -> PolicyEngineResult:
    reasons: list[str] = []
    classification = RiskClassification.model_validate(state["classification"])
    blockers = bool(
        state.get("requires_human_review", False)
        or not state.get("evidence_check_passed", False)
        or _context_is_incomplete(state)
        or classification.fallback_used
        or decision.model_fallback_used
    )
    if blockers:
        reasons.append("UPSTREAM_EVIDENCE_REQUIRES_REVIEW")
    if decision.model_fallback_used:
        reasons.append("JUDGE_MODEL_FALLBACK_USED")
    if classification.fallback_used:
        reasons.append("CLASSIFIER_MODEL_FALLBACK_USED")

    if decision.risk_type == RiskType.NORMAL:
        supported = bool(
            decision.recommended_action == ModerationAction.PASS
            and not blockers
            and decision.evidence_complete
        )
        return PolicyEngineResult(
            disposition=(
                PolicyEngineDisposition.ALLOW
                if supported
                else PolicyEngineDisposition.ESCALATE
            ),
            selected_action=(
                ModerationAction.PASS if supported else ModerationAction.HUMAN_REVIEW
            ),
            risk_type=decision.risk_type,
            decision_supported=supported,
            auto_action_eligible=supported,
            reason_codes=(
                ["NORMAL_FAST_PATH_SUPPORTED"]
                if supported
                else [*reasons, "NORMAL_DECISION_NOT_PROVEN"]
            ),
            reason=(
                "Deterministic evidence checks support the normal-content PASS decision."
                if supported
                else "The normal-content decision did not pass deterministic evidence gates."
            ),
        )

    policies = {
        policy.policy_id: policy
        for policy in decision.matched_policies
        if policy.risk_type in {None, decision.risk_type}
    }
    grade_value = state.get("policy_grade_result")
    grade_result = PolicyGradeResult.model_validate(grade_value) if grade_value else None
    semantic_ids: set[Any] = set()
    supported_ids: set[Any] = set()
    if grade_result is not None:
        for grade in grade_result.item_grades:
            policy = policies.get(grade.policy_id)
            if policy is None:
                continue
            if (
                grade.relevant
                and grade.applicability == PolicyApplicability.APPLICABLE
                and grade.confidence >= min_semantic_confidence
                and not grade.exclusion_conditions_triggered
            ):
                semantic_ids.add(grade.policy_id)
                if decision.recommended_action in grade.supports_actions:
                    supported_ids.add(grade.policy_id)
    else:
        supported_ids = {
            policy_id
            for policy_id, policy in policies.items()
            if decision.recommended_action in _allowed_actions(policy)
        }

    exclusion_ids = {
        evaluation.policy_id
        for evaluation in ledger.policy_conditions
        if evaluation.condition_kind == PolicyConditionKind.EXCLUSION
        and evaluation.value == ConditionTruthValue.TRUE
    }
    supported_ids.difference_update(exclusion_ids)
    semantic_verified = bool(semantic_ids)
    action_is_enforcement = decision.recommended_action in {
        ModerationAction.REJECT,
        ModerationAction.LIMIT,
    }
    supported = bool(
        action_is_enforcement
        and supported_ids
        and decision.evidence_complete
        and not blockers
    )
    if grade_result is not None and not semantic_verified:
        supported = False
        reasons.append("POLICY_CONDITIONS_NOT_SEMANTICALLY_VERIFIED")
    if not policies:
        reasons.append("NO_CURRENT_POLICY")
    if not supported_ids:
        reasons.append("ACTION_NOT_SUPPORTED_BY_POLICY")
    if exclusion_ids:
        reasons.append("POLICY_EXCLUSION_TRIGGERED")
    if not action_is_enforcement:
        reasons.append("NON_NORMAL_DECISION_REQUIRES_ENFORCEMENT_OR_REVIEW")

    selected_ids = sorted(supported_ids, key=str)
    return PolicyEngineResult(
        disposition=(
            PolicyEngineDisposition.ENFORCE
            if supported
            else PolicyEngineDisposition.ESCALATE
        ),
        selected_action=(
            decision.recommended_action if supported else ModerationAction.HUMAN_REVIEW
        ),
        risk_type=decision.risk_type,
        decision_supported=supported,
        auto_action_eligible=supported,
        policy_ids=selected_ids,
        semantic_conditions_verified=semantic_verified,
        reason_codes=(
            ["POLICY_ACTION_SUPPORTED"]
            if supported
            else list(dict.fromkeys(reasons or ["POLICY_DECISION_NOT_PROVEN"]))
        ),
        reason=(
            "Current Policy evidence supports the proposed enforcement action."
            if supported
            else "The proposed action is not fully supported by deterministic Policy gates."
        ),
    )


def _applicability_value(
    condition: str,
    grade: Any | None,
    matched: list[str],
    missing: list[str],
) -> tuple[ConditionTruthValue, str]:
    if grade is None:
        return (
            ConditionTruthValue.UNKNOWN,
            "No semantic Policy grade is available for this condition.",
        )
    if _matches_any(condition, matched):
        return ConditionTruthValue.TRUE, "The semantic Policy grade matched this condition."
    if _matches_any(condition, missing):
        return ConditionTruthValue.UNKNOWN, "The semantic Policy grade marked this condition missing."
    if (
        grade.applicability == PolicyApplicability.APPLICABLE
        and not grade.missing_conditions
    ):
        return ConditionTruthValue.TRUE, "The complete semantic grade established applicability."
    if grade.applicability == PolicyApplicability.NOT_APPLICABLE:
        return ConditionTruthValue.FALSE, "The semantic grade found the Policy inapplicable."
    return ConditionTruthValue.UNKNOWN, "The condition was not established by verified evidence."


def _exclusion_value(
    condition: str,
    grade: Any | None,
    triggered: list[str],
) -> tuple[ConditionTruthValue, str]:
    if grade is None:
        return (
            ConditionTruthValue.UNKNOWN,
            "No semantic Policy grade is available for this exclusion.",
        )
    if _matches_any(condition, triggered):
        return ConditionTruthValue.TRUE, "The semantic Policy grade triggered this exclusion."
    if grade.applicability == PolicyApplicability.APPLICABLE:
        return ConditionTruthValue.FALSE, "The semantic grade found no triggered exclusion."
    return ConditionTruthValue.UNKNOWN, "The exclusion was not conclusively evaluated."


def _condition_claim(
    policy: PolicyEvidence,
    condition: str,
    confidence: float,
    *,
    index: int,
    kind: PolicyConditionKind,
) -> EvidenceClaim:
    return _claim(
        claim=f"Policy condition evaluated as true: {condition}",
        source_type=EvidenceSourceType.POLICY_GRADE,
        source_id=f"{policy.policy_id}:{kind.value}:{index}",
        stance=(
            EvidenceStance.REFUTES
            if kind == PolicyConditionKind.EXCLUSION
            else EvidenceStance.SUPPORTS
        ),
        confidence=confidence,
        policy_id=policy.policy_id,
        policy_condition=condition,
        provenance=["policy_grader"],
    )


def _claim(
    *,
    claim: str,
    source_type: EvidenceSourceType,
    source_id: str,
    stance: EvidenceStance,
    confidence: float,
    evidence_text: str | None = None,
    span_start: int | None = None,
    span_end: int | None = None,
    policy_id: Any | None = None,
    policy_condition: str | None = None,
    provenance: list[str] | None = None,
) -> EvidenceClaim:
    signature = json.dumps(
        {
            "claim": claim,
            "source_type": source_type.value,
            "source_id": source_id,
            "policy_id": str(policy_id) if policy_id else None,
            "policy_condition": policy_condition,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return EvidenceClaim(
        claim_id=hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16],
        claim=claim,
        source_type=source_type,
        source_id=source_id,
        stance=stance,
        confidence=max(0.0, min(1.0, confidence)),
        evidence_text=evidence_text,
        span_start=span_start,
        span_end=span_end,
        policy_id=policy_id,
        policy_condition=policy_condition,
        provenance=provenance or [],
    )


def _find_span(content: str, value: str) -> tuple[int | None, int | None]:
    start = content.casefold().find(value.casefold())
    if start < 0:
        return None, None
    return start, start + len(value)


def _matches_any(condition: str, values: list[str]) -> bool:
    normalized = _normalize(condition)
    return any(
        normalized == candidate
        or normalized in candidate
        or candidate in normalized
        for value in values
        if (candidate := _normalize(value))
    )


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split()).strip(" .,:;")


def _allowed_actions(policy: PolicyEvidence) -> set[ModerationAction]:
    actions = set(policy.suggested_actions)
    if policy.default_action is not None:
        actions.add(policy.default_action)
    return actions


def _context_is_incomplete(state: ModerationState) -> bool:
    context = state.get("context_evidence")
    return isinstance(context, dict) and context.get("complete") is False


def _optional_policy_id(value: str | None) -> Any | None:
    if not value:
        return None
    try:
        from uuid import UUID

        return UUID(value)
    except ValueError:
        return None
