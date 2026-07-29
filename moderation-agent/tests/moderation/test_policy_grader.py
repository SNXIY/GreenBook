from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig

from agents.moderation.nodes.policy_grader import (
    LLMPolicyGrader,
    constrain_policy_grade_result,
    validate_policy_facts,
)
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    ModerationAction,
    PolicyApplicability,
    PolicyGradeNextAction,
    PolicyGradeResult,
    PolicyItemGrade,
    PolicyQueryPlan,
    PolicyRetrievalMode,
    PolicySeverity,
    RetrievedPolicy,
    RiskClassification,
    RiskType,
)
from schema.models import OpenAICompatibleName


class ScriptedGraderModel:
    def __init__(self, responses: list[dict | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.structured_output_calls: list[dict] = []

    def with_structured_output(self, schema, **kwargs):
        self.structured_output_calls.append({"schema": schema, **kwargs})
        return self

    async def ainvoke(self, messages, config):
        self.calls.append({"messages": messages, "config": config})
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def _policy(
    *,
    code: str = "ADV-001",
    version: int = 1,
    risk_type: RiskType = RiskType.ADVERTISING,
    enabled: bool = True,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    suggested_actions: list[ModerationAction] | None = None,
) -> RetrievedPolicy:
    now = datetime.now(UTC)
    return RetrievedPolicy(
        policy_id=uuid4(),
        code=code,
        title="Off-platform promotion",
        risk_type=risk_type,
        version=version,
        severity=PolicySeverity.HIGH,
        description="Commercial solicitation using off-platform contact is prohibited.",
        applicability_conditions=["Commercial or promotional intent is present."],
        exclusion_conditions=["The contact reference is non-commercial."],
        suggested_actions=(
            [ModerationAction.REJECT] if suggested_actions is None else suggested_actions
        ),
        enabled=enabled,
        effective_at=effective_at or now - timedelta(days=1),
        expires_at=expires_at,
        vector_score=0.8,
        keyword_score=0.7,
        combined_score=0.82,
        retrieval_query="off-platform commercial promotion",
        retrieval_round=1,
    )


def _plan() -> PolicyQueryPlan:
    return PolicyQueryPlan(
        risk_hypotheses=[RiskType.ADVERTISING],
        queries=["off-platform commercial promotion"],
        required_conditions=["Commercial intent is present."],
        exclusion_conditions_to_check=["The contact is non-commercial."],
        risk_type_filters=[RiskType.ADVERTISING],
        severity_filters=[PolicySeverity.HIGH],
        retrieval_mode=PolicyRetrievalMode.HYBRID,
        reason="Advertising Policy evidence is required.",
    )


def _item(
    policy: RetrievedPolicy,
    applicability: PolicyApplicability,
    *,
    relevant: bool = True,
    confidence: float = 0.9,
    supports_actions: list[ModerationAction] | None = None,
    exclusions: list[str] | None = None,
) -> PolicyItemGrade:
    return PolicyItemGrade(
        policy_id=policy.policy_id,
        relevant=relevant,
        applicability=applicability,
        matched_conditions=["Commercial intent is present."],
        exclusion_conditions_triggered=exclusions or [],
        supports_actions=supports_actions or [ModerationAction.REJECT],
        confidence=confidence,
        reason="The supplied evidence was compared with the Policy conditions.",
    )


def _grade(
    *items: PolicyItemGrade,
    sufficient: bool,
    next_action: PolicyGradeNextAction,
) -> PolicyGradeResult:
    return PolicyGradeResult(
        relevant=any(item.relevant for item in items),
        sufficient=sufficient,
        item_grades=list(items),
        applicable_policy_ids=[
            item.policy_id for item in items if item.applicability == PolicyApplicability.APPLICABLE
        ],
        partial_policy_ids=[
            item.policy_id
            for item in items
            if item.applicability == PolicyApplicability.PARTIALLY_APPLICABLE
        ],
        rejected_policy_ids=[
            item.policy_id
            for item in items
            if item.applicability
            in {
                PolicyApplicability.NOT_APPLICABLE,
                PolicyApplicability.INSUFFICIENT_EVIDENCE,
            }
        ],
        suggested_next_action=next_action,
        reason="The retrieved Policies were graded against current evidence.",
    )


def _constrain(raw: PolicyGradeResult, *policies: RetrievedPolicy):
    return constrain_policy_grade_result(
        raw,
        plan=_plan(),
        policies=tuple(policies),
        min_confidence=0.65,
    )


def _config() -> RunnableConfig:
    return RunnableConfig(
        configurable={
            "model": OpenAICompatibleName.OPENAI_COMPATIBLE,
            "moderation_task_id": "task-123",
        }
    )


async def _run_llm(grader: LLMPolicyGrader, policy: RetrievedPolicy):
    return await grader.grade(
        content="Add me for free Java material: 13812345678",
        classification=RiskClassification(
            risk_type=RiskType.ADVERTISING,
            risk_score=0.7,
            confidence=0.75,
            indicators=["contact"],
        ),
        signals=[],
        evidence_summary={"contact": "138****5678"},
        plan=_plan(),
        policies=[policy],
        config=_config(),
    )


def test_deterministic_validation_rejects_invalid_facts_and_old_versions() -> None:
    now = datetime.now(UTC)
    old = _policy(code="ADV-VERSIONED", version=1)
    current = _policy(code="ADV-VERSIONED", version=2)
    disabled = _policy(code="ADV-DISABLED", enabled=False)
    future = _policy(code="ADV-FUTURE", effective_at=now + timedelta(days=1))
    expired = _policy(
        code="ADV-EXPIRED",
        effective_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    wrong_risk = _policy(code="PRIVACY-001", risk_type=RiskType.PRIVACY)
    no_action = _policy(code="ADV-NO-ACTION", suggested_actions=[])
    unverified = _policy(code="ADV-UNVERIFIED").model_copy(update={"fact_source": "QDRANT"})

    result = validate_policy_facts(
        [old, current, disabled, future, expired, wrong_risk, no_action, unverified],
        _plan(),
        as_of=now,
    )

    assert [item.policy_id for item in result.valid_policies] == [current.policy_id]
    reasons = " ".join(item.reason for item in result.rejected_policies)
    assert "newer active version" in reasons
    assert "disabled" in reasons
    assert "not effective" in reasons
    assert "expired" in reasons
    assert "risk type" in reasons
    assert "support any moderation action" in reasons
    assert "PostgreSQL" in reasons


def test_applicable_policy_is_accepted_when_conditions_and_action_match() -> None:
    policy = _policy()
    raw = _grade(
        _item(policy, PolicyApplicability.APPLICABLE),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )

    result, rejected, errors = _constrain(raw, policy)

    assert result.sufficient is True
    assert result.applicable_policy_ids == [policy.policy_id]
    assert rejected == ()
    assert errors == ()


def test_semantically_similar_policy_without_conditions_is_rejected() -> None:
    policy = _policy()
    raw = _grade(
        _item(policy, PolicyApplicability.INSUFFICIENT_EVIDENCE),
        sufficient=False,
        next_action=PolicyGradeNextAction.REWRITE_QUERY,
    )

    result, rejected, _ = _constrain(raw, policy)

    assert result.sufficient is False
    assert result.rejected_policy_ids == [policy.policy_id]
    assert rejected[0].stage == "SEMANTIC"


def test_triggered_exclusion_overrides_applicable_claim() -> None:
    policy = _policy()
    raw = _grade(
        _item(
            policy,
            PolicyApplicability.APPLICABLE,
            exclusions=["The contact reference is non-commercial."],
        ),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )

    result, _, _ = _constrain(raw, policy)

    assert result.sufficient is False
    assert result.item_grades[0].applicability == PolicyApplicability.NOT_APPLICABLE


def test_partially_applicable_policy_remains_partial() -> None:
    policy = _policy()
    raw = _grade(
        _item(policy, PolicyApplicability.PARTIALLY_APPLICABLE),
        sufficient=False,
        next_action=PolicyGradeNextAction.REWRITE_QUERY,
    )

    result, rejected, _ = _constrain(raw, policy)

    assert result.partial_policy_ids == [policy.policy_id]
    assert result.rejected_policy_ids == []
    assert rejected == ()


def test_unknown_policy_id_is_removed_from_semantic_result() -> None:
    policy = _policy()
    unknown = _policy(code="ADV-INVENTED")
    raw = _grade(
        _item(policy, PolicyApplicability.APPLICABLE),
        _item(unknown, PolicyApplicability.APPLICABLE),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )

    result, _, errors = _constrain(raw, policy)

    assert result.applicable_policy_ids == [policy.policy_id]
    assert unknown.policy_id not in result.applicable_policy_ids
    assert errors == ("policy_grader:UnknownPolicyId:1",)


def test_unsupported_action_and_low_confidence_cannot_be_applicable() -> None:
    policy = _policy()
    unsupported = _grade(
        _item(
            policy,
            PolicyApplicability.APPLICABLE,
            supports_actions=[ModerationAction.LIMIT],
        ),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )
    low_confidence = _grade(
        _item(policy, PolicyApplicability.APPLICABLE, confidence=0.4),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )

    unsupported_result, _, _ = _constrain(unsupported, policy)
    confidence_result, _, _ = _constrain(low_confidence, policy)

    assert unsupported_result.item_grades[0].applicability == (
        PolicyApplicability.INSUFFICIENT_EVIDENCE
    )
    assert confidence_result.item_grades[0].applicability == (
        PolicyApplicability.INSUFFICIENT_EVIDENCE
    )
    assert unsupported_result.suggested_next_action == PolicyGradeNextAction.REWRITE_QUERY


@pytest.mark.asyncio
async def test_grader_repairs_one_structured_parse_failure(monkeypatch) -> None:
    policy = _policy()
    valid_grade = _grade(
        _item(policy, PolicyApplicability.APPLICABLE),
        sufficient=True,
        next_action=PolicyGradeNextAction.ACCEPT,
    )
    fake = ScriptedGraderModel(
        [
            {"parsed": None, "raw": SimpleNamespace(), "parsing_error": ValueError("bad")},
            {"parsed": valid_grade, "raw": SimpleNamespace(), "parsing_error": None},
        ]
    )
    monkeypatch.setattr("agents.moderation.nodes.policy_grader.get_model", lambda _: fake)
    grader = LLMPolicyGrader(AgenticPolicyRAGConfig(agent_timeout_seconds=1))

    call = await _run_llm(grader, policy)

    assert call.result.sufficient is True
    assert len(fake.calls) == 2
    assert "corrected structured result" in fake.calls[1]["messages"][-1].content
    assert fake.calls[0]["config"]["run_name"] == "policy_grader"
    assert "13812345678" not in str(fake.calls[0]["config"]["metadata"])


@pytest.mark.asyncio
async def test_grader_parse_failure_after_repair_is_explicit(monkeypatch) -> None:
    policy = _policy()
    invalid = {"parsed": None, "raw": SimpleNamespace(), "parsing_error": ValueError("bad")}
    fake = ScriptedGraderModel([invalid, invalid])
    monkeypatch.setattr("agents.moderation.nodes.policy_grader.get_model", lambda _: fake)
    grader = LLMPolicyGrader(AgenticPolicyRAGConfig(agent_timeout_seconds=1))

    with pytest.raises(ValueError, match="could not be repaired"):
        await _run_llm(grader, policy)

    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_grader_model_failure_is_explicit(monkeypatch) -> None:
    policy = _policy()
    fake = ScriptedGraderModel([RuntimeError("provider unavailable")])
    monkeypatch.setattr("agents.moderation.nodes.policy_grader.get_model", lambda _: fake)
    grader = LLMPolicyGrader(AgenticPolicyRAGConfig(agent_timeout_seconds=1))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await _run_llm(grader, policy)
