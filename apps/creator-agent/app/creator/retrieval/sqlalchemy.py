from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.infrastructure.sqlalchemy import CreatorBase
from app.creator.memory.models import CreatorEngagementMetrics
from app.creator.retrieval.models import (
    CreatorCorpusDocument,
    CreatorRetrievalFilters,
    CreatorSourceHit,
    RetrievalChannel,
)
from app.creator.retrieval.scoring import (
    best_excerpt,
    bm25_scores,
    freshness_score,
    normalize_scores,
    query_sha256,
    raw_business_score,
    searchable_text,
)


class CreatorRetrievalDocumentRow(CreatorBase):
    __tablename__ = "creator_retrieval_documents"
    __table_args__ = (
        Index(
            "ix_creator_retrieval_scope_visibility",
            "tenant_id",
            "status",
            "visibility",
            "published_at",
        ),
        Index(
            "ix_creator_retrieval_scope_creator",
            "tenant_id",
            "creator_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    heat_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    authority_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.7,
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )


class SqlAlchemyCreatorDocumentAuthority:
    channel = RetrievalChannel.SQL
    backend_name = "postgresql"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        candidate_pool_size: int = 200,
        max_excerpt_chars: int = 1_200,
    ) -> None:
        if candidate_pool_size < 20:
            raise ValueError("SQL retrieval candidate pool must be at least 20")
        if max_excerpt_chars < 200:
            raise ValueError("SQL retrieval excerpt must be at least 200 characters")
        self._sessions = sessions
        self._candidate_pool_size = candidate_pool_size
        self._max_excerpt_chars = max_excerpt_chars

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
        pool_limit = min(
            self._candidate_pool_size,
            max(50, limit * 10),
        )
        statement = (
            select(CreatorRetrievalDocumentRow)
            .where(
                CreatorRetrievalDocumentRow.tenant_id == tenant_id,
                CreatorRetrievalDocumentRow.status == "published",
                CreatorRetrievalDocumentRow.visibility == "public",
            )
            .order_by(
                CreatorRetrievalDocumentRow.heat_score.desc(),
                CreatorRetrievalDocumentRow.published_at.desc(),
            )
            .limit(pool_limit)
        )
        statement = _apply_database_filters(statement, filters)
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
        documents = [
            _document_from_row(row)
            for row in rows
            if _matches_post_filters(row, filters)
        ]
        lexical_raw = bm25_scores(
            query,
            {document.document_id: searchable_text(document) for document in documents},
        )
        documents = [
            document
            for document in documents
            if lexical_raw.get(document.document_id, 0.0) > 0
        ]
        if not documents:
            return ()
        lexical = normalize_scores(lexical_raw)
        business = normalize_scores(
            {
                document.document_id: raw_business_score(document.metrics)
                for document in documents
            }
        )
        ranked = sorted(
            documents,
            key=lambda document: (
                business.get(document.document_id, 0.0) * 0.55
                + lexical.get(document.document_id, 0.0) * 0.35
                + freshness_score(document.published_at) * 0.10
            ),
            reverse=True,
        )[:limit]
        query_hash = query_sha256(query)
        return tuple(
            CreatorSourceHit(
                channel=self.channel,
                backend=self.backend_name,
                tenant_id=document.tenant_id,
                creator_id=document.creator_id,
                document_id=document.document_id,
                title=document.title,
                excerpt=best_excerpt(
                    query,
                    document,
                    max_chars=self._max_excerpt_chars,
                ),
                tags=document.tags,
                source_url=document.source_url,
                published_at=document.published_at,
                raw_score=lexical_raw[document.document_id],
                rank=rank,
                query_hash=query_hash,
            )
            for rank, document in enumerate(ranked, start=1)
        )

    async def load_authorized(
        self,
        *,
        tenant_id: str,
        document_ids: tuple[str, ...],
        filters: CreatorRetrievalFilters,
    ) -> tuple[CreatorCorpusDocument, ...]:
        if not document_ids:
            return ()
        statement = select(CreatorRetrievalDocumentRow).where(
            CreatorRetrievalDocumentRow.tenant_id == tenant_id,
            CreatorRetrievalDocumentRow.document_id.in_(document_ids),
            CreatorRetrievalDocumentRow.status == "published",
            CreatorRetrievalDocumentRow.visibility == "public",
        )
        statement = _apply_database_filters(statement, filters)
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
        by_id = {
            row.document_id: _document_from_row(row)
            for row in rows
            if _matches_post_filters(row, filters)
        }
        return tuple(
            by_id[document_id] for document_id in document_ids if document_id in by_id
        )

    async def upsert_document(
        self,
        document: CreatorCorpusDocument,
    ) -> CreatorCorpusDocument:
        async with self._sessions() as session:
            async with session.begin():
                key = {
                    "tenant_id": document.tenant_id,
                    "document_id": document.document_id,
                }
                row = await session.get(
                    CreatorRetrievalDocumentRow,
                    key,
                    with_for_update=True,
                )
                if row is None:
                    row = CreatorRetrievalDocumentRow(
                        tenant_id=document.tenant_id,
                        document_id=document.document_id,
                        creator_id=document.creator_id,
                        title=document.title,
                        body=document.body,
                        description=document.description,
                        tags_json=list(document.tags),
                        content_type=document.content_type,
                        visibility=document.visibility.strip().lower(),
                        status=document.status.strip().lower(),
                        source_url=document.source_url,
                        published_at=document.published_at,
                        updated_at=document.updated_at,
                        metrics_json=document.metrics.model_dump(mode="json"),
                        heat_score=document.metrics.heat_score,
                        authority_score=document.authority_score,
                        source_system=document.source_system,
                        source_revision=document.source_revision,
                        content_sha256=_content_hash(document),
                        projection_version=1,
                    )
                    session.add(row)
                else:
                    _update_row(row, document)
            await session.refresh(row)
            return _document_from_row(row)

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(CreatorRetrievalDocumentRow).where(
                        CreatorRetrievalDocumentRow.tenant_id == tenant_id,
                        CreatorRetrievalDocumentRow.document_id == document_id,
                    )
                )


def _apply_database_filters(statement, filters: CreatorRetrievalFilters):
    if filters.creator_ids:
        statement = statement.where(
            CreatorRetrievalDocumentRow.creator_id.in_(filters.creator_ids)
        )
    if filters.content_types:
        statement = statement.where(
            CreatorRetrievalDocumentRow.content_type.in_(filters.content_types)
        )
    if filters.published_after is not None:
        statement = statement.where(
            CreatorRetrievalDocumentRow.published_at >= filters.published_after
        )
    if filters.published_before is not None:
        statement = statement.where(
            CreatorRetrievalDocumentRow.published_at < filters.published_before
        )
    return statement


def _matches_post_filters(
    row: CreatorRetrievalDocumentRow,
    filters: CreatorRetrievalFilters,
) -> bool:
    if filters.tags and not set(filters.tags).intersection(row.tags_json or ()):
        return False
    return True


def _update_row(
    row: CreatorRetrievalDocumentRow,
    document: CreatorCorpusDocument,
) -> None:
    content_hash = _content_hash(document)
    changed = row.content_sha256 != content_hash
    row.creator_id = document.creator_id
    row.title = document.title
    row.body = document.body
    row.description = document.description
    row.tags_json = list(document.tags)
    row.content_type = document.content_type
    row.visibility = document.visibility.strip().lower()
    row.status = document.status.strip().lower()
    row.source_url = document.source_url
    row.published_at = document.published_at
    row.updated_at = document.updated_at
    row.metrics_json = document.metrics.model_dump(mode="json")
    row.heat_score = document.metrics.heat_score
    row.authority_score = document.authority_score
    row.source_system = document.source_system
    row.source_revision = document.source_revision
    row.content_sha256 = content_hash
    if changed:
        row.projection_version += 1


def _document_from_row(
    row: CreatorRetrievalDocumentRow,
) -> CreatorCorpusDocument:
    return CreatorCorpusDocument(
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        document_id=row.document_id,
        title=row.title,
        body=row.body,
        description=row.description,
        tags=tuple(str(tag) for tag in (row.tags_json or ())),
        content_type=row.content_type,
        visibility=row.visibility,
        status=row.status,
        source_url=row.source_url,
        published_at=row.published_at,
        updated_at=row.updated_at,
        metrics=CreatorEngagementMetrics.model_validate(row.metrics_json or {}),
        authority_score=row.authority_score,
        source_system=row.source_system,
        source_revision=row.source_revision,
    )


def _content_hash(document: CreatorCorpusDocument) -> str:
    payload = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
