from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.creator.memory.errors import CreatorMemoryConflictError
from app.creator.memory.models import CreatorLongTermProfile, CreatorTaskMemory


class InMemoryCreatorShortTermMemoryStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], CreatorTaskMemory] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        *,
        tenant_id: str,
        task_id: str,
    ) -> CreatorTaskMemory | None:
        async with self._lock:
            return self._items.get((tenant_id, task_id))

    async def put(
        self,
        memory: CreatorTaskMemory,
        *,
        expected_version: int | None,
    ) -> CreatorTaskMemory:
        key = (memory.tenant_id, memory.task_id)
        async with self._lock:
            existing = self._items.get(key)
            actual_version = existing.version if existing is not None else 0
            if expected_version is not None and actual_version != expected_version:
                raise CreatorMemoryConflictError(
                    "Creator task memory changed concurrently",
                    details={
                        "expected_version": expected_version,
                        "actual_version": actual_version,
                    },
                )
            stored = memory.model_copy(
                update={
                    "version": actual_version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._items[key] = stored
            return stored

    async def delete(self, *, tenant_id: str, task_id: str) -> None:
        async with self._lock:
            self._items.pop((tenant_id, task_id), None)


class InMemoryCreatorLongTermProfileStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], CreatorLongTermProfile] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> CreatorLongTermProfile | None:
        async with self._lock:
            return self._items.get((tenant_id, creator_id))

    async def put(
        self,
        profile: CreatorLongTermProfile,
        *,
        expected_version: int | None,
    ) -> CreatorLongTermProfile:
        key = (profile.tenant_id, profile.creator_id)
        async with self._lock:
            existing = self._items.get(key)
            actual_version = existing.version if existing is not None else 0
            if expected_version is not None and actual_version != expected_version:
                raise CreatorMemoryConflictError(
                    "Creator profile changed concurrently",
                    details={
                        "expected_version": expected_version,
                        "actual_version": actual_version,
                    },
                )
            now = datetime.now(UTC)
            stored = profile.model_copy(
                update={
                    "version": actual_version + 1,
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                }
            )
            self._items[key] = stored
            return stored
