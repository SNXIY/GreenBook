from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.infrastructure.sqlalchemy import CreatorBase
from app.creator.memory.errors import CreatorMemoryConflictError
from app.creator.memory.models import CreatorLongTermProfile


class CreatorLongTermProfileRow(CreatorBase):
    __tablename__ = "creator_memory_profiles"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    creator_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    bio: Mapped[str] = mapped_column(Text, nullable=False)
    expertise_tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    audience_segments_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    style_traits_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    preferred_formats_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    explicit_preferences_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    inferred_preferences_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SqlAlchemyCreatorLongTermProfileStore:
    backend_name = "postgresql"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def get(
        self,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> CreatorLongTermProfile | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CreatorLongTermProfileRow).where(
                    CreatorLongTermProfileRow.tenant_id == tenant_id,
                    CreatorLongTermProfileRow.creator_id == creator_id,
                )
            )
        return _from_row(row) if row is not None else None

    async def put(
        self,
        profile: CreatorLongTermProfile,
        *,
        expected_version: int | None,
    ) -> CreatorLongTermProfile:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CreatorLongTermProfileRow)
                .where(
                    CreatorLongTermProfileRow.tenant_id == profile.tenant_id,
                    CreatorLongTermProfileRow.creator_id == profile.creator_id,
                )
                .with_for_update()
            )
            actual_version = row.version if row is not None else 0
            if expected_version is not None and expected_version != actual_version:
                raise _version_conflict(profile, expected_version, actual_version)

            now = self._clock()
            stored = profile.model_copy(
                update={
                    "version": actual_version + 1,
                    "created_at": _as_utc(row.created_at) if row else now,
                    "updated_at": now,
                }
            )
            if row is None:
                session.add(_to_row(stored))
            else:
                result = await session.execute(
                    update(CreatorLongTermProfileRow)
                    .where(
                        CreatorLongTermProfileRow.tenant_id == profile.tenant_id,
                        CreatorLongTermProfileRow.creator_id == profile.creator_id,
                        CreatorLongTermProfileRow.version == actual_version,
                    )
                    .values(**_values(stored))
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    raise _version_conflict(
                        profile,
                        expected_version,
                        actual_version,
                    )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise CreatorMemoryConflictError(
                    "Creator profile write conflicted",
                    details={
                        "tenant_id": profile.tenant_id,
                        "creator_id": profile.creator_id,
                    },
                ) from exc
            return stored


def _to_row(profile: CreatorLongTermProfile) -> CreatorLongTermProfileRow:
    return CreatorLongTermProfileRow(
        tenant_id=profile.tenant_id,
        creator_id=profile.creator_id,
        **_values(profile),
    )


def _values(profile: CreatorLongTermProfile) -> dict[str, Any]:
    return {
        "display_name": profile.display_name,
        "bio": profile.bio,
        "expertise_tags_json": list(profile.expertise_tags),
        "audience_segments_json": list(profile.audience_segments),
        "style_traits_json": list(profile.style_traits),
        "preferred_formats_json": list(profile.preferred_formats),
        "language": profile.language,
        "explicit_preferences_json": profile.explicit_preferences,
        "inferred_preferences_json": profile.inferred_preferences,
        "source_system": profile.source_system,
        "source_revision": profile.source_revision,
        "version": profile.version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _from_row(row: CreatorLongTermProfileRow) -> CreatorLongTermProfile:
    return CreatorLongTermProfile(
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        display_name=row.display_name,
        bio=row.bio,
        expertise_tags=tuple(row.expertise_tags_json),
        audience_segments=tuple(row.audience_segments_json),
        style_traits=tuple(row.style_traits_json),
        preferred_formats=tuple(row.preferred_formats_json),
        language=row.language,
        explicit_preferences=row.explicit_preferences_json,
        inferred_preferences=row.inferred_preferences_json,
        source_system=row.source_system,
        source_revision=row.source_revision,
        version=row.version,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _version_conflict(
    profile: CreatorLongTermProfile,
    expected_version: int | None,
    actual_version: int,
) -> CreatorMemoryConflictError:
    return CreatorMemoryConflictError(
        "Creator profile changed concurrently",
        details={
            "tenant_id": profile.tenant_id,
            "creator_id": profile.creator_id,
            "expected_version": expected_version,
            "actual_version": actual_version,
        },
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
