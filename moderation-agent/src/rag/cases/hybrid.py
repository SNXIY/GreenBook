import asyncio
import logging
from collections.abc import Sequence

from moderation.schemas import CaseEvidence, RiskType
from rag.cases.database import DatabaseCaseRetriever
from rag.qdrant import ModerationQdrantIndex

logger = logging.getLogger(__name__)


class HybridCaseRetriever:
    def __init__(
        self,
        fallback: DatabaseCaseRetriever,
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
        limit: int = 3,
    ) -> list[CaseEvidence]:
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
        merged = {item.case_id: item for item in database_results}
        for item in vector_results:
            existing = merged.get(item.case_id)
            if existing is None or item.score > existing.score:
                merged[item.case_id] = item
        return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:limit]

    async def _search_vector(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int,
    ) -> list[CaseEvidence]:
        if self.vector_index is None:
            return []
        try:
            return await self.vector_index.search_cases(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            )
        except Exception:
            logger.exception("Qdrant case search failed; using database retrieval")
            return []
