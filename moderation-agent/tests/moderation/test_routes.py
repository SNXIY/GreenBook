from uuid import uuid4

from agents.moderation.routes import (
    decision_route,
    detect_evidence_conflict,
    should_use_adversarial_review,
)
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    ModerationAction,
    PolicyEvidence,
    RiskClassification,
    RiskType,
)


def make_decision(
    risk_type: RiskType,
    score: float,
    confidence: float,
    *,
    evidence_complete: bool,
    action: ModerationAction | None = None,
) -> AgentDecision:
    policies = []
    if evidence_complete and risk_type != RiskType.NORMAL:
        policies = [
            PolicyEvidence(
                policy_id="52cf8581-5fe7-43b2-a52d-a965b538e57b",
                code="TEST-001",
                title="Test policy",
                excerpt="Test evidence",
                score=0.9,
                risk_type=risk_type,
                default_action=ModerationAction.REJECT,
                version=1,
            )
        ]
    return AgentDecision(
        risk_type=risk_type,
        risk_score=score,
        confidence=confidence,
        recommended_action=(
            action
            if action is not None
            else (
                ModerationAction.PASS if risk_type == RiskType.NORMAL else ModerationAction.REJECT
            )
        ),
        reason="Test decision",
        matched_policies=policies,
        evidence_complete=evidence_complete,
    )


def test_low_risk_normal_content_is_auto_passed() -> None:
    decision = make_decision(RiskType.NORMAL, 0.1, 0.9, evidence_complete=True)
    assert decision_route({"agent_decision": decision}) == "auto_pass"


def test_high_risk_content_with_evidence_is_auto_rejected() -> None:
    decision = make_decision(RiskType.ABUSE, 0.9, 0.9, evidence_complete=True)
    assert decision_route({"agent_decision": decision}) == "auto_reject"


def test_high_risk_content_without_evidence_requires_human_review() -> None:
    decision = make_decision(RiskType.ABUSE, 0.9, 0.9, evidence_complete=False)
    assert decision_route({"agent_decision": decision}) == "human_review"


def test_medium_risk_content_requires_human_review() -> None:
    decision = make_decision(RiskType.ADVERTISING, 0.5, 0.7, evidence_complete=True)
    assert decision_route({"agent_decision": decision}) == "human_review"


def test_explicit_human_review_action_has_priority_over_score() -> None:
    decision = make_decision(
        RiskType.ABUSE,
        0.95,
        0.95,
        evidence_complete=True,
        action=ModerationAction.HUMAN_REVIEW,
    )
    assert decision_route({"agent_decision": decision}) == "human_review"


def test_supported_limit_decision_is_automated() -> None:
    decision = make_decision(
        RiskType.ADVERTISING,
        0.65,
        0.88,
        evidence_complete=True,
        action=ModerationAction.LIMIT,
    )
    assert decision_route({"agent_decision": decision}) == "auto_limit"


def review_state(
    risk_type: RiskType,
    score: float,
    confidence: float,
    *,
    policy_action: ModerationAction | None = None,
) -> dict:
    policies = []
    if policy_action is not None:
        policies.append(
            PolicyEvidence(
                policy_id=uuid4(),
                code="ROUTE-001",
                title="Route policy",
                excerpt="Route evidence",
                score=0.9,
                risk_type=risk_type,
                default_action=policy_action,
                version=1,
            ).model_dump(mode="json")
        )
    return {
        "classification": RiskClassification(
            risk_type=risk_type,
            risk_score=score,
            confidence=confidence,
        ).model_dump(mode="json"),
        "matched_policies": policies,
        "similar_cases": [],
    }


def test_clear_normal_content_skips_adversarial_review() -> None:
    state = review_state(RiskType.NORMAL, 0.1, 0.9)
    assert should_use_adversarial_review(state) is False


def test_clear_high_risk_with_policy_skips_adversarial_review() -> None:
    state = review_state(
        RiskType.ADVERTISING,
        0.9,
        0.9,
        policy_action=ModerationAction.REJECT,
    )
    assert should_use_adversarial_review(state) is False


def test_medium_risk_content_uses_adversarial_review() -> None:
    state = review_state(
        RiskType.ADVERTISING,
        0.6,
        0.85,
        policy_action=ModerationAction.REJECT,
    )
    assert should_use_adversarial_review(state) is True


def test_low_confidence_content_uses_adversarial_review() -> None:
    state = review_state(
        RiskType.ABUSE,
        0.9,
        0.7,
        policy_action=ModerationAction.REJECT,
    )
    assert should_use_adversarial_review(state) is True


def test_conflicting_policy_and_case_are_detected() -> None:
    state = review_state(
        RiskType.ADVERTISING,
        0.9,
        0.9,
        policy_action=ModerationAction.REJECT,
    )
    state["similar_cases"] = [
        CaseEvidence(
            case_id=uuid4(),
            content_excerpt="A comparable permitted example",
            risk_type=RiskType.ADVERTISING,
            final_action=ModerationAction.PASS,
            score=0.9,
        ).model_dump(mode="json")
    ]

    assert detect_evidence_conflict(state) is True
    assert should_use_adversarial_review(state) is True
