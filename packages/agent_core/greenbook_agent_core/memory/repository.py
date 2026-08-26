"""Memory persistence contracts and implementations."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa

from .models import MemoryQuery, MemoryRecord


class MemoryRepository(Protocol):
    """Canonical source-of-truth contract for long-term memory."""

    def save(self, record: MemoryRecord) -> MemoryRecord | Awaitable[MemoryRecord]: ...
    def get(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None | Awaitable[MemoryRecord | None]: ...
    def search(self, query: MemoryQuery) -> list[MemoryRecord] | Awaitable[list[MemoryRecord]]: ...
    def touch(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None | Awaitable[MemoryRecord | None]: ...
    def delete(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Any: ...
    def find_by_source(
        self,
        user_id: str,
        source_type: str,
        source_id: str,
        *,
        tenant_id: str = "",
    ) -> MemoryRecord | None | Awaitable[MemoryRecord | None]: ...


class InMemoryMemoryRepository:
    """Deterministic repository for tests and local development.

    The module-level default used by ``MemoryManager`` is intentionally
    process-shared, while production can inject ``PostgresMemoryRepository``.
    """

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def save(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.memory_id] = record.model_copy(deep=True)
        return record

    def get(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None:
        value = self._records.get(memory_id)
        if value is not None and not _in_scope(
            value,
            user_id=user_id,
            tenant_id=tenant_id,
        ):
            return None
        return value.model_copy(deep=True) if value else None

    def find_by_id(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None:
        return self.get(memory_id, user_id=user_id, tenant_id=tenant_id)

    def find_by_source(
        self,
        user_id: str,
        source_type: str,
        source_id: str,
        *,
        tenant_id: str = "",
    ) -> MemoryRecord | None:
        for item in self._records.values():
            if (
                item.user_id == user_id
                and item.tenant_id == tenant_id
                and item.source_type == source_type
                and item.source_id == source_id
            ):
                return item.model_copy(deep=True)
        return None

    def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        values = [item for item in self._records.values() if _matches(item, query)]
        values = [item for item in values if not _expired(item)]
        if query.keywords:
            terms = [term.casefold() for term in query.keywords if term]
            values = [
                item for item in values
                if any(term in _search_text(item).casefold() for term in terms)
            ]
        if query.sort_by == "created_at":
            values.sort(key=lambda item: item.created_at, reverse=True)
        elif query.sort_by == "access_count":
            values.sort(key=lambda item: item.access_count, reverse=True)
        else:
            values.sort(key=lambda item: item.importance, reverse=True)
        return [item.model_copy(deep=True) for item in values[: query.limit]]

    def touch(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None:
        item = self._records.get(memory_id)
        if item is None or not _in_scope(item, user_id=user_id, tenant_id=tenant_id):
            return None
        item.access_count += 1
        item.last_accessed_at = datetime.now(UTC).isoformat()
        item.updated_at = item.last_accessed_at
        return item.model_copy(deep=True)

    def update(self, memory_id: str, **fields: Any) -> MemoryRecord | None:
        item = self._records.get(memory_id)
        if item is None:
            return None
        for key, value in fields.items():
            if key == "type":
                key = "memory_type"
            if key == "metadata":
                key = "structured_metadata"
            if key == "source_conversation_id":
                key = "conversation_id"
            if hasattr(item, key):
                setattr(item, key, value)
        item.updated_at = datetime.now(UTC).isoformat()
        return item.model_copy(deep=True)

    def delete(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        if memory_id in self._records and not _in_scope(
            self._records[memory_id],
            user_id=user_id,
            tenant_id=tenant_id,
        ):
            return
        self._records.pop(memory_id, None)

    def count(self, user_id: str | None = None) -> int:
        return sum(
            1 for item in self._records.values()
            if user_id is None or item.user_id == user_id
        )

    def clear(self) -> None:
        self._records.clear()


class PostgresMemoryRepository:
    """Async PostgreSQL implementation using the existing session factory."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def ensure_storage(self) -> None:
        async with self._session_factory() as session:
            await session.execute(sa.text("""
                CREATE TABLE IF NOT EXISTS agent_memories (
                    memory_id VARCHAR(128) PRIMARY KEY,
                    user_id VARCHAR(128) NOT NULL,
                    tenant_id VARCHAR(128) NOT NULL DEFAULT '',
                    conversation_id VARCHAR(128),
                    source_conversation_id VARCHAR(128),
                    task_id VARCHAR(128),
                    memory_type VARCHAR(32) NOT NULL,
                    content TEXT NOT NULL,
                    structured_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    source_type VARCHAR(64) NOT NULL DEFAULT '',
                    source_id VARCHAR(128),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_accessed_at TIMESTAMPTZ,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TIMESTAMPTZ
                )
            """))
            await session.execute(sa.text(
                "ALTER TABLE agent_memories "
                "ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128) NOT NULL DEFAULT ''"
            ))
            await session.execute(sa.text(
                "ALTER TABLE agent_memories "
                "ADD COLUMN IF NOT EXISTS source_conversation_id VARCHAR(128)"
            ))
            await session.execute(sa.text(
                "ALTER TABLE agent_memories "
                "ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active'"
            ))
            await session.execute(sa.text(
                "UPDATE agent_memories "
                "SET source_conversation_id = conversation_id "
                "WHERE source_conversation_id IS NULL"
            ))
            await session.execute(sa.text(
                "CREATE INDEX IF NOT EXISTS idx_agent_memories_scope_type "
                "ON agent_memories (tenant_id, user_id, memory_type, status, updated_at DESC)"
            ))
            await session.commit()

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        async with self._session_factory() as session:
            await session.execute(sa.text("""
                INSERT INTO agent_memories
                    (memory_id, user_id, tenant_id, conversation_id,
                     source_conversation_id, task_id, memory_type,
                     content, structured_metadata, importance, confidence,
                     status,
                     source_type, source_id, created_at, updated_at,
                     last_accessed_at, access_count, expires_at)
                VALUES
                    (:memory_id, :user_id, :tenant_id, :conversation_id,
                     :source_conversation_id, :task_id, :memory_type,
                     :content, CAST(:structured_metadata AS jsonb), :importance,
                     :confidence, :status, :source_type, :source_id, :created_at,
                     :updated_at, NULLIF(:last_accessed_at, '')::timestamptz,
                     :access_count, NULLIF(:expires_at, '')::timestamptz)
                ON CONFLICT (memory_id) DO UPDATE SET
                    structured_metadata = EXCLUDED.structured_metadata,
                    content = EXCLUDED.content,
                    importance = EXCLUDED.importance,
                    confidence = EXCLUDED.confidence,
                    status = EXCLUDED.status,
                    conversation_id = EXCLUDED.conversation_id,
                    source_conversation_id = EXCLUDED.source_conversation_id,
                    updated_at = EXCLUDED.updated_at,
                    access_count = EXCLUDED.access_count,
                    last_accessed_at = EXCLUDED.last_accessed_at
            """), _params(record))
            await session.commit()
        return record

    async def get(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None:
        async with self._session_factory() as session:
            clauses = ["memory_id = :memory_id"]
            params: dict[str, Any] = {"memory_id": memory_id}
            if user_id is not None:
                clauses.append("user_id = :user_id")
                params["user_id"] = user_id
            if tenant_id is not None:
                clauses.append("tenant_id = :tenant_id")
                params["tenant_id"] = tenant_id
            result = await session.execute(
                sa.text(
                    "SELECT * FROM agent_memories WHERE " + " AND ".join(clauses)
                ),
                params,
            )
            row = result.mappings().first()
        return _row_record(row) if row else None

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        clauses = [
            "user_id = :user_id",
            "tenant_id = :tenant_id",
            "(expires_at IS NULL OR expires_at > NOW())",
        ]
        params: dict[str, Any] = {
            "user_id": query.user_id,
            "tenant_id": query.tenant_id,
        }
        if query.conversation_id:
            clauses.append("conversation_id = :conversation_id")
            params["conversation_id"] = query.conversation_id
        if query.task_id:
            clauses.append("task_id = :task_id")
            params["task_id"] = query.task_id
        if query.type:
            clauses.append("memory_type = :memory_type")
            params["memory_type"] = query.type.value
        if query.min_importance:
            clauses.append("importance >= :min_importance")
            params["min_importance"] = query.min_importance
        if query.status:
            clauses.append("status = :status")
            params["status"] = query.status.value
        if query.metadata_filters:
            # JSONB containment is the existing metadata boundary used to
            # distinguish canonical Episodic V1 rows from legacy EPISODIC
            # history.  No schema or second table is required.
            clauses.append("structured_metadata @> CAST(:metadata_filters AS jsonb)")
            params["metadata_filters"] = json.dumps(
                query.metadata_filters,
                ensure_ascii=False,
                default=str,
            )
        order = {
            "created_at": "created_at DESC",
            "access_count": "access_count DESC",
        }.get(query.sort_by, "importance DESC")
        params["limit"] = query.limit
        async with self._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT * FROM agent_memories WHERE "
                    + " AND ".join(clauses)
                    + f" ORDER BY {order} LIMIT :limit"
                ),
                params,
            )
            rows = result.mappings().all()
        values = [_row_record(row) for row in rows]
        if query.keywords:
            values = [
                item for item in values
                if any(term.casefold() in _search_text(item).casefold() for term in query.keywords)
            ]
        return values

    async def touch(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryRecord | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            clauses = ["memory_id = :memory_id"]
            params: dict[str, Any] = {"memory_id": memory_id, "now": now}
            if user_id is not None:
                clauses.append("user_id = :user_id")
                params["user_id"] = user_id
            if tenant_id is not None:
                clauses.append("tenant_id = :tenant_id")
                params["tenant_id"] = tenant_id
            await session.execute(sa.text(
                """
                UPDATE agent_memories
                SET access_count = access_count + 1,
                    last_accessed_at = :now,
                    updated_at = :now
                WHERE """ + " AND ".join(clauses)
            ), params)
            await session.commit()
        return await self.get(memory_id, user_id=user_id, tenant_id=tenant_id)

    async def delete(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            clauses = ["memory_id = :memory_id"]
            params: dict[str, Any] = {"memory_id": memory_id}
            if user_id is not None:
                clauses.append("user_id = :user_id")
                params["user_id"] = user_id
            if tenant_id is not None:
                clauses.append("tenant_id = :tenant_id")
                params["tenant_id"] = tenant_id
            await session.execute(
                sa.text("DELETE FROM agent_memories WHERE " + " AND ".join(clauses)),
                params,
            )
            await session.commit()

    async def find_by_source(
        self,
        user_id: str,
        source_type: str,
        source_id: str,
        *,
        tenant_id: str = "",
    ) -> MemoryRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                sa.text(
                    "SELECT * FROM agent_memories "
                    "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                    "AND source_type = :source_type "
                    "AND source_id = :source_id LIMIT 1"
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "source_type": source_type,
                    "source_id": source_id,
                },
            )
            row = result.mappings().first()
        return _row_record(row) if row else None


def _matches(item: MemoryRecord, query: MemoryQuery) -> bool:
    if query.user_id and item.user_id != query.user_id:
        return False
    if item.tenant_id != query.tenant_id:
        return False
    if query.conversation_id and item.conversation_id != query.conversation_id:
        return False
    if query.task_id and item.task_id != query.task_id:
        return False
    if query.type and item.memory_type != query.type:
        return False
    if item.importance < query.min_importance:
        return False
    if query.status and item.status != query.status:
        return False
    return all(item.metadata.get(key) == value for key, value in query.metadata_filters.items())


def _in_scope(
    item: MemoryRecord,
    *,
    user_id: str | None,
    tenant_id: str | None,
) -> bool:
    if user_id is not None and item.user_id != user_id:
        return False
    return tenant_id is None or item.tenant_id == tenant_id


def _expired(item: MemoryRecord) -> bool:
    if not item.expires_at:
        return False
    try:
        return datetime.now(UTC) > datetime.fromisoformat(item.expires_at)
    except (TypeError, ValueError):
        return False


def _search_text(item: MemoryRecord) -> str:
    return " ".join(
        [item.content, json.dumps(item.metadata, ensure_ascii=False, default=str)]
    )


def _params(record: MemoryRecord) -> dict[str, Any]:
    return {
        "memory_id": record.memory_id,
        "user_id": record.user_id,
        "tenant_id": record.tenant_id,
        "conversation_id": record.conversation_id,
        "source_conversation_id": record.source_conversation_id,
        "task_id": record.task_id,
        "memory_type": record.memory_type.value,
        "content": record.content,
        "structured_metadata": json.dumps(record.metadata, ensure_ascii=False, default=str),
        "importance": record.importance,
        "confidence": record.confidence,
        "status": record.status.value,
        "source_type": record.source_type,
        "source_id": record.source_id,
        # asyncpg binds PostgreSQL timestamptz parameters as datetime values;
        # MemoryRecord intentionally exposes ISO strings at the contract
        # boundary, so convert only at this persistence boundary.
        "created_at": _database_datetime(record.created_at),
        "updated_at": _database_datetime(record.updated_at),
        "last_accessed_at": _database_datetime(record.last_accessed_at),
        "access_count": record.access_count,
        "expires_at": _database_datetime(record.expires_at),
    }


def _database_datetime(value: str | datetime | None) -> datetime | None:
    """Convert a contract timestamp into an asyncpg-compatible value."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _row_record(row: Any) -> MemoryRecord:
    value = dict(row)
    for key in ("created_at", "updated_at", "last_accessed_at", "expires_at"):
        if isinstance(value.get(key), datetime):
            value[key] = value[key].isoformat()
    return MemoryRecord.model_validate(value)


__all__ = [
    "InMemoryMemoryRepository",
    "MemoryRepository",
    "PostgresMemoryRepository",
]
