from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.creator.drafts.sqlalchemy import SqlAlchemyCreatorDraftStore
from app.creator.evaluation.sqlalchemy import (
    SqlAlchemyCreatorEvaluationSnapshotReader,
    SqlAlchemyCreatorEvaluationStore,
)
from app.creator.infrastructure.sqlalchemy import (
    SqlAlchemyCreatorArtifactStore,
    SqlAlchemyCreatorUnitOfWorkFactory,
    create_creator_schema,
)
from app.creator.memory.sqlalchemy import SqlAlchemyCreatorLongTermProfileStore
from app.creator.publication.sqlalchemy import (
    SqlAlchemyCreatorPublicationHandoffStore,
)
from app.creator.retrieval.sqlalchemy import SqlAlchemyCreatorDocumentAuthority
from app.creator.studio import sqlalchemy as studio_models  # noqa: F401
from app.creator.tools.sqlalchemy import SqlAlchemyCreatorToolAuditStore


class CreatorDatabaseSettings(Protocol):
    creator_database_url: str
    creator_database_echo: bool
    creator_database_pool_size: int
    creator_database_max_overflow: int


@dataclass(frozen=True)
class CreatorDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    uow_factory: SqlAlchemyCreatorUnitOfWorkFactory
    artifact_store: SqlAlchemyCreatorArtifactStore
    profile_store: SqlAlchemyCreatorLongTermProfileStore
    retrieval_store: SqlAlchemyCreatorDocumentAuthority
    draft_store: SqlAlchemyCreatorDraftStore
    publication_store: SqlAlchemyCreatorPublicationHandoffStore
    tool_audit_store: SqlAlchemyCreatorToolAuditStore
    evaluation_store: SqlAlchemyCreatorEvaluationStore
    evaluation_snapshot_reader: SqlAlchemyCreatorEvaluationSnapshotReader

    @classmethod
    def from_url(
        cls,
        database_url: str,
        *,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
    ) -> CreatorDatabase:
        engine_options: dict = {
            "echo": echo,
            "pool_pre_ping": True,
        }
        if not database_url.startswith("sqlite"):
            engine_options.update(
                pool_size=max(1, pool_size),
                max_overflow=max(0, max_overflow),
            )
        engine = create_async_engine(database_url, **engine_options)
        if database_url.startswith("sqlite"):

            @event.listens_for(engine.sync_engine, "connect")
            def enable_sqlite_foreign_keys(
                dbapi_connection: Any,
                connection_record: Any,
            ) -> None:
                del connection_record
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        sessions = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=True,
        )
        return cls(
            engine=engine,
            sessions=sessions,
            uow_factory=SqlAlchemyCreatorUnitOfWorkFactory(sessions),
            artifact_store=SqlAlchemyCreatorArtifactStore(sessions),
            profile_store=SqlAlchemyCreatorLongTermProfileStore(sessions),
            retrieval_store=SqlAlchemyCreatorDocumentAuthority(sessions),
            draft_store=SqlAlchemyCreatorDraftStore(sessions),
            publication_store=SqlAlchemyCreatorPublicationHandoffStore(sessions),
            tool_audit_store=SqlAlchemyCreatorToolAuditStore(sessions),
            evaluation_store=SqlAlchemyCreatorEvaluationStore(sessions),
            evaluation_snapshot_reader=SqlAlchemyCreatorEvaluationSnapshotReader(
                sessions
            ),
        )

    @classmethod
    def from_settings(cls, settings: CreatorDatabaseSettings) -> CreatorDatabase:
        return cls.from_url(
            settings.creator_database_url,
            echo=settings.creator_database_echo,
            pool_size=settings.creator_database_pool_size,
            max_overflow=settings.creator_database_max_overflow,
        )

    async def create_schema_for_development(self) -> None:
        await create_creator_schema(self.engine)

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    def diagnostics(self) -> dict[str, str]:
        url = self.engine.url.render_as_string(hide_password=True)
        database_path = ""
        if url.startswith("sqlite"):
            database_path = str(Path(self.engine.url.database or "").resolve())
        return {
            "store_instance_id": f"database:{id(self)}",
            "database_url": url,
            "database_absolute_path": database_path,
            "connection_pool_identity": f"pool:{id(self.engine.pool)}",
        }

    async def dispose(self) -> None:
        await self.engine.dispose()
