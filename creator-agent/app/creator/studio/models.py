from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StudioModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatorProjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class CreatorMaterialKind(str, Enum):
    NOTE = "NOTE"
    FILE = "FILE"
    LINK = "LINK"


class CreatorMaterialStatus(str, Enum):
    READY = "READY"
    FAILED = "FAILED"


class CreatorSuggestionKind(str, Enum):
    REWRITE = "REWRITE"
    SHORTEN = "SHORTEN"
    EXPAND = "EXPAND"
    CUSTOM = "CUSTOM"


class CreatorSuggestionStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    STALE = "STALE"


class CreatorFeedbackKind(str, Enum):
    SUGGESTION_ACCEPTED = "SUGGESTION_ACCEPTED"
    SUGGESTION_REJECTED = "SUGGESTION_REJECTED"
    MANUAL_EDIT = "MANUAL_EDIT"
    BRANCH_CREATED = "BRANCH_CREATED"
    CHANNEL_VARIANT_CREATED = "CHANNEL_VARIANT_CREATED"
    RATING = "RATING"


class CreatorDeliveryChannel(str, Enum):
    ARTICLE = "ARTICLE"
    COMMUNITY_POST = "COMMUNITY_POST"
    THREAD = "THREAD"
    NEWSLETTER = "NEWSLETTER"


class CreatorDeliveryStatus(str, Enum):
    READY = "READY"
    COPIED = "COPIED"
    EXPORTED = "EXPORTED"


class CreatorProject(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    name: str
    description: str = ""
    status: CreatorProjectStatus
    task_count: int = 0
    material_count: int = 0
    created_at: datetime
    updated_at: datetime


class CreatorMaterial(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    project_id: str | None = None
    title: str
    kind: CreatorMaterialKind
    source_url: str | None = None
    content_text: str
    content_sha256: str
    status: CreatorMaterialStatus
    chunk_count: int
    tags: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime


class SuggestionProposalDocument(StudioModel):
    replacement_text: str = Field(min_length=1, max_length=120_000)
    rationale: str = Field(min_length=1, max_length=2_000)
    evidence_ids: tuple[str, ...] = ()
    risk_note: str = Field(default="", max_length=1_000)


class ChannelVariantDocument(StudioModel):
    title: str = Field(min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    adaptation_note: str = Field(min_length=1, max_length=2_000)


class CreatorSuggestion(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    task_id: str
    draft_id: str
    base_version: int
    kind: CreatorSuggestionKind
    instruction: str
    original_text: str
    replacement_text: str
    prefix_context: str = ""
    suffix_context: str = ""
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    risk_note: str = ""
    status: CreatorSuggestionStatus
    model_provider: str
    model_name: str
    applied_version: int | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class CreatorBranch(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    source_draft_id: str
    source_version: int
    draft_id: str
    name: str
    created_at: datetime


class CreatorChannelVariant(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    task_id: str
    draft_id: str
    draft_version: int
    channel: CreatorDeliveryChannel
    title: str
    content_markdown: str
    adaptation_note: str
    status: CreatorDeliveryStatus
    model_provider: str
    model_name: str
    created_at: datetime
    updated_at: datetime


class CreatorFeedback(StudioModel):
    id: str
    tenant_id: str
    creator_id: str
    task_id: str | None = None
    draft_id: str | None = None
    suggestion_id: str | None = None
    kind: CreatorFeedbackKind
    score: float | None = None
    reason: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: datetime


class CreatorFeedbackSummary(StudioModel):
    accepted_suggestions: int = 0
    rejected_suggestions: int = 0
    manual_edits: int = 0
    acceptance_rate: float | None = None
    average_rating: float | None = None
    total_events: int = 0

