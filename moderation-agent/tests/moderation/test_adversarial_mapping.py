from uuid import uuid4

from agents.moderation.adversarial import map_judge_to_agent_decision
from agents.moderation.state import ModerationState
from moderation.schemas import (
    CommunityContentRecord,
    JudgeAgentResult,
    ModerationAction,
    ModerationContentType,
    ModerationContextEvidence,
    PolicyEvidence,
    RiskType,
)


def policy(risk_type: RiskType = RiskType.ADVERTISING) -> PolicyEvidence:
    return PolicyEvidence(
        policy_id=uuid4(),
        code="ADV-001",
        title="Unsolicited advertising",
        excerpt="Unsolicited promotions and off-platform sales are prohibited.",
        score=0.91,
        risk_type=risk_type,
        default_action=ModerationAction.REJECT,
        version=2,
    )


def state_with_policy(matched_policy: PolicyEvidence) -> ModerationState:
    return {
        "task_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "content": "Limited offer, buy now on Telegram.",
        "normalized_content": "Limited offer, buy now on Telegram.",
        "content_type": ModerationContentType.POST.value,
        "platform": "default",
        "matched_policies": [matched_policy.model_dump(mode="json")],
        "similar_cases": [],
        "signals": [],
        "context_evidence": ModerationContextEvidence().model_dump(mode="json"),
    }


def reject_judgement(matched_policy_id: str, quote: str) -> JudgeAgentResult:
    return JudgeAgentResult(
        action=ModerationAction.REJECT,
        risk_type=RiskType.ADVERTISING,
        risk_score=0.82,
        confidence=0.88,
        accepted_risk_arguments=["The content contains a direct sales solicitation."],
        rejected_arguments=["No non-commercial interpretation is supported."],
        content_evidence=[quote],
        matched_policy_ids=[matched_policy_id],
        reason="The content contains an unsolicited promotion covered by the policy.",
    )


def test_valid_judge_result_maps_to_existing_agent_decision() -> None:
    matched_policy = policy()
    state = state_with_policy(matched_policy)

    mapping = map_judge_to_agent_decision(
        state,
        reject_judgement(str(matched_policy.policy_id), "Limited offer"),
    )

    assert mapping.errors == ()
    assert mapping.decision.recommended_action == ModerationAction.REJECT
    assert mapping.decision.risk_type == RiskType.ADVERTISING
    assert mapping.decision.matched_policies == [matched_policy]
    assert mapping.decision.source_evidence == ["Limited offer"]
    assert mapping.decision.evidence_complete is False


def test_unknown_policy_id_forces_human_review() -> None:
    matched_policy = policy()
    state = state_with_policy(matched_policy)

    mapping = map_judge_to_agent_decision(
        state,
        reject_judgement(str(uuid4()), "Limited offer"),
    )

    assert mapping.errors
    assert mapping.decision.recommended_action == ModerationAction.HUMAN_REVIEW
    assert mapping.decision.matched_policies == []
    assert "Structured evidence validation" in mapping.decision.reason


def test_quote_not_present_in_content_forces_human_review() -> None:
    matched_policy = policy()
    state = state_with_policy(matched_policy)

    mapping = map_judge_to_agent_decision(
        state,
        reject_judgement(str(matched_policy.policy_id), "A fabricated quotation"),
    )

    assert any("content_evidence" in error for error in mapping.errors)
    assert mapping.decision.recommended_action == ModerationAction.HUMAN_REVIEW
    assert mapping.decision.source_evidence == []


def test_policy_for_different_risk_type_forces_human_review() -> None:
    matched_policy = policy(RiskType.ABUSE)
    state = state_with_policy(matched_policy)

    mapping = map_judge_to_agent_decision(
        state,
        reject_judgement(str(matched_policy.policy_id), "Limited offer"),
    )

    assert any("final risk type" in error for error in mapping.errors)
    assert mapping.decision.recommended_action == ModerationAction.HUMAN_REVIEW


def test_normal_pass_maps_without_requiring_a_policy() -> None:
    state: ModerationState = {
        "task_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "content": "Have a pleasant day.",
        "normalized_content": "Have a pleasant day.",
        "content_type": ModerationContentType.TEXT.value,
        "platform": "default",
        "matched_policies": [],
        "similar_cases": [],
        "signals": [],
    }
    judge = JudgeAgentResult(
        action=ModerationAction.PASS,
        risk_type=RiskType.NORMAL,
        risk_score=0.05,
        confidence=0.92,
        accepted_safe_arguments=["No prohibited intent or target is present."],
        content_evidence=["Have a pleasant day"],
        reason="The risk allegation is not supported by the current content.",
    )

    mapping = map_judge_to_agent_decision(state, judge)

    assert mapping.errors == ()
    assert mapping.decision.recommended_action == ModerationAction.PASS
    assert mapping.decision.matched_policies == []


def test_judge_human_review_request_is_preserved() -> None:
    matched_policy = policy()
    state = state_with_policy(matched_policy)
    judge = reject_judgement(str(matched_policy.policy_id), "Limited offer").model_copy(
        update={"need_human_review": True}
    )

    mapping = map_judge_to_agent_decision(state, judge)

    assert mapping.errors == ()
    assert mapping.decision.recommended_action == ModerationAction.HUMAN_REVIEW


def test_judge_mapping_preserves_labeled_parent_context_source() -> None:
    matched_policy = policy()
    state = state_with_policy(matched_policy)
    state["context_evidence"] = ModerationContextEvidence(
        parent_comment=CommunityContentRecord(
            content_id="parent-1",
            content_type=ModerationContentType.COMMENT,
            author_id="author-1",
            content="This is the parent context.",
        )
    ).model_dump(mode="json")
    judge = reject_judgement(str(matched_policy.policy_id), "Limited offer").model_copy(
        update={"context_evidence": ["This is the parent context."]}
    )

    mapping = map_judge_to_agent_decision(state, judge)

    assert mapping.errors == ()
    assert "Parent comment: This is the parent context." in mapping.decision.source_evidence
