from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.creator.drafts.models import (
    CreateDraftRecord,
    CreatorDraftWriteResult,
    UpdateDraftRecord,
)
from app.creator.drafts.ports import CreatorDraftStore


class CreatorDraftService:
    def __init__(
        self,
        store: CreatorDraftStore,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))

    async def save_draft(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        title: str,
        content_markdown: str,
        source_artifact_id: str | None,
        editor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CreatorDraftWriteResult:
        normalized_title = title.strip()
        normalized_content = content_markdown.strip()
        payload = {
            "tenant_id": tenant_id,
            "creator_id": creator_id,
            "task_id": task_id,
            "title": normalized_title,
            "content_markdown": normalized_content,
            "source_artifact_id": source_artifact_id,
            "editor_type": editor_type,
            "actor_id": actor_id,
        }
        return await self._store.create(
            CreateDraftRecord(
                draft_id=self._id_factory(),
                tenant_id=tenant_id,
                creator_id=creator_id,
                task_id=task_id,
                title=normalized_title,
                content_markdown=normalized_content,
                content_sha256=_content_hash(normalized_content),
                source_artifact_id=source_artifact_id,
                editor_type=editor_type,
                actor_id=actor_id,
                idempotency_scope="draft.save",
                idempotency_key_hash=_hash_text(idempotency_key),
                request_hash=_canonical_hash(payload),
                now=self._clock(),
            )
        )

    async def update_draft(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
        expected_version: int,
        title: str | None,
        content_markdown: str,
        source_artifact_id: str | None,
        editor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CreatorDraftWriteResult:
        normalized_title = title.strip() if title is not None else None
        normalized_content = content_markdown.strip()
        payload = {
            "tenant_id": tenant_id,
            "creator_id": creator_id,
            "draft_id": draft_id,
            "expected_version": expected_version,
            "title": normalized_title,
            "content_markdown": normalized_content,
            "source_artifact_id": source_artifact_id,
            "editor_type": editor_type,
            "actor_id": actor_id,
        }
        return await self._store.update(
            UpdateDraftRecord(
                tenant_id=tenant_id,
                creator_id=creator_id,
                draft_id=draft_id,
                expected_version=expected_version,
                title=normalized_title,
                content_markdown=normalized_content,
                content_sha256=_content_hash(normalized_content),
                source_artifact_id=source_artifact_id,
                editor_type=editor_type,
                actor_id=actor_id,
                idempotency_scope=f"draft.update:{draft_id}",
                idempotency_key_hash=_hash_text(idempotency_key),
                request_hash=_canonical_hash(payload),
                now=self._clock(),
            )
        )


def _content_hash(content_markdown: str) -> str:
    return hashlib.sha256(content_markdown.encode("utf-8")).hexdigest()


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
