from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.infrastructure.sqlalchemy import CreatorBase
from app.creator.publication.models import (
    ContentOrigin,
    PublicationHandoff,
    PublicationHandoffStatus,
)


class CreatorPublicationHandoffRow(CreatorBase):
    __tablename__ = "creator_publication_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "task_id",
            "source_artifact_id",
            name="uq_creator_publication_handoff_artifact",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    content_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    external_draft_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SqlAlchemyCreatorPublicationHandoffStore:
    backend_name = "sqlalchemy"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_task_artifact(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        source_artifact_id: str,
    ) -> PublicationHandoff | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CreatorPublicationHandoffRow).where(
                    CreatorPublicationHandoffRow.tenant_id == tenant_id,
                    CreatorPublicationHandoffRow.creator_id == creator_id,
                    CreatorPublicationHandoffRow.task_id == task_id,
                    CreatorPublicationHandoffRow.source_artifact_id
                    == source_artifact_id,
                )
            )
            return _from_row(row) if row is not None else None

    async def add(self, handoff: PublicationHandoff) -> None:
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    CreatorPublicationHandoffRow(
                        id=handoff.id,
                        tenant_id=handoff.tenant_id,
                        creator_id=handoff.creator_id,
                        task_id=handoff.task_id,
                        draft_id=handoff.draft_id,
                        content_origin=handoff.content_origin.value,
                        source_artifact_id=handoff.source_artifact_id,
                        source_artifact_revision=handoff.source_artifact_revision,
                        source_content_sha256=handoff.source_content_sha256,
                        external_draft_id=handoff.external_draft_id,
                        title=handoff.title,
                        status=handoff.status.value,
                        created_at=handoff.created_at,
                    )
                )

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[PublicationHandoff, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorPublicationHandoffRow)
                    .where(
                        CreatorPublicationHandoffRow.tenant_id == tenant_id,
                        CreatorPublicationHandoffRow.creator_id == creator_id,
                        CreatorPublicationHandoffRow.task_id == task_id,
                    )
                    .order_by(CreatorPublicationHandoffRow.created_at.desc())
                )
            ).all()
            return tuple(_from_row(row) for row in rows)


def _from_row(row: CreatorPublicationHandoffRow) -> PublicationHandoff:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return PublicationHandoff(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        draft_id=row.draft_id,
        content_origin=ContentOrigin(row.content_origin),
        source_artifact_id=row.source_artifact_id,
        source_artifact_revision=row.source_artifact_revision,
        source_content_sha256=row.source_content_sha256,
        external_draft_id=row.external_draft_id,
        title=row.title,
        status=PublicationHandoffStatus(row.status),
        created_at=created,
    )
