from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from database import DatabaseManager
from moderation.models import ModerationPolicy
from moderation.schemas import ModerationAction, PolicySeverity, RiskType
from rag.embedding import HashingTextEmbedder
from rag.policy.database import DatabasePolicyRetriever
from rag.policy.hybrid import HybridPolicyRetriever


class StaleVectorIndex:
    def __init__(self, policy: ModerationPolicy) -> None:
        self.policy = policy

    async def search_policies(self, **kwargs):
        del kwargs
        from moderation.schemas import PolicyEvidence

        return [
            PolicyEvidence(
                policy_id=self.policy.id,
                code="STALE-CODE",
                title="Stale vector title",
                excerpt="Stale vector payload",
                score=0.99,
                risk_type=RiskType.PRIVACY,
                default_action=ModerationAction.PASS,
                version=999,
            )
        ]


@pytest_asyncio.fixture
async def policy_database(tmp_path) -> AsyncGenerator[DatabaseManager, None]:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'policy-rag.db'}")
    yield database
    await database.close()


def _policy(
    code: str,
    *,
    enabled: bool = True,
    effective_at: datetime,
    expires_at: datetime | None = None,
) -> ModerationPolicy:
    return ModerationPolicy(
        code=code,
        title=code,
        description="Phone number and private contact information policy.",
        risk_type=RiskType.PRIVACY,
        default_action=ModerationAction.REJECT,
        platform="default",
        enabled=enabled,
        priority=100,
        version=1,
        applicability_conditions=["A private phone number is disclosed."],
        exclusion_conditions=["The number is synthetic."],
        violation_examples=[],
        safe_examples=[],
        severity=PolicySeverity.CRITICAL,
        suggested_actions=[ModerationAction.REJECT.value],
        tags=["phone", "privacy"],
        effective_at=effective_at,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_database_keyword_search_filters_disabled_expired_and_future_policies(
    policy_database: DatabaseManager,
) -> None:
    now = datetime.now(UTC)
    active = _policy("PRIVACY-ACTIVE", effective_at=now - timedelta(days=1))
    disabled = _policy(
        "PRIVACY-DISABLED",
        enabled=False,
        effective_at=now - timedelta(days=1),
    )
    expired = _policy(
        "PRIVACY-EXPIRED",
        effective_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    future = _policy("PRIVACY-FUTURE", effective_at=now + timedelta(days=1))
    async with policy_database.session() as session:
        session.add_all([active, disabled, expired, future])
        await session.commit()

    retriever = DatabasePolicyRetriever(policy_database, HashingTextEmbedder(64))
    results = await retriever.search_keywords(
        query="privacy phone number",
        platform="default",
        risk_types=[RiskType.PRIVACY],
        severities=[PolicySeverity.CRITICAL],
        limit=10,
        as_of=now,
    )

    assert [item.policy.code for item in results] == ["PRIVACY-ACTIVE"]


@pytest.mark.asyncio
async def test_existing_hybrid_retriever_rehydrates_vector_payload(
    policy_database: DatabaseManager,
) -> None:
    now = datetime.now(UTC)
    policy = _policy("PRIVACY-CURRENT", effective_at=now - timedelta(days=1))
    async with policy_database.session() as session:
        session.add(policy)
        await session.commit()

    database_retriever = DatabasePolicyRetriever(
        policy_database,
        HashingTextEmbedder(64),
    )
    retriever = HybridPolicyRetriever(database_retriever, StaleVectorIndex(policy))
    results = await retriever.search(
        query="privacy phone number",
        platform="default",
        risk_types=[RiskType.PRIVACY],
        limit=5,
    )

    assert results[0].code == "PRIVACY-CURRENT"
    assert results[0].title == "PRIVACY-CURRENT"
    assert results[0].version == 1
    assert results[0].default_action == ModerationAction.REJECT
