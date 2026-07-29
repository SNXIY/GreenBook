from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.creator.memory.ports import CreatorTextEmbedder
from app.creator.retrieval.errors import CreatorRetrievalIntegrityError
from app.creator.retrieval.models import (
    CreatorCorpusDocument,
    CreatorRetrievalFilters,
    CreatorSourceHit,
    RetrievalChannel,
)
from app.creator.retrieval.scoring import query_sha256


logger = logging.getLogger(__name__)

_POINT_NAMESPACE = uuid.UUID("7d862284-cff8-48d4-887a-7b13b07927d7")


class QdrantCreatorVectorSource:
    channel = RetrievalChannel.QDRANT
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
        max_excerpt_chars: int = 1_200,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        if not url and client is None:
            raise ValueError("Qdrant URL is required")
        if chunk_chars < 200:
            raise ValueError("Qdrant retrieval chunk size must be at least 200")
        if chunk_overlap_chars < 0 or chunk_overlap_chars >= chunk_chars:
            raise ValueError("Qdrant retrieval chunk overlap is invalid")
        if not 0.0 <= score_threshold <= 1.0:
            raise ValueError("Qdrant retrieval score threshold is invalid")
        if max_excerpt_chars < 200:
            raise ValueError("Qdrant retrieval excerpt must be at least 200")
        self._url = url
        self._api_key = api_key or None
        self._collection = collection_name
        self._embedder = embedder
        self._chunk_chars = chunk_chars
        self._chunk_overlap = chunk_overlap_chars
        self._score_threshold = score_threshold
        self._max_excerpt_chars = max_excerpt_chars
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
            try:
                await client.create_collection(
                    collection_name=self._collection,
                    vectors_config=models.VectorParams(
                        size=self._embedder.dimensions,
                        distance=models.Distance.COSINE,
                    ),
                    on_disk_payload=True,
                )
            except Exception:
                if not await client.collection_exists(self._collection):
                    raise
        await self._validate_collection()
        await self._ensure_payload_indexes()

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        filters: CreatorRetrievalFilters,
        limit: int,
    ) -> tuple[CreatorSourceHit, ...]:
        if limit <= 0:
            return ()
        vector = (await self._embedder.embed((query,)))[0]
        response = await self._require_client().query_points(
            collection_name=self._collection,
            query=list(vector),
            query_filter=_search_filter(tenant_id, filters),
            with_payload=True,
            limit=limit,
            score_threshold=self._score_threshold or None,
        )
        query_hash = query_sha256(query)
        hits: list[CreatorSourceHit] = []
        for point in response.points:
            try:
                hit = _hit_from_point(
                    point,
                    tenant_id=tenant_id,
                    filters=filters,
                    rank=len(hits) + 1,
                    query_hash=query_hash,
                    max_excerpt_chars=self._max_excerpt_chars,
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Ignoring malformed Qdrant retrieval point id=%s error=%s",
                    point.id,
                    type(exc).__name__,
                )
                continue
            if hit is not None:
                hits.append(hit)
        return tuple(hits)

    async def upsert_document(self, document: CreatorCorpusDocument) -> int:
        if not document.is_public_and_published:
            await self.delete_document(
                tenant_id=document.tenant_id,
                document_id=document.document_id,
            )
            return 0
        chunks = _chunk_document(
            document,
            chunk_chars=self._chunk_chars,
            overlap_chars=self._chunk_overlap,
        )
        vectors = await self._embedder.embed(
            tuple(f"{document.title}\n\n{chunk}" for chunk in chunks)
        )
        if len(vectors) != len(chunks):
            raise CreatorRetrievalIntegrityError(
                "Qdrant retrieval embedder returned the wrong vector count",
                details={
                    "document_id": document.document_id,
                    "expected": len(chunks),
                    "actual": len(vectors),
                },
            )
        existing = await self._document_point_ids(
            tenant_id=document.tenant_id,
            document_id=document.document_id,
        )
        new_ids: set[str] = set()
        points: list[models.PointStruct] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            point_id = _point_id(document, index)
            new_ids.add(point_id)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=list(vector),
                    payload={
                        "tenant_id": document.tenant_id,
                        "creator_id": document.creator_id,
                        "document_id": document.document_id,
                        "chunk_id": f"{document.document_id}:{index}",
                        "chunk_index": index,
                        "title": document.title,
                        "excerpt": chunk,
                        "tags": list(document.tags),
                        "content_type": document.content_type,
                        "visibility": "public",
                        "status": "published",
                        "source_url": document.source_url,
                        "published_at": (
                            document.published_at.isoformat()
                            if document.published_at is not None
                            else None
                        ),
                        "updated_at": document.updated_at.isoformat(),
                        "metrics": document.metrics.model_dump(mode="json"),
                        "authority_score": document.authority_score,
                        "source_system": document.source_system,
                        "source_revision": document.source_revision,
                    },
                )
            )
        if points:
            await self._require_client().upsert(
                collection_name=self._collection,
                points=points,
                wait=True,
            )
        stale_ids = sorted(existing - new_ids)
        if stale_ids:
            stale_point_ids: list[int | str | uuid.UUID] = list(stale_ids)
            await self._require_client().delete(
                collection_name=self._collection,
                points_selector=models.PointIdsList(points=stale_point_ids),
                wait=True,
            )
        return len(points)

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> None:
        await self._require_client().delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        _match("tenant_id", tenant_id),
                        _match("document_id", document_id),
                    ]
                )
            ),
            wait=True,
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None

    async def _validate_collection(self) -> None:
        info = await self._require_client().get_collection(self._collection)
        vectors = info.config.params.vectors
        actual_size = getattr(vectors, "size", None)
        if actual_size is not None and int(actual_size) != self._embedder.dimensions:
            raise CreatorRetrievalIntegrityError(
                "Qdrant retrieval vector dimensions do not match",
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
        for field_name in (
            "creator_id",
            "document_id",
            "tags",
            "content_type",
            "visibility",
            "status",
        ):
            if field_name not in existing:
                await client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
        if "published_at" not in existing:
            await client.create_payload_index(
                collection_name=self._collection,
                field_name="published_at",
                field_schema=models.PayloadSchemaType.DATETIME,
                wait=True,
            )

    async def _document_point_ids(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> set[str]:
        records, _ = await self._require_client().scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    _match("tenant_id", tenant_id),
                    _match("document_id", document_id),
                ]
            ),
            limit=10_000,
            with_payload=False,
            with_vectors=False,
        )
        return {str(record.id) for record in records}

    def _require_client(self) -> AsyncQdrantClient:
        if self._client is None:
            raise RuntimeError("Qdrant creator retrieval source has not been started")
        return self._client


def _search_filter(
    tenant_id: str,
    filters: CreatorRetrievalFilters,
) -> models.Filter:
    conditions: list[models.Condition] = [
        _match("tenant_id", tenant_id),
        _match("status", "published"),
        _match("visibility", "public"),
    ]
    if filters.tags:
        conditions.append(
            models.FieldCondition(
                key="tags",
                match=models.MatchAny(any=list(filters.tags)),
            )
        )
    if filters.creator_ids:
        conditions.append(
            models.FieldCondition(
                key="creator_id",
                match=models.MatchAny(any=list(filters.creator_ids)),
            )
        )
    if filters.content_types:
        conditions.append(
            models.FieldCondition(
                key="content_type",
                match=models.MatchAny(any=list(filters.content_types)),
            )
        )
    if filters.published_after is not None or filters.published_before is not None:
        conditions.append(
            models.FieldCondition(
                key="published_at",
                range=models.DatetimeRange(
                    gte=filters.published_after,
                    lt=filters.published_before,
                ),
            )
        )
    return models.Filter(must=conditions)


def _match(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(
        key=key,
        match=models.MatchValue(value=value),
    )


def _hit_from_point(
    point: Any,
    *,
    tenant_id: str,
    filters: CreatorRetrievalFilters,
    rank: int,
    query_hash: str,
    max_excerpt_chars: int,
) -> CreatorSourceHit | None:
    payload = point.payload or {}
    if str(payload.get("tenant_id")) != tenant_id:
        return None
    if str(payload.get("status", "")).lower() != "published":
        return None
    if str(payload.get("visibility", "")).lower() != "public":
        return None
    creator_id = str(payload["creator_id"])
    content_type = str(payload.get("content_type") or "")
    tags = tuple(str(tag) for tag in payload.get("tags") or ())
    if filters.creator_ids and creator_id not in filters.creator_ids:
        return None
    if filters.content_types and content_type not in filters.content_types:
        return None
    if filters.tags and not set(tags).intersection(filters.tags):
        return None
    published_at = payload.get("published_at")
    excerpt = str(payload.get("excerpt") or "").strip()
    if len(excerpt) > max_excerpt_chars:
        excerpt = excerpt[: max_excerpt_chars - 1].rstrip() + "…"
    return CreatorSourceHit(
        channel=RetrievalChannel.QDRANT,
        backend="qdrant",
        tenant_id=tenant_id,
        creator_id=creator_id,
        document_id=str(payload["document_id"]),
        title=str(payload["title"]),
        excerpt=excerpt,
        tags=tags,
        source_url=(str(payload["source_url"]) if payload.get("source_url") else None),
        published_at=(
            datetime.fromisoformat(str(published_at)) if published_at else None
        ),
        raw_score=max(0.0, min(1.0, float(point.score))),
        rank=rank,
        query_hash=query_hash,
    )


def _chunk_document(
    document: CreatorCorpusDocument,
    *,
    chunk_chars: int,
    overlap_chars: int,
) -> tuple[str, ...]:
    text = "\n\n".join(
        value.strip()
        for value in (document.description, document.body)
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


def _point_id(document: CreatorCorpusDocument, chunk_index: int) -> str:
    scope = f"{document.tenant_id}:{document.document_id}:{chunk_index}"
    return str(uuid.uuid5(_POINT_NAMESPACE, scope))
