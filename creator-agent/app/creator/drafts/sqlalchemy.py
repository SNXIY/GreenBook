from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.drafts.errors import (
    CreatorDraftIdempotencyError,
    CreatorDraftNotFoundError,
    CreatorDraftPersistenceError,
    CreatorDraftScopeError,
    CreatorDraftSourceArtifactError,
    CreatorDraftTaskNotFoundError,
    CreatorDraftVersionConflictError,
)
from app.creator.drafts.models import (
    CreateDraftRecord,
    CreatorDraft,
    CreatorDraftStatus,
    CreatorDraftVersion,
    CreatorDraftWriteResult,
    UpdateDraftRecord,
)
from app.creator.infrastructure.sqlalchemy import (
    CreatorArtifactRow,
    CreatorBase,
    CreatorTaskRow,
)


class CreatorDraftRow(CreatorBase):
    __tablename__ = "creator_drafts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "id",
            name="uq_creator_draft_scope",
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
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_draft_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class CreatorDraftVersionRow(CreatorBase):
    __tablename__ = "creator_draft_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "idempotency_scope",
            "idempotency_key_hash",
            name="uq_creator_draft_version_idempotency",
        ),
    )

    draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("creator_artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    editor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SqlAlchemyCreatorDraftStore:
    backend_name = "postgresql"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sessions = sessions

    async def create(
        self,
        record: CreateDraftRecord,
    ) -> CreatorDraftWriteResult:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    replay = await self._find_idempotent(
                        session,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        scope=record.idempotency_scope,
                        key_hash=record.idempotency_key_hash,
                        request_hash=record.request_hash,
                    )
                    if replay is not None:
                        return replay
                    await self._require_task(
                        session,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        task_id=record.task_id,
                    )
                    await self._require_artifact(
                        session,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        task_id=record.task_id,
                        artifact_id=record.source_artifact_id,
                    )
                    draft_row = CreatorDraftRow(
                        id=record.draft_id,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        task_id=record.task_id,
                        title=record.title,
                        current_version=1,
                        status=CreatorDraftStatus.DRAFT.value,
                        created_at=record.now,
                        updated_at=record.now,
                    )
                    version_row = CreatorDraftVersionRow(
                        draft_id=record.draft_id,
                        version=1,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        title=record.title,
                        content_markdown=record.content_markdown,
                        content_sha256=record.content_sha256,
                        source_artifact_id=record.source_artifact_id,
                        editor_type=record.editor_type,
                        actor_id=record.actor_id,
                        idempotency_scope=record.idempotency_scope,
                        idempotency_key_hash=record.idempotency_key_hash,
                        request_hash=record.request_hash,
                        created_at=record.now,
                    )
                    session.add_all((draft_row, version_row))
                return _result(draft_row, version_row)
        except IntegrityError as exc:
            replay = await self._replay_after_conflict(
                tenant_id=record.tenant_id,
                creator_id=record.creator_id,
                scope=record.idempotency_scope,
                key_hash=record.idempotency_key_hash,
                request_hash=record.request_hash,
            )
            if replay is not None:
                return replay
            raise CreatorDraftPersistenceError(
                "Draft creation conflicted with another transaction"
            ) from exc

    async def update(
        self,
        record: UpdateDraftRecord,
    ) -> CreatorDraftWriteResult:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    replay = await self._find_idempotent(
                        session,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        scope=record.idempotency_scope,
                        key_hash=record.idempotency_key_hash,
                        request_hash=record.request_hash,
                    )
                    if replay is not None:
                        return replay
                    draft_row = await session.get(
                        CreatorDraftRow,
                        record.draft_id,
                        with_for_update=True,
                    )
                    if draft_row is None:
                        raise CreatorDraftNotFoundError(
                            f"Draft {record.draft_id} was not found",
                            details={"draft_id": record.draft_id},
                        )
                    self._require_scope(
                        draft_row,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                    )
                    if draft_row.current_version != record.expected_version:
                        raise CreatorDraftVersionConflictError(
                            "Draft version changed before update",
                            details={
                                "draft_id": record.draft_id,
                                "expected_version": record.expected_version,
                                "actual_version": draft_row.current_version,
                            },
                        )
                    await self._require_artifact(
                        session,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        task_id=draft_row.task_id,
                        artifact_id=record.source_artifact_id,
                    )
                    title = record.title or draft_row.title
                    next_version = record.expected_version + 1
                    changed = await session.execute(
                        update(CreatorDraftRow)
                        .where(
                            CreatorDraftRow.id == record.draft_id,
                            CreatorDraftRow.current_version == record.expected_version,
                        )
                        .values(
                            title=title,
                            current_version=next_version,
                            updated_at=record.now,
                        )
                    )
                    if changed.rowcount != 1:
                        raise CreatorDraftVersionConflictError(
                            "Draft version changed before update",
                            details={"draft_id": record.draft_id},
                        )
                    version_row = CreatorDraftVersionRow(
                        draft_id=record.draft_id,
                        version=next_version,
                        tenant_id=record.tenant_id,
                        creator_id=record.creator_id,
                        title=title,
                        content_markdown=record.content_markdown,
                        content_sha256=record.content_sha256,
                        source_artifact_id=record.source_artifact_id,
                        editor_type=record.editor_type,
                        actor_id=record.actor_id,
                        idempotency_scope=record.idempotency_scope,
                        idempotency_key_hash=record.idempotency_key_hash,
                        request_hash=record.request_hash,
                        created_at=record.now,
                    )
                    session.add(version_row)
                    draft = _draft_from_row(draft_row).model_copy(
                        update={
                            "title": title,
                            "current_version": next_version,
                            "updated_at": record.now,
                        }
                    )
                return CreatorDraftWriteResult(
                    draft=draft,
                    version=_version_from_row(version_row),
                )
        except IntegrityError as exc:
            replay = await self._replay_after_conflict(
                tenant_id=record.tenant_id,
                creator_id=record.creator_id,
                scope=record.idempotency_scope,
                key_hash=record.idempotency_key_hash,
                request_hash=record.request_hash,
            )
            if replay is not None:
                return replay
            raise CreatorDraftPersistenceError(
                "Draft update conflicted with another transaction"
            ) from exc

    async def get(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> CreatorDraftWriteResult | None:
        async with self._sessions() as session:
            draft_row = await session.get(CreatorDraftRow, draft_id)
            if draft_row is None:
                return None
            self._require_scope(
                draft_row,
                tenant_id=tenant_id,
                creator_id=creator_id,
            )
            version_row = await session.get(
                CreatorDraftVersionRow,
                {
                    "draft_id": draft_id,
                    "version": draft_row.current_version,
                },
            )
            if version_row is None:
                raise CreatorDraftPersistenceError("Draft current version is missing")
            return _result(draft_row, version_row)

    async def _find_idempotent(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        creator_id: str,
        scope: str,
        key_hash: str,
        request_hash: str,
    ) -> CreatorDraftWriteResult | None:
        version_row = await session.scalar(
            select(CreatorDraftVersionRow).where(
                CreatorDraftVersionRow.tenant_id == tenant_id,
                CreatorDraftVersionRow.creator_id == creator_id,
                CreatorDraftVersionRow.idempotency_scope == scope,
                CreatorDraftVersionRow.idempotency_key_hash == key_hash,
            )
        )
        if version_row is None:
            return None
        if version_row.request_hash != request_hash:
            raise CreatorDraftIdempotencyError(
                "Idempotency key was reused with different draft content"
            )
        draft_row = await session.get(CreatorDraftRow, version_row.draft_id)
        if draft_row is None:
            raise CreatorDraftPersistenceError(
                "Idempotent draft version has no parent draft"
            )
        return _result(draft_row, version_row, replayed=True)

    async def _replay_after_conflict(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        scope: str,
        key_hash: str,
        request_hash: str,
    ) -> CreatorDraftWriteResult | None:
        async with self._sessions() as session:
            return await self._find_idempotent(
                session,
                tenant_id=tenant_id,
                creator_id=creator_id,
                scope=scope,
                key_hash=key_hash,
                request_hash=request_hash,
            )

    @staticmethod
    async def _require_task(
        session: AsyncSession,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> CreatorTaskRow:
        task = await session.get(CreatorTaskRow, task_id)
        if task is None:
            raise CreatorDraftTaskNotFoundError(
                f"Creator task {task_id} was not found",
                details={"task_id": task_id},
            )
        if task.tenant_id != tenant_id or task.creator_id != creator_id:
            raise CreatorDraftScopeError(
                "Creator task does not belong to the draft owner",
                details={"task_id": task_id},
            )
        return task

    @staticmethod
    async def _require_artifact(
        session: AsyncSession,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        artifact_id: str | None,
    ) -> None:
        if artifact_id is None:
            return
        artifact = await session.get(CreatorArtifactRow, artifact_id)
        if artifact is None:
            raise CreatorDraftSourceArtifactError(
                f"Source artifact {artifact_id} was not found"
            )
        if (
            artifact.tenant_id != tenant_id
            or artifact.creator_id != creator_id
            or artifact.task_id != task_id
        ):
            raise CreatorDraftSourceArtifactError(
                "Source artifact does not match draft scope",
                details={"artifact_id": artifact_id},
            )

    @staticmethod
    def _require_scope(
        row: CreatorDraftRow,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> None:
        if row.tenant_id != tenant_id or row.creator_id != creator_id:
            raise CreatorDraftScopeError(
                "Caller cannot access this draft",
                details={"draft_id": row.id},
            )


def _result(
    draft_row: CreatorDraftRow,
    version_row: CreatorDraftVersionRow,
    *,
    replayed: bool = False,
) -> CreatorDraftWriteResult:
    return CreatorDraftWriteResult(
        draft=_draft_from_row(draft_row),
        version=_version_from_row(version_row),
        replayed=replayed,
    )


def _draft_from_row(row: CreatorDraftRow) -> CreatorDraft:
    return CreatorDraft(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        title=row.title,
        current_version=row.current_version,
        status=CreatorDraftStatus(row.status),
        external_draft_id=row.external_draft_id,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _version_from_row(row: CreatorDraftVersionRow) -> CreatorDraftVersion:
    return CreatorDraftVersion(
        draft_id=row.draft_id,
        version=row.version,
        title=row.title,
        content_markdown=row.content_markdown,
        content_sha256=row.content_sha256,
        source_artifact_id=row.source_artifact_id,
        editor_type=row.editor_type,
        actor_id=row.actor_id,
        created_at=_as_utc(row.created_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
