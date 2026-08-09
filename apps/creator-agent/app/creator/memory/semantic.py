from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.creator.memory.errors import CreatorMemoryIntegrityError
from app.creator.memory.models import (
    CreatorEngagementMetrics,
    CreatorHistoricalPost,
    CreatorSemanticMemoryHit,
)
from app.creator.memory.ports import CreatorTextEmbedder


logger = logging.getLogger(__name__)

_POINT_NAMESPACE = uuid.UUID("b178f2f4-74cc-4e17-9b65-c15ce8a45177")


class QdrantCreatorSemanticMemoryStore:
    backend_name = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        collection_name: str,
        embedder: CreatorTextEmbedder,
        chunk_chars: int = 1_200,
        chunk_overlap_chars: int = 160,
        score_threshold: float = 0.0,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        if not url and client is None:
            raise ValueError("Qdrant URL is required")
        if chunk_chars < 200:
            raise ValueError("Semantic memory chunk size must be at least 200")
        if chunk_overlap_chars < 0 or chunk_overlap_chars >= chunk_chars:
            raise ValueError("Semantic memory chunk overlap is invalid")
        if not 0.0 <= score_threshold <= 1.0:
            raise ValueError("Semantic memory score threshold must be between 0 and 1")
        self._url = url
        self._api_key = api_key or None
        self._collection = collection_name
        self._embedder = embedder
        self._chunk_chars = chunk_chars
        self._chunk_overlap = chunk_overlap_chars
        self._score_threshold = score_threshold
        self._client = client
        self._owns_client = client is None

    async def start(self) -> None:
        if self._client is None:
            self._client = (
                AsyncQdrantClient(location=":memory:")
                if self._url == ":memory:"
                else AsyncQdrantClient(url=self._url, api_key=self._api_key)
            )
        client = self._require_client()
        if not await client.collection_exists(self._collection):
            await client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._embedder.dimensions,
                    distance=models.Distance.COSINE,
                ),
                on_disk_payload=True,
            )
        else:
            await self._validate_collection()
        await self._ensure_payload_indexes()

    async def upsert_post(self, post: CreatorHistoricalPost) -> int:
        client = self._require_client()
        if post.status.lower() == "deleted":
            await self.delete_post(
                tenant_id=post.tenant_id,
                creator_id=post.creator_id,
                post_id=post.post_id,
            )
            return 0

        chunks = _chunk_post(
            post,
            chunk_chars=self._chunk_chars,
            overlap_chars=self._chunk_overlap,
        )
        embeddings = await self._embedder.embed(
            tuple(f"{post.title}\n\n{chunk}" for chunk in chunks)
        )
        if len(embeddings) != len(chunks):
            raise CreatorMemoryIntegrityError(
                "Semantic memory embedder returned the wrong vector count",
                details={
                    "post_id": post.post_id,
                    "expected": len(chunks),
                    "actual": len(embeddings),
                },
            )

        existing_ids = await self._post_point_ids(
            tenant_id=post.tenant_id,
            creator_id=post.creator_id,
            post_id=post.post_id,
        )
        content_hash = _post_hash(post)
        points: list[models.PointStruct] = []
        new_ids: set[str] = set()
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point_id = _point_id(post, index)
            new_ids.add(point_id)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=list(embedding),
                    payload={
                        "tenant_id": post.tenant_id,
                        "creator_id": post.creator_id,
                        "post_id": post.post_id,
                        "chunk_id": f"{post.post_id}:{index}",
                        "chunk_index": index,
                        "title": post.title,
                        "excerpt": chunk,
                        "tags": list(post.tags),
                        "content_type": post.content_type,
                        "visibility": post.visibility,
                        "status": post.status,
                        "published_at": (
                            post.published_at.isoformat()
                            if post.published_at is not None
                            else None
                        ),
                        "metrics": post.metrics.model_dump(mode="json"),
                        "source_system": post.source_system,
                        "source_revision": post.source_revision,
                        "content_hash": content_hash,
                    },
                )
            )
        await client.upsert(
            collection_name=self._collection,
            points=points,
            wait=True,
        )
        stale_ids = sorted(existing_ids - new_ids)
        if stale_ids:
            stale_point_ids: list[int | str | uuid.UUID] = [
                point_id for point_id in stale_ids
            ]
            await client.delete(
                collection_name=self._collection,
                points_selector=models.PointIdsList(points=stale_point_ids),
                wait=True,
            )
        return len(points)

    async def delete_post(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        post_id: str,
    ) -> None:
        await self._require_client().delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=_scope_filter(
                    tenant_id=tenant_id,
                    creator_id=creator_id,
                    post_id=post_id,
                )
            ),
            wait=True,
        )

    async def search(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        query: str,
        limit: int,
        tags: tuple[str, ...] = (),
    ) -> tuple[CreatorSemanticMemoryHit, ...]:
        if limit <= 0:
            return ()
        vector = (await self._embedder.embed((query,)))[0]
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="tenant_id",
                match=models.MatchValue(value=tenant_id),
            ),
            models.FieldCondition(
                key="creator_id",
                match=models.MatchValue(value=creator_id),
            ),
        ]
        if tags:
            conditions.append(
                models.FieldCondition(
                    key="tags",
                    match=models.MatchAny(any=list(tags)),
                )
            )
        response = await self._require_client().query_points(
            collection_name=self._collection,
            query=list(vector),
            query_filter=models.Filter(must=conditions),
            with_payload=True,
            limit=limit,
            score_threshold=self._score_threshold or None,
        )
        hits: list[CreatorSemanticMemoryHit] = []
        for point in response.points:
            try:
                hits.append(_hit_from_point(point))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Ignoring malformed creator semantic memory point id=%s error=%s",
                    point.id,
                    type(exc).__name__,
                )
        return tuple(hits)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None

    async def _validate_collection(self) -> None:
        info = await self._require_client().get_collection(self._collection)
        vectors = info.config.params.vectors
        actual_size = getattr(vectors, "size", None)
        if actual_size is not None and int(actual_size) != self._embedder.dimensions:
            raise CreatorMemoryIntegrityError(
                "Qdrant creator memory vector dimensions do not match",
                details={
                    "collection": self._collection,
                    "expected": self._embedder.dimensions,
                    "actual": int(actual_size),
                },
            )

    async def _ensure_payload_indexes(self) -> None:
        client = self._require_client()
        info = await client.get_collection(self._collection)
        existing = set(info.payload_schema)
        if "tenant_id" not in existing:
            await client.create_payload_index(
                collection_name=self._collection,
                field_name="tenant_id",
                field_schema=models.KeywordIndexParams(
                    type=models.KeywordIndexType.KEYWORD,
                    is_tenant=True,
                ),
                wait=True,
            )
        for field_name in ("creator_id", "post_id", "tags"):
            if field_name not in existing:
                await client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )

    async def _post_point_ids(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        post_id: str,
    ) -> set[str]:
        records, _ = await self._require_client().scroll(
            collection_name=self._collection,
            scroll_filter=_scope_filter(
                tenant_id=tenant_id,
                creator_id=creator_id,
                post_id=post_id,
            ),
            limit=10_000,
            with_payload=False,
            with_vectors=False,
        )
        return {str(record.id) for record in records}

    def _require_client(self) -> AsyncQdrantClient:
        if self._client is None:
            raise RuntimeError("Qdrant creator semantic memory has not been started")
        return self._client


def _chunk_post(
    post: CreatorHistoricalPost,
    *,
    chunk_chars: int,
    overlap_chars: int,
) -> tuple[str, ...]:
    text = "\n\n".join(
        value.strip()
        for value in (post.description, post.body)
        if value and value.strip()
    )
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + chunk_chars // 2, end),
                text.rfind("。", start + chunk_chars // 2, end),
                text.rfind(". ", start + chunk_chars // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return tuple(chunks)


def _scope_filter(
    *,
    tenant_id: str,
    creator_id: str,
    post_id: str | None = None,
) -> models.Filter:
    conditions: list[models.Condition] = [
        models.FieldCondition(
            key="tenant_id",
            match=models.MatchValue(value=tenant_id),
        ),
        models.FieldCondition(
            key="creator_id",
            match=models.MatchValue(value=creator_id),
        ),
    ]
    if post_id is not None:
        conditions.append(
            models.FieldCondition(
                key="post_id",
                match=models.MatchValue(value=post_id),
            )
        )
    return models.Filter(must=conditions)


def _point_id(post: CreatorHistoricalPost, chunk_index: int) -> str:
    scope = f"{post.tenant_id}:{post.creator_id}:{post.post_id}:{chunk_index}"
    return str(uuid.uuid5(_POINT_NAMESPACE, scope))


def _post_hash(post: CreatorHistoricalPost) -> str:
    payload = "\n".join(
        (
            post.title,
            post.description,
            post.body,
            "|".join(post.tags),
            post.source_revision or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hit_from_point(point: Any) -> CreatorSemanticMemoryHit:
    payload = point.payload or {}
    published_at = payload.get("published_at")
    return CreatorSemanticMemoryHit(
        post_id=str(payload["post_id"]),
        chunk_id=str(payload["chunk_id"]),
        chunk_index=int(payload["chunk_index"]),
        title=str(payload["title"]),
        excerpt=str(payload["excerpt"]),
        tags=tuple(str(tag) for tag in payload.get("tags", ())),
        content_type=str(payload.get("content_type") or "unknown"),
        visibility=str(payload.get("visibility") or "unknown"),
        published_at=(
            datetime.fromisoformat(str(published_at)) if published_at else None
        ),
        metrics=CreatorEngagementMetrics.model_validate(payload.get("metrics") or {}),
        semantic_score=max(0.0, min(1.0, float(point.score))),
        source_system=str(payload.get("source_system") or "unknown"),
    )
