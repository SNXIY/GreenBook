from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moderation.models import ModerationPolicy
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    ModerationAction,
    PolicyEvidence,
    PolicyQueryPlan,
    PolicyRetrievalMode,
    PolicySeverity,
    RiskType,
)
from rag.policy.agentic import AgenticPolicyRetriever, PolicyFactsUnavailableError
from rag.policy.database import KeywordPolicyMatch


class FakePolicyDatabase:
    def __init__(self, policies: list[ModerationPolicy]) -> None:
        self.policies = policies
        self.keyword_scores: dict[str, float] = {}
        self.keyword_calls = 0
        self.load_calls = 0
        self.fail_keyword = False
        self.fail_load = False

    async def search_keywords(self, *, query, limit, **kwargs):
        del kwargs
        self.keyword_calls += 1
        if self.fail_keyword:
            raise RuntimeError("keyword database unavailable")
        return [
            KeywordPolicyMatch(policy=policy, score=self.keyword_scores.get(policy.code, 0.8))
            for policy in self.policies[:limit]
        ]

    async def load_active_by_ids(self, *, policy_ids, **kwargs):
        del kwargs
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("facts database unavailable")
        ids = set(policy_ids)
        return [policy for policy in self.policies if policy.id in ids]


class FakeVectorIndex:
    def __init__(self, results: list[PolicyEvidence] | Exception) -> None:
        self.results = results
        self.calls = 0

    async def search_policies(self, **kwargs):
        del kwargs
        self.calls += 1
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


def _policy(
    code: str,
    risk_type: RiskType,
    *,
    version: int = 1,
    platform: str = "default",
    severity: PolicySeverity = PolicySeverity.HIGH,
) -> ModerationPolicy:
    now = datetime.now(UTC)
    return ModerationPolicy(
        id=uuid4(),
        code=code,
        title=f"Current {code}",
        description=f"Current database facts for {code}.",
        risk_type=risk_type,
        default_action=ModerationAction.REJECT,
        platform=platform,
        enabled=True,
        priority=100,
        version=version,
        applicability_conditions=["A required behavior is present."],
        exclusion_conditions=[],
        violation_examples=[],
        safe_examples=[],
        severity=severity,
        suggested_actions=[ModerationAction.REJECT.value],
        tags=["policy"],
        effective_at=now - timedelta(days=1),
        expires_at=None,
        created_at=now - timedelta(days=1),
        updated_at=now,
    )


def _plan(mode: PolicyRetrievalMode, *queries: str) -> PolicyQueryPlan:
    return PolicyQueryPlan(
        risk_hypotheses=[RiskType.ADVERTISING],
        queries=list(queries) or ["advertising off-platform promotion"],
        risk_type_filters=[RiskType.ADVERTISING],
        severity_filters=[PolicySeverity.HIGH],
        retrieval_mode=mode,
        reason="Retrieve advertising policy facts.",
    )


def _vector_evidence(policy, score: float = 0.9) -> PolicyEvidence:
    return PolicyEvidence(
        policy_id=policy.id,
        code="STALE-CODE",
        title="Stale Qdrant title",
        excerpt="Stale Qdrant payload",
        score=score,
        risk_type=RiskType.ADVERTISING,
        default_action=ModerationAction.PASS,
        version=999,
    )


def _config(**updates) -> AgenticPolicyRAGConfig:
    return AgenticPolicyRAGConfig(
        min_combined_score=0,
        min_vector_score=0,
        **updates,
    )


@pytest.mark.asyncio
async def test_vector_mode_rehydrates_qdrant_candidate_from_database() -> None:
    policy = _policy("ADV-001", RiskType.ADVERTISING)
    database = FakePolicyDatabase([policy])
    vector = FakeVectorIndex([_vector_evidence(policy)])
    retriever = AgenticPolicyRetriever(database, vector, _config())

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.VECTOR),
        platform="community",
        retrieval_round=1,
    )

    assert batch.policies[0].title == "Current ADV-001"
    assert batch.policies[0].version == 1
    assert database.load_calls == 1
    assert batch.history.vector_result_count == 1


@pytest.mark.asyncio
async def test_keyword_mode_uses_database_search_without_qdrant() -> None:
    policy = _policy("ADV-001", RiskType.ADVERTISING)
    database = FakePolicyDatabase([policy])
    retriever = AgenticPolicyRetriever(database, None, _config())

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.KEYWORD),
        platform="default",
        retrieval_round=1,
    )

    assert [item.code for item in batch.policies] == ["ADV-001"]
    assert database.keyword_calls == 1
    assert batch.history.keyword_result_count == 1
    assert batch.history.fallback_used is False


@pytest.mark.asyncio
async def test_hybrid_mode_fuses_vector_and_keyword_scores() -> None:
    policy = _policy("ADV-001", RiskType.ADVERTISING)
    database = FakePolicyDatabase([policy])
    database.keyword_scores[policy.code] = 0.4
    vector = FakeVectorIndex([_vector_evidence(policy, 0.8)])
    retriever = AgenticPolicyRetriever(
        database,
        vector,
        _config(vector_weight=0.75, keyword_weight=0.25),
    )

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.HYBRID),
        platform="default",
        retrieval_round=1,
    )

    result = batch.policies[0]
    assert result.vector_score == 0.8
    assert result.keyword_score == 0.4
    assert result.combined_score == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_retriever_deduplicates_policy_ids_and_keeps_current_version() -> None:
    old = _policy("ADV-SAME", RiskType.ADVERTISING, version=1)
    current = _policy("ADV-SAME", RiskType.ADVERTISING, version=2)
    database = FakePolicyDatabase([old, current])
    vector = FakeVectorIndex([_vector_evidence(old), _vector_evidence(current)])
    retriever = AgenticPolicyRetriever(database, vector, _config())

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.VECTOR, "advertising", "advertising"),
        platform="default",
        retrieval_round=1,
    )

    assert len(batch.policies) == 1
    assert batch.policies[0].policy_id == current.id
    assert vector.calls == 1


@pytest.mark.asyncio
async def test_qdrant_failure_falls_back_to_keyword_search() -> None:
    policy = _policy("ADV-001", RiskType.ADVERTISING)
    database = FakePolicyDatabase([policy])
    vector = FakeVectorIndex(RuntimeError("qdrant unavailable"))
    retriever = AgenticPolicyRetriever(database, vector, _config())

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.VECTOR),
        platform="default",
        retrieval_round=1,
    )

    assert [item.code for item in batch.policies] == ["ADV-001"]
    assert batch.history.fallback_used is True
    assert "QDRANT_UNAVAILABLE" in batch.errors


@pytest.mark.asyncio
async def test_qdrant_only_stale_id_is_not_treated_as_policy_fact() -> None:
    policy = _policy("ADV-STALE", RiskType.ADVERTISING)
    database = FakePolicyDatabase([])
    vector = FakeVectorIndex([_vector_evidence(policy)])
    retriever = AgenticPolicyRetriever(database, vector, _config())

    batch = await retriever.retrieve(
        plan=_plan(PolicyRetrievalMode.VECTOR),
        platform="default",
        retrieval_round=1,
    )

    assert batch.policies == ()


@pytest.mark.asyncio
async def test_postgresql_fact_failure_is_a_safe_error() -> None:
    policy = _policy("ADV-001", RiskType.ADVERTISING)
    database = FakePolicyDatabase([policy])
    database.fail_load = True
    retriever = AgenticPolicyRetriever(
        database,
        FakeVectorIndex([_vector_evidence(policy)]),
        _config(),
    )

    with pytest.raises(PolicyFactsUnavailableError):
        await retriever.retrieve(
            plan=_plan(PolicyRetrievalMode.VECTOR),
            platform="default",
            retrieval_round=1,
        )
