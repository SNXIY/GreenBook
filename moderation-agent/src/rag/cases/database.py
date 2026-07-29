from collections.abc import Sequence

from database import DatabaseManager
from moderation.repositories import ModerationReviewCaseRepository
from moderation.schemas import CaseEvidence, RiskType
from rag.embedding import HashingTextEmbedder, cosine_similarity


class DatabaseCaseRetriever:
    def __init__(self, database: DatabaseManager, embedder: HashingTextEmbedder) -> None:
        self.database = database
        self.embedder = embedder
        self.repository = ModerationReviewCaseRepository()

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 3,
    ) -> list[CaseEvidence]:
        async with self.database.session() as session:
            cases = await self.repository.list_candidates(
                session,
                platform=platform,
                risk_types=risk_types,
            )
        query_vector = self.embedder.embed(query)
        evidence = [
            CaseEvidence(
                case_id=review_case.id,
                content_excerpt=review_case.content[:500],
                risk_type=review_case.final_risk_type or review_case.agent_risk_type,
                final_action=review_case.final_action,
                reviewer_reason=review_case.reviewer_reason,
                score=max(
                    0.0,
                    min(
                        1.0,
                        cosine_similarity(
                            query_vector,
                            self.embedder.embed(review_case.normalized_content),
                        ),
                    ),
                ),
            )
            for review_case in cases
        ]
        evidence.sort(key=lambda item: item.score, reverse=True)
        return evidence[:limit]
