from dataclasses import dataclass
from typing import Literal

from agents.moderation.state import ModerationState
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    ModerationAction,
    PolicyEvidence,
    RiskClassification,
    RiskType,
)

DecisionRoute = Literal["auto_pass", "auto_reject", "auto_limit", "human_review"]
ReviewNode = Literal["judge", "risk_investigator", "safe_advocate"]
ReviewRoute = ReviewNode | list[ReviewNode]


@dataclass(frozen=True)
class ModerationThresholds:
    pass_score_max: float = 0.20
    reject_score_min: float = 0.80
    auto_pass_confidence_min: float = 0.70
    auto_reject_confidence_min: float = 0.80
    auto_limit_confidence_min: float = 0.80
    adversarial_score_min: float = 0.40
    adversarial_score_max: float = 0.85
    adversarial_confidence_min: float = 0.80


DEFAULT_THRESHOLDS = ModerationThresholds()


def policy_risk_types(state: ModerationState) -> list[RiskType]:
    classification = RiskClassification.model_validate(state["classification"])
    if classification.risk_type == RiskType.NORMAL:
        return [RiskType.NORMAL]
    return [classification.risk_type]


def detect_evidence_conflict(state: ModerationState) -> bool:
    classification = RiskClassification.model_validate(state["classification"])
    try:
        policies = [
            PolicyEvidence.model_validate(value) for value in state.get("matched_policies", [])
        ]
        cases = [CaseEvidence.model_validate(value) for value in state.get("similar_cases", [])]
    except (TypeError, ValueError):
        return True

    if any(
        policy.risk_type is not None and policy.risk_type != classification.risk_type
        for policy in policies
    ):
        return True
    if any(case.risk_type != classification.risk_type for case in cases):
        return True

    policy_actions = {
        policy.default_action for policy in policies if policy.default_action is not None
    }
    case_actions = {case.final_action for case in cases}
    if len(policy_actions) > 1 or len(case_actions) > 1:
        return True
    return bool(policy_actions and case_actions and policy_actions != case_actions)


def should_use_adversarial_review(
    state: ModerationState,
    thresholds: ModerationThresholds = DEFAULT_THRESHOLDS,
) -> bool:
    classification = RiskClassification.model_validate(state["classification"])
    evidence_conflict = bool(state.get("evidence_conflict")) or detect_evidence_conflict(state)
    context = state.get("context_evidence")
    context_incomplete = isinstance(context, dict) and context.get("complete") is False

    if evidence_conflict or context_incomplete:
        return True

    if (
        classification.risk_type == RiskType.NORMAL
        and classification.risk_score <= thresholds.pass_score_max
        and classification.confidence >= thresholds.auto_pass_confidence_min
    ):
        return False

    policies = [PolicyEvidence.model_validate(value) for value in state.get("matched_policies", [])]
    has_enforcement_policy = any(
        policy.risk_type in {None, classification.risk_type}
        and policy.default_action not in {ModerationAction.PASS, ModerationAction.HUMAN_REVIEW}
        for policy in policies
    )
    if (
        classification.risk_type != RiskType.NORMAL
        and classification.risk_score >= thresholds.reject_score_min
        and classification.confidence >= thresholds.auto_reject_confidence_min
        and has_enforcement_policy
    ):
        return False

    return (
        thresholds.adversarial_score_min
        <= classification.risk_score
        <= thresholds.adversarial_score_max
        or classification.confidence < thresholds.adversarial_confidence_min
        or (classification.risk_type != RiskType.NORMAL and not has_enforcement_policy)
        or (
            classification.risk_type == RiskType.NORMAL
            and classification.risk_score > thresholds.adversarial_score_max
        )
    )


def review_mode_route(state: ModerationState) -> ReviewRoute:
    if state.get("use_adversarial_review", False):
        return ["risk_investigator", "safe_advocate"]
    return "judge"


def decision_route(
    state: ModerationState,
    thresholds: ModerationThresholds = DEFAULT_THRESHOLDS,
) -> DecisionRoute:
    decision = AgentDecision.model_validate(state["agent_decision"])

    if (
        state.get("requires_human_review", False)
        or decision.needs_context_review
        or decision.recommended_action == ModerationAction.HUMAN_REVIEW
    ):
        return "human_review"

    if (
        decision.recommended_action == ModerationAction.PASS
        and decision.risk_type == RiskType.NORMAL
        and decision.risk_score <= thresholds.pass_score_max
        and decision.confidence >= thresholds.auto_pass_confidence_min
    ):
        return "auto_pass"

    if (
        decision.recommended_action == ModerationAction.REJECT
        and decision.risk_type != RiskType.NORMAL
        and decision.risk_score >= thresholds.reject_score_min
        and decision.confidence >= thresholds.auto_reject_confidence_min
        and decision.evidence_complete
    ):
        return "auto_reject"

    if (
        decision.recommended_action == ModerationAction.LIMIT
        and decision.risk_type != RiskType.NORMAL
        and decision.risk_score >= thresholds.adversarial_score_min
        and decision.confidence >= thresholds.auto_limit_confidence_min
        and decision.evidence_complete
    ):
        return "auto_limit"

    return "human_review"


def final_action_for_route(route: DecisionRoute) -> ModerationAction:
    if route == "auto_pass":
        return ModerationAction.PASS
    if route == "auto_reject":
        return ModerationAction.REJECT
    if route == "auto_limit":
        return ModerationAction.LIMIT
    return ModerationAction.HUMAN_REVIEW
