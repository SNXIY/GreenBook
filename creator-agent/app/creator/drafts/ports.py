from __future__ import annotations

from typing import Protocol

from app.creator.drafts.models import (
    CreateDraftRecord,
    CreatorDraftWriteResult,
    UpdateDraftRecord,
)


class CreatorDraftStore(Protocol):
    backend_name: str

    async def create(
        self,
        record: CreateDraftRecord,
    ) -> CreatorDraftWriteResult: ...

    async def update(
        self,
        record: UpdateDraftRecord,
    ) -> CreatorDraftWriteResult: ...

    async def get(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> CreatorDraftWriteResult | None: ...
