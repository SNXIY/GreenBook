import asyncio
import logging
from collections.abc import Sequence

from moderation.schemas import PolicyEvidence, RiskType
from rag.policy.database import DatabasePolicyRetriever
from rag.qdrant import ModerationQdrantIndex

logger = logging.getLogger(__name__)


class HybridPolicyRetriever:
    def __init__(
        self,
        fallback: DatabasePolicyRetriever,
        vector_index: ModerationQdrantIndex | None = None,
    ) -> None:
        self.fallback = fallback
        self.vector_index = vector_index

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 5,
    ) -> list[PolicyEvidence]:
        vector_results, database_results = await asyncio.gather(
            self._search_vector(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            ),
            self.fallback.search(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            ),
        )
        vector_scores = {item.policy_id: item.score for item in vector_results}
        vector_facts = await self.fallback.load_evidence_by_ids(
            policy_ids=list(vector_scores),
            platform=platform,
            risk_types=risk_types,
            scores=vector_scores,
        )
        merged = {item.policy_id: item for item in database_results}
        for item in vector_facts:
            existing = merged.get(item.policy_id)
            if existing is None or item.score > existing.score:
                merged[item.policy_id] = item
        return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:limit]

    async def _search_vector(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int,
    ) -> list[PolicyEvidence]:
        if self.vector_index is None:
            return []
        try:
            return await self.vector_index.search_policies(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            )
        except Exception:
            logger.exception("Qdrant policy search failed; using database retrieval")
            return []
