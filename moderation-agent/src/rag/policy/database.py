from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from database import DatabaseManager
from moderation.models import ModerationPolicy
from moderation.schemas import PolicyEvidence, PolicySeverity, RiskType
from rag.embedding import HashingTextEmbedder, cosine_similarity
from rag.policy.text import keyword_relevance, policy_search_text


@dataclass(frozen=True)
class KeywordPolicyMatch:
    policy: ModerationPolicy
    score: float


class DatabasePolicyRetriever:
    def __init__(self, database: DatabaseManager, embedder: HashingTextEmbedder) -> None:
        from moderation.repositories import ModerationPolicyRepository

        self.database = database
        self.embedder = embedder
        self.repository = ModerationPolicyRepository()

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 5,
    ) -> list[PolicyEvidence]:
        async with self.database.session() as session:
            policies = await self.repository.list_active(
                session,
                platform=platform,
                risk_types=risk_types,
            )
        query_vector = self.embedder.embed(query)
        evidence = []
        for policy in policies:
            policy_text = policy_search_text(policy)
            similarity = cosine_similarity(query_vector, self.embedder.embed(policy_text))
            evidence.append(
                policy_to_evidence(
                    policy,
                    score=max(0.0, min(1.0, 0.75 + similarity * 0.25)),
                )
            )
        evidence.sort(key=lambda item: item.score, reverse=True)
        return evidence[:limit]

    async def search_keywords(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity] = (),
        limit: int = 5,
        as_of: datetime | None = None,
    ) -> list[KeywordPolicyMatch]:
        async with self.database.session() as session:
            policies = await self.repository.list_active(
                session,
                platform=platform,
                risk_types=risk_types,
                severities=severities,
                as_of=as_of,
            )
        matches = [
            KeywordPolicyMatch(
                policy=policy,
                score=keyword_relevance(query, policy_search_text(policy)),
            )
            for policy in policies
        ]
        matches = [match for match in matches if match.score > 0]
        matches.sort(key=lambda item: (item.score, -item.policy.priority), reverse=True)
        return matches[:limit]

    async def load_active_by_ids(
        self,
        *,
        policy_ids: Sequence[UUID],
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity] = (),
        as_of: datetime | None = None,
    ) -> list[ModerationPolicy]:
        async with self.database.session() as session:
            return await self.repository.get_active_by_ids(
                session,
                policy_ids=policy_ids,
                platform=platform,
                risk_types=risk_types,
                severities=severities,
                as_of=as_of,
            )

    async def load_evidence_by_ids(
        self,
        *,
        policy_ids: Sequence[UUID],
        platform: str,
        risk_types: Sequence[RiskType],
        scores: dict[UUID, float],
    ) -> list[PolicyEvidence]:
        policies = await self.load_active_by_ids(
            policy_ids=policy_ids,
            platform=platform,
            risk_types=risk_types,
        )
        return [policy_to_evidence(policy, score=scores.get(policy.id, 0.0)) for policy in policies]


def policy_to_evidence(policy: ModerationPolicy, *, score: float) -> PolicyEvidence:
    return PolicyEvidence(
        policy_id=policy.id,
        code=policy.code,
        title=policy.title,
        excerpt=policy.description,
        score=max(0.0, min(1.0, score)),
        risk_type=policy.risk_type,
        default_action=policy.default_action,
        version=policy.version,
        severity=policy.severity,
        suggested_actions=policy.suggested_actions or [policy.default_action],
        applicability_conditions=policy.applicability_conditions or [],
        exclusion_conditions=policy.exclusion_conditions or [],
        violation_examples=policy.violation_examples or [],
        safe_examples=policy.safe_examples or [],
        enabled=policy.enabled,
        effective_at=policy.effective_at,
        expires_at=policy.expires_at,
    )
