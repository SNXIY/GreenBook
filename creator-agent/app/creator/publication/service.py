from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.creator.domain.models import CreatorTask, CreatorTaskStatus
from app.creator.drafts.models import CreatorDraft, CreatorDraftStatus, CreatorDraftWriteResult
from app.creator.drafts.service import CreatorDraftService
from app.creator.publication.errors import (
    CreatorPublicationArtifactError,
    CreatorPublicationLockedError,
    CreatorPublicationNotReadyError,
)
from app.creator.publication.models import (
    ContentOrigin,
    PublicationHandoff,
    PublicationHandoffResult,
    PublicationHandoffStatus,
)
from app.creator.publication.ports import CreatorPublicationHandoffStore
from app.creator.runtime.models import ArtifactKind, CreatorArtifact

DraftLoader = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


class CreatorPublicationHandoffService:
    """AI_ASSISTED publication handoff to the real Zhiguang Java draft API."""

    def __init__(
        self,
        *,
        handoffs: CreatorPublicationHandoffStore,
        drafts: CreatorDraftService,
        draft_loader: DraftLoader | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        java_base_url: str = "",
        java_shared_secret: str = "",
        java_timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._handoffs = handoffs
        self._drafts = drafts
        self._draft_loader = draft_loader
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._java_base_url = (java_base_url or "").rstrip("/")
        self._java_shared_secret = java_shared_secret or ""
        self._java_timeout_seconds = java_timeout_seconds
        self._http_client = http_client

    async def handoff(
        self,
        *,
        task: CreatorTask,
        artifact: CreatorArtifact,
        actor_id: str,
        idempotency_key: str,
    ) -> PublicationHandoffResult:
        if task.status != CreatorTaskStatus.COMPLETED:
            raise CreatorPublicationNotReadyError(
                "Only COMPLETED creator tasks can be handed off for publication",
                details={"task_id": task.id, "status": task.status.value},
            )
        _require_final_artifact(
            artifact,
            tenant_id=task.tenant_id,
            creator_id=task.creator_id,
            task_id=task.id,
        )

        existing = await self._handoffs.get_by_task_artifact(
            tenant_id=task.tenant_id,
            creator_id=task.creator_id,
            task_id=task.id,
            source_artifact_id=artifact.id,
        )
        if existing is not None:
            await self._ensure_not_locked(existing)
            return PublicationHandoffResult(handoff=existing, replayed=True)

        title, body, description, content_sha = _extract_final_document(artifact)
        draft_result = await self._drafts.save_draft(
            tenant_id=task.tenant_id,
            creator_id=task.creator_id,
            task_id=task.id,
            title=title,
            content_markdown=body,
            source_artifact_id=artifact.id,
            editor_type="AI_ASSISTED",
            actor_id=actor_id,
            idempotency_key=f"publication-handoff:{idempotency_key}",
        )
        external_draft_id = await self._create_zhiguang_ai_draft(
            creator_id=task.creator_id,
            title=title,
            body_markdown=body,
            description=description,
            content_sha256=content_sha,
            source_task_id=task.id,
            local_draft_id=draft_result.draft.id,
        )
        handoff = PublicationHandoff(
            id=self._id_factory(),
            tenant_id=task.tenant_id,
            creator_id=task.creator_id,
            task_id=task.id,
            draft_id=draft_result.draft.id,
            content_origin=ContentOrigin.AI_ASSISTED,
            source_artifact_id=artifact.id,
            source_artifact_revision=artifact.revision,
            source_content_sha256=content_sha,
            external_draft_id=external_draft_id,
            title=title,
            status=PublicationHandoffStatus.READY,
            created_at=self._clock(),
        )
        await self._handoffs.add(handoff)
        return PublicationHandoffResult(handoff=handoff, replayed=False)

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[PublicationHandoff, ...]:
        return await self._handoffs.list_for_task(
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=task_id,
        )

    async def _create_zhiguang_ai_draft(
        self,
        *,
        creator_id: str,
        title: str,
        body_markdown: str,
        description: str,
        content_sha256: str,
        source_task_id: str,
        local_draft_id: str,
    ) -> str:
        if not self._java_base_url or not self._java_shared_secret:
            raise CreatorPublicationNotReadyError(
                "Zhiguang publication endpoint and shared secret are required",
                details={"local_draft_id": local_draft_id},
            )

        try:
            java_creator_id = int(str(creator_id).strip())
        except ValueError as exc:
            raise CreatorPublicationNotReadyError(
                "Zhiguang creator identity must be a numeric user id",
                details={"creator_id": creator_id},
            ) from exc

        payload = {
            "creatorId": java_creator_id,
            "title": title,
            "bodyMarkdown": body_markdown,
            "description": description,
            "sourceTaskId": source_task_id,
            "contentSha256": content_sha256,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Creator-Handoff-Secret": self._java_shared_secret,
        }
        url = f"{self._java_base_url}/api/v1/knowposts/ai-drafts"
        client = self._http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=self._java_timeout_seconds)
        assert client is not None
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            draft_id = str(data.get("id") or "").strip()
            if not draft_id:
                raise RuntimeError("Zhiguang ai-drafts response missing id")
            return draft_id
        except Exception as exc:
            logger.exception("Failed to create Zhiguang AI draft")
            raise CreatorPublicationNotReadyError(
                "Zhiguang draft synchronization failed; no platform draft was created",
                details={"source_task_id": source_task_id},
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

    async def _ensure_not_locked(self, existing: PublicationHandoff) -> None:
        if existing.status == PublicationHandoffStatus.LOCKED:
            raise CreatorPublicationLockedError(
                "Publication handoff is locked because the draft already entered publish flow",
                details={"handoff_id": existing.id, "draft_id": existing.draft_id},
            )
        if self._draft_loader is None:
            return
        loaded = await self._draft_loader(
            tenant_id=existing.tenant_id,
            creator_id=existing.creator_id,
            draft_id=existing.draft_id,
        )
        draft = (
            loaded.draft
            if isinstance(loaded, CreatorDraftWriteResult)
            else loaded
        )
        if (
            isinstance(draft, CreatorDraft)
            and draft.status == CreatorDraftStatus.PUBLISHED
        ):
            raise CreatorPublicationLockedError(
                "Published drafts cannot be overwritten by handoff",
                details={"draft_id": existing.draft_id},
            )


def _require_final_artifact(
    artifact: CreatorArtifact,
    *,
    tenant_id: str,
    creator_id: str,
    task_id: str,
) -> None:
    if artifact.kind != ArtifactKind.FINAL_CONTENT:
        raise CreatorPublicationArtifactError(
            "Publication handoff requires FINAL_CONTENT artifact",
            details={
                "tenant_id": tenant_id,
                "creator_id": creator_id,
                "task_id": task_id,
                "artifact_kind": artifact.kind.value,
            },
        )


def _extract_final_document(artifact: CreatorArtifact) -> tuple[str, str, str, str]:
    content = artifact.content if isinstance(artifact.content, dict) else {}
    document = content.get("document") if isinstance(content.get("document"), dict) else content
    title = _normalize_title(str(document.get("title") or "")) or "未命名内容"
    raw_body = str(
        document.get("body_markdown")
        or document.get("content_markdown")
        or document.get("body")
        or ""
    )
    body = _strip_duplicate_leading_title(raw_body, title)
    if not body:
        raise CreatorPublicationArtifactError(
            "FINAL_CONTENT artifact does not contain a publishable body",
            details={"artifact_id": artifact.id},
        )
    explicit_description = str(
        document.get("summary")
        or document.get("description")
        or document.get("excerpt")
        or ""
    )
    description = _publication_description(explicit_description, body, title)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return title, body, description, digest


def _normalize_title(value: str) -> str:
    title = re.sub(r"^\s{0,3}#{1,6}\s+", "", value or "").strip()
    return title.strip("*_` ")


def _strip_duplicate_leading_title(body: str, title: str) -> str:
    lines = (
        (body or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .lstrip("\ufeff")
        .split("\n")
    )
    while lines and not lines[0].strip():
        lines.pop(0)
    normalized_title = _normalize_title(title)
    while lines and _normalize_title(lines[0]) == normalized_title:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _publication_description(explicit: str, body: str, title: str, limit: int = 50) -> str:
    candidate = _plain_text(explicit)
    if candidate and candidate != _plain_text(title):
        return candidate[:limit]
    for paragraph in re.split(r"\n\s*\n", body):
        candidate = _plain_text(paragraph)
        if candidate and candidate != _plain_text(title):
            return candidate[:limit]
    return ""


def _plain_text(value: str) -> str:
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", "", value or "")
    text = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}|>|[-*+])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()
