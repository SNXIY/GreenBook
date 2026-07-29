from uuid import uuid4

import pytest

from moderation.models import ModerationPolicy, ModerationReviewCase
from moderation.schemas import ModerationAction, PolicySeverity, RiskType
from rag.embedding import HashingTextEmbedder
from rag.qdrant import ModerationQdrantIndex


@pytest.mark.asyncio
async def test_qdrant_indexes_and_retrieves_policies_and_cases() -> None:
    index = ModerationQdrantIndex(
        url=":memory:",
        api_key=None,
        policy_collection="test_policies",
        case_collection="test_cases",
        embedder=HashingTextEmbedder(64),
    )
    await index.start()
    try:
        policy = ModerationPolicy(
            id=uuid4(),
            code="ADV-TEST",
            title="Advertising",
            description="Discount sales and buy-now promotions are prohibited.",
            risk_type=RiskType.ADVERTISING,
            default_action=ModerationAction.REJECT,
            platform="default",
            enabled=True,
            priority=1,
            version=1,
            applicability_conditions=["A promotional purpose is present."],
            exclusion_conditions=[],
            violation_examples=[],
            safe_examples=[],
            severity=PolicySeverity.HIGH,
            suggested_actions=[ModerationAction.REJECT.value],
            tags=["promotion"],
        )
        await index.index_policy(policy)
        policies = await index.search_policies(
            query="buy now discount",
            platform="default",
            risk_types=[RiskType.ADVERTISING],
            limit=3,
        )
        assert [item.code for item in policies] == ["ADV-TEST"]
        assert policies[0].severity == PolicySeverity.HIGH
        assert policies[0].suggested_actions == [ModerationAction.REJECT]

        review_case = ModerationReviewCase(
            id=uuid4(),
            original_task_id=uuid4(),
            content="buy now discount",
            normalized_content="buy now discount",
            content_hash="0" * 64,
            platform="default",
            agent_risk_type=RiskType.ADVERTISING,
            agent_action=ModerationAction.REJECT,
            final_action=ModerationAction.LIMIT,
            reviewer_id="reviewer-1",
            reviewer_reason="Limit reach instead of rejecting.",
            matched_policy_ids=[],
        )
        await index.index_case(review_case)
        cases = await index.search_cases(
            query="discount sale",
            platform="default",
            risk_types=[RiskType.ADVERTISING],
            limit=3,
        )
        assert [item.case_id for item in cases] == [review_case.id]
        assert cases[0].final_action == ModerationAction.LIMIT
    finally:
        await index.close()
