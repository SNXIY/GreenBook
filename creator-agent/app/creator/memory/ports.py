from __future__ import annotations

from typing import Protocol

from app.creator.memory.models import (
    CreatorHistoricalPost,
    CreatorLongTermProfile,
    CreatorMemoryBundle,
    CreatorMemoryQuery,
    CreatorSemanticMemoryHit,
    CreatorTaskMemory,
)


class CreatorShortTermMemoryStore(Protocol):
    backend_name: str

    async def get(
        self,
        *,
        tenant_id: str,
        task_id: str,
    ) -> CreatorTaskMemory | None: ...

    async def put(
        self,
        memory: CreatorTaskMemory,
        *,
        expected_version: int | None,
    ) -> CreatorTaskMemory: ...

    async def delete(self, *, tenant_id: str, task_id: str) -> None: ...


class CreatorLongTermProfileStore(Protocol):
    backend_name: str

    async def get(
        self,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> CreatorLongTermProfile | None: ...

    async def put(
        self,
        profile: CreatorLongTermProfile,
        *,
        expected_version: int | None,
    ) -> CreatorLongTermProfile: ...


class CreatorSemanticMemoryStore(Protocol):
    backend_name: str

    async def upsert_post(self, post: CreatorHistoricalPost) -> int: ...

    async def delete_post(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        post_id: str,
    ) -> None: ...

    async def search(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        query: str,
        limit: int,
        tags: tuple[str, ...] = (),
    ) -> tuple[CreatorSemanticMemoryHit, ...]: ...


class CreatorTextEmbedder(Protocol):
    name: str
    dimensions: int

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class CreatorMemoryReader(Protocol):
    async def load(self, query: CreatorMemoryQuery) -> CreatorMemoryBundle: ...
