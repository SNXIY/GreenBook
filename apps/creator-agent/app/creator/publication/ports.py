from __future__ import annotations

from typing import Protocol

from app.creator.publication.models import PublicationHandoff


class CreatorPublicationHandoffStore(Protocol):
    async def get_by_task_artifact(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        source_artifact_id: str,
    ) -> PublicationHandoff | None: ...

    async def add(self, handoff: PublicationHandoff) -> None: ...

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[PublicationHandoff, ...]: ...
