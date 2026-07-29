from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from moderation.schemas import (
    AgenticPolicyRAGConfig,
    ModerationAction,
    ModerationPolicyCreate,
    PolicyApplicability,
    PolicyEvidence,
    PolicyEvidenceSummary,
    PolicyGradeNextAction,
    PolicyGradeResult,
    PolicyItemGrade,
    PolicyQueryHistoryEntry,
    PolicyQueryPlan,
    PolicyRetrievalMode,
    PolicySeverity,
    RetrievedPolicy,
    RiskType,
    agentic_policy_rag_audit_from_state,
)


def test_policy_create_preserves_facts_and_default_action() -> None:
    policy = ModerationPolicyCreate(
        code="ADV-TOOL-001",
        title="Off-platform promotion",
        description="Promotional off-platform contact is prohibited.",
        risk_type=RiskType.ADVERTISING,
        default_action=ModerationAction.REJECT,
        applicability_conditions=["A promotional purpose is present."],
        exclusion_conditions=["The reference is non-commercial."],
        severity=PolicySeverity.HIGH,
        suggested_actions=[ModerationAction.LIMIT],
        tags=["off-platform"],
    )

    assert policy.suggested_actions == [ModerationAction.REJECT, ModerationAction.LIMIT]
    assert policy.applicability_conditions == ["A promotional purpose is present."]


def test_policy_expiration_must_follow_effective_time() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="expires_at must be later"):
        ModerationPolicyCreate(
            code="PRIVACY-OLD",
            title="Expired rule",
            description="A test rule.",
            risk_type=RiskType.PRIVACY,
            default_action=ModerationAction.REJECT,
            effective_at=now,
            expires_at=now - timedelta(seconds=1),
        )


def test_policy_query_plan_enforces_query_budget() -> None:
    with pytest.raises(ValidationError):
        PolicyQueryPlan(
            risk_hypotheses=[RiskType.ADVERTISING],
            queries=["one", "two", "three", "four"],
            risk_type_filters=[RiskType.ADVERTISING],
            retrieval_mode=PolicyRetrievalMode.HYBRID,
            reason="Test query budget.",
        )


def test_policy_rag_config_rejects_invalid_budget() -> None:
    with pytest.raises(ValidationError, match="retrieval weight"):
        AgenticPolicyRAGConfig(vector_weight=0, keyword_weight=0)
    with pytest.raises(ValidationError, match="final_top_k"):
        AgenticPolicyRAGConfig(final_top_k=8, max_total_retrieved_policies=4)


def test_retrieved_policy_and_grade_are_structured() -> None:
    policy_id = uuid4()
    effective_at = datetime.now(UTC)
    policy = RetrievedPolicy(
        policy_id=policy_id,
        code="ADV-001",
        title="Off-platform promotion",
        risk_type=RiskType.ADVERTISING,
        version=2,
        severity=PolicySeverity.HIGH,
        description="Promotional off-platform contact is prohibited.",
        applicability_conditions=["Commercial purpose is present."],
        suggested_actions=[ModerationAction.REJECT],
        effective_at=effective_at,
        vector_score=0.8,
        keyword_score=0.6,
        combined_score=0.75,
        retrieval_query="free material used to induce adding a contact",
        retrieval_round=1,
    )
    item = PolicyItemGrade(
        policy_id=policy_id,
        relevant=True,
        applicability=PolicyApplicability.PARTIALLY_APPLICABLE,
        matched_conditions=["An off-platform contact is present."],
        missing_conditions=["Commercial purpose is not yet established."],
        supports_actions=[ModerationAction.REJECT],
        confidence=0.74,
        reason="The rule is relevant, but one required condition is missing.",
    )
    grade = PolicyGradeResult(
        relevant=True,
        sufficient=False,
        item_grades=[item],
        partial_policy_ids=[policy_id],
        missing_policy_topics=["Non-commercial contact exception."],
        missing_evidence=["Commercial purpose."],
        suggested_next_action=PolicyGradeNextAction.REWRITE_QUERY,
        reason="A narrower policy query is required.",
    )

    assert policy.version == 2
    assert grade.item_grades[0].applicability == PolicyApplicability.PARTIALLY_APPLICABLE


def test_policy_rag_audit_is_built_from_graph_state() -> None:
    policy_id = uuid4()
    plan = PolicyQueryPlan(
        risk_hypotheses=[RiskType.PRIVACY],
        queries=["unauthorized publication of another person's phone number"],
        required_conditions=["The phone number belongs to another person."],
        risk_type_filters=[RiskType.PRIVACY],
        severity_filters=[PolicySeverity.HIGH, PolicySeverity.CRITICAL],
        retrieval_mode=PolicyRetrievalMode.HYBRID,
        reason="The content contains a possible third-party phone number.",
    )
    summary = PolicyEvidenceSummary(
        complete=True,
        sufficient=True,
        applicable_policies=[
            PolicyEvidence(
                policy_id=policy_id,
                code="PRIVACY-001",
                title="Personal information",
                excerpt="Do not expose private phone numbers.",
                score=0.91,
                risk_type=RiskType.PRIVACY,
                severity=PolicySeverity.CRITICAL,
                suggested_actions=[ModerationAction.REJECT],
            )
        ],
        retrieval_rounds=1,
        queries_used=plan.queries,
        reason="A current and applicable privacy policy was found.",
    )
    history = PolicyQueryHistoryEntry(
        retrieval_round=1,
        queries=plan.queries,
        risk_type_filters=plan.risk_type_filters,
        severity_filters=plan.severity_filters,
        retrieval_mode=plan.retrieval_mode,
        vector_result_count=1,
        keyword_result_count=1,
        retrieved_policy_ids=[policy_id],
        new_policy_ids=[policy_id],
    )

    audit = agentic_policy_rag_audit_from_state(
        {
            "policy_query_plan": plan.model_dump(mode="json"),
            "policy_query_history": [history.model_dump(mode="json")],
            "policy_evidence_summary": summary.model_dump(mode="json"),
            "policy_rewrite_count": 0,
            "policy_rag_fallback_used": False,
        },
        entered_human_review=False,
    )

    assert audit is not None
    assert audit.evidence_summary is not None
    assert audit.evidence_summary.applicable_policies[0].policy_id == policy_id
    assert audit.query_history[0].retrieval_mode == PolicyRetrievalMode.HYBRID


def test_empty_state_has_no_policy_rag_audit() -> None:
    assert agentic_policy_rag_audit_from_state({}, entered_human_review=False) is None
