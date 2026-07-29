import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from database import DatabaseManager
from moderation.models import ModerationPolicy, ModerationReviewCase
from moderation.schemas import CaseEvidence, PolicyEvidence, PolicySeverity, RiskType
from moderation.security import redact_text
from rag.embedding import HashingTextEmbedder
from rag.policy.text import policy_search_text

logger = logging.getLogger(__name__)

try:
    from qdrant_client import AsyncQdrantClient, models
except ImportError:  # pragma: no cover - exercised only in minimal client installs
    AsyncQdrantClient = None  # type: ignore[assignment,misc]
    models = None  # type: ignore[assignment]


class ModerationQdrantIndex:
    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        policy_collection: str,
        case_collection: str,
        embedder: HashingTextEmbedder,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.policy_collection = policy_collection
        self.case_collection = case_collection
        self.embedder = embedder
        self._client: Any = None

    async def start(self) -> None:
        if AsyncQdrantClient is None or models is None:
            raise RuntimeError("qdrant-client is required when QDRANT_URL is configured")
        if self.url == ":memory:":
            self._client = AsyncQdrantClient(location=self.url)
        else:
            self._client = AsyncQdrantClient(url=self.url, api_key=self.api_key)
        await self._ensure_collection(self.policy_collection)
        await self._ensure_collection(self.case_collection)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None

    async def _ensure_collection(self, name: str) -> None:
        client = self._require_client()
        if not await client.collection_exists(name):
            await client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=self.embedder.dimensions,
                    distance=models.Distance.COSINE,
                ),
            )

    async def index_policy(self, policy: ModerationPolicy) -> None:
        client = self._require_client()
        text = policy_search_text(policy)
        severity = policy.severity or PolicySeverity.MEDIUM
        suggested_actions = policy.suggested_actions or [policy.default_action.value]
        effective_at = policy.effective_at or policy.created_at
        await client.upsert(
            collection_name=self.policy_collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=str(policy.id),
                    vector=self.embedder.embed(text),
                    payload={
                        "policy_id": str(policy.id),
                        "code": policy.code,
                        "title": policy.title,
                        "description": policy.description,
                        "risk_type": policy.risk_type.value,
                        "default_action": policy.default_action.value,
                        "platform": policy.platform,
                        "enabled": policy.enabled,
                        "version": policy.version,
                        "severity": severity.value,
                        "suggested_actions": [
                            action.value if hasattr(action, "value") else str(action)
                            for action in suggested_actions
                        ],
                        "effective_at": effective_at.isoformat() if effective_at else None,
                        "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
                    },
                )
            ],
        )

    async def index_case(self, review_case: ModerationReviewCase) -> None:
        client = self._require_client()
        await client.upsert(
            collection_name=self.case_collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=str(review_case.id),
                    vector=self.embedder.embed(review_case.normalized_content),
                    payload={
                        "case_id": str(review_case.id),
                        "content": redact_text(review_case.content[:1000]),
                        "risk_type": (
                            review_case.final_risk_type or review_case.agent_risk_type
                        ).value,
                        "final_action": review_case.final_action.value,
                        "reviewer_reason": (
                            redact_text(review_case.reviewer_reason)
                            if review_case.reviewer_reason
                            else None
                        ),
                        "platform": review_case.platform,
                    },
                )
            ],
        )

    async def search_policies(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int,
    ) -> list[PolicyEvidence]:
        client = self._require_client()
        response = await client.query_points(
            collection_name=self.policy_collection,
            query=self.embedder.embed(query),
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="risk_type",
                        match=models.MatchAny(any=[risk.value for risk in risk_types]),
                    ),
                    models.FieldCondition(
                        key="platform",
                        match=models.MatchAny(any=list({"default", platform})),
                    ),
                    models.FieldCondition(key="enabled", match=models.MatchValue(value=True)),
                ]
            ),
            with_payload=True,
            limit=limit,
        )
        evidence = []
        for point in response.points:
            payload = point.payload or {}
            try:
                evidence.append(
                    PolicyEvidence(
                        policy_id=UUID(str(payload["policy_id"])),
                        code=str(payload["code"]),
                        title=str(payload["title"]),
                        excerpt=str(payload["description"]),
                        score=max(0.0, min(1.0, float(point.score))),
                        risk_type=payload.get("risk_type"),
                        default_action=payload.get("default_action"),
                        version=payload.get("version"),
                        severity=payload.get("severity"),
                        suggested_actions=payload.get("suggested_actions") or [],
                        enabled=payload.get("enabled"),
                        effective_at=payload.get("effective_at"),
                        expires_at=payload.get("expires_at"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed Qdrant policy point %s", point.id)
        return evidence

    async def search_cases(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int,
    ) -> list[CaseEvidence]:
        client = self._require_client()
        response = await client.query_points(
            collection_name=self.case_collection,
            query=self.embedder.embed(query),
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="risk_type",
                        match=models.MatchAny(any=[risk.value for risk in risk_types]),
                    ),
                    models.FieldCondition(
                        key="platform",
                        match=models.MatchAny(any=list({"default", platform})),
                    ),
                ]
            ),
            with_payload=True,
            limit=limit,
        )
        evidence = []
        for point in response.points:
            payload = point.payload or {}
            try:
                evidence.append(
                    CaseEvidence(
                        case_id=UUID(str(payload["case_id"])),
                        content_excerpt=str(payload["content"]),
                        risk_type=payload["risk_type"],
                        final_action=payload["final_action"],
                        reviewer_reason=payload.get("reviewer_reason"),
                        score=max(0.0, min(1.0, float(point.score))),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring malformed Qdrant case point %s", point.id)
        return evidence

    async def sync_policies(self, database: DatabaseManager) -> None:
        from moderation.repositories import ModerationPolicyRepository

        repository = ModerationPolicyRepository()
        async with database.session() as session:
            policies = await repository.list(session, enabled_only=True)
        for policy in policies:
            await self.index_policy(policy)

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("Qdrant moderation index has not been started")
        return self._client
