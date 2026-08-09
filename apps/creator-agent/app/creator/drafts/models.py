from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class DraftModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatorDraftStatus(str, Enum):
    DRAFT = "DRAFT"
    ARCHIVED = "ARCHIVED"
    PUBLISHED = "PUBLISHED"


class CreatorDraft(DraftModel):
    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    current_version: int = Field(ge=1)
    status: CreatorDraftStatus = CreatorDraftStatus.DRAFT
    external_draft_id: str | None = Field(default=None, max_length=128)
    created_at: datetime
    updated_at: datetime


class CreatorDraftVersion(DraftModel):
    draft_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    content_sha256: str = Field(min_length=64, max_length=64)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    editor_type: str = Field(min_length=1, max_length=32)
    actor_id: str = Field(min_length=1, max_length=128)
    created_at: datetime


class CreatorDraftWriteResult(DraftModel):
    draft: CreatorDraft
    version: CreatorDraftVersion
    replayed: bool = False


class CreateDraftRecord(DraftModel):
    draft_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    content_sha256: str = Field(min_length=64, max_length=64)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    editor_type: str = Field(min_length=1, max_length=32)
    actor_id: str = Field(min_length=1, max_length=128)
    idempotency_scope: str = Field(min_length=1, max_length=256)
    idempotency_key_hash: str = Field(min_length=64, max_length=64)
    request_hash: str = Field(min_length=64, max_length=64)
    now: datetime


class UpdateDraftRecord(DraftModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    draft_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    content_sha256: str = Field(min_length=64, max_length=64)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    editor_type: str = Field(min_length=1, max_length=32)
    actor_id: str = Field(min_length=1, max_length=128)
    idempotency_scope: str = Field(min_length=1, max_length=256)
    idempotency_key_hash: str = Field(min_length=64, max_length=64)
    request_hash: str = Field(min_length=64, max_length=64)
    now: datetime
