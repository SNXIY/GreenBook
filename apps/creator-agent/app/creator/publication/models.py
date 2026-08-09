"""Publication handoff models aligned with Zhiguang AI_ASSISTED provenance."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PublicationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContentOrigin(str, Enum):
    AI_GENERATED = "AI_GENERATED"
    AI_ASSISTED = "AI_ASSISTED"
    USER_AUTHORED = "USER_AUTHORED"


class PublicationHandoffStatus(str, Enum):
    READY = "READY"
    LOCKED = "LOCKED"


class PublicationHandoff(PublicationModel):
    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    content_origin: ContentOrigin
    source_artifact_id: str = Field(min_length=1, max_length=128)
    source_artifact_revision: int = Field(ge=1)
    source_content_sha256: str = Field(min_length=64, max_length=64)
    external_draft_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    status: PublicationHandoffStatus = PublicationHandoffStatus.READY
    created_at: datetime


class PublicationHandoffResult(PublicationModel):
    handoff: PublicationHandoff
    replayed: bool = False
