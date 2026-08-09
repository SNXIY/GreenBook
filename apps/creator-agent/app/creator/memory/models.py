from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.models import CreatorRunStatus, CreatorTaskStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryTier(str, Enum):
    SHORT = "SHORT"
    LONG = "LONG"
    SEMANTIC = "SEMANTIC"


class MemorySourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    DISABLED = "DISABLED"
    DEGRADED = "DEGRADED"


class MemoryAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    NOT_CONNECTED = "NOT_CONNECTED"


class MemoryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatorTaskMemory(MemoryModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    goal: str = Field(min_length=1, max_length=20_000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_scope: dict[str, Any] = Field(default_factory=dict)
    task_status: CreatorTaskStatus
    run_status: CreatorRunStatus
    task_version: int = Field(ge=1)
    run_version: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    execution_attempts: int = Field(ge=0)
    checkpoint_id: str | None = Field(default=None, max_length=128)
    pending_decision_id: str | None = Field(default=None, max_length=128)
    final_artifact_id: str | None = Field(default=None, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)


class CreatorLongTermProfile(MemoryModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    bio: str = Field(default="", max_length=2_000)
    expertise_tags: tuple[str, ...] = Field(default=(), max_length=100)
    audience_segments: tuple[str, ...] = Field(default=(), max_length=100)
    style_traits: tuple[str, ...] = Field(default=(), max_length=100)
    preferred_formats: tuple[str, ...] = Field(default=(), max_length=50)
    language: str = Field(default="zh-CN", max_length=32)
    explicit_preferences: dict[str, Any] = Field(default_factory=dict)
    inferred_preferences: dict[str, Any] = Field(default_factory=dict)
    source_system: str = Field(default="mindflow", max_length=64)
    source_revision: str | None = Field(default=None, max_length=128)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reject_sensitive_preference_fields(self) -> "CreatorLongTermProfile":
        sensitive = _sensitive_keys(
            {
                "explicit_preferences": self.explicit_preferences,
                "inferred_preferences": self.inferred_preferences,
            }
        )
        if sensitive:
            raise ValueError(
                "Creator profile preferences cannot contain credential or "
                f"contact fields: {sorted(sensitive)}"
            )
        return self


class CreatorEngagementMetrics(MemoryModel):
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    favorites: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    heat_score: float = Field(default=0.0, ge=0.0)


class CreatorHistoricalPost(MemoryModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    post_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=500_000)
    description: str = Field(default="", max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    content_type: str = Field(default="image_text", max_length=64)
    visibility: str = Field(default="public", max_length=32)
    status: str = Field(default="published", max_length=32)
    published_at: datetime | None = None
    metrics: CreatorEngagementMetrics = Field(default_factory=CreatorEngagementMetrics)
    source_system: str = Field(default="zhiguang", max_length=64)
    source_revision: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_searchable_text(self) -> "CreatorHistoricalPost":
        if not (self.body.strip() or self.description.strip()):
            raise ValueError("Historical post requires body or description")
        return self


class CreatorSemanticMemoryHit(MemoryModel):
    post_id: str
    chunk_id: str
    chunk_index: int = Field(ge=0)
    title: str
    excerpt: str
    tags: tuple[str, ...] = ()
    content_type: str
    visibility: str
    published_at: datetime | None = None
    metrics: CreatorEngagementMetrics = Field(default_factory=CreatorEngagementMetrics)
    semantic_score: float = Field(ge=0.0, le=1.0)
    source_system: str


class CreatorMemorySourceReport(MemoryModel):
    tier: MemoryTier
    status: MemorySourceStatus
    backend: str = Field(min_length=1, max_length=128)
    record_count: int = Field(default=0, ge=0)
    detail: str = Field(default="", max_length=500)


class CreatorMemoryQuery(MemoryModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=20_000)
    source_scope: dict[str, Any] = Field(default_factory=dict)
    include_task_state: bool = True
    include_profile: bool = True
    include_semantic: bool = True
    semantic_top_k: int = Field(default=6, ge=1, le=50)


class CreatorMemoryBundle(MemoryModel):
    task_state: CreatorTaskMemory | None = None
    profile: CreatorLongTermProfile | None = None
    semantic_hits: tuple[CreatorSemanticMemoryHit, ...] = ()
    source_reports: tuple[CreatorMemorySourceReport, ...]
    profile_availability: MemoryAvailability
    history_availability: MemoryAvailability
    overall_availability: MemoryAvailability
    limitations: tuple[str, ...] = ()
    generated_at: datetime = Field(default_factory=utc_now)


_SENSITIVE_KEYS = {
    "address",
    "api_key",
    "credential",
    "email",
    "id_card",
    "password",
    "phone",
    "secret",
    "token",
}


def _sensitive_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _SENSITIVE_KEYS:
                found.add(key)
            found.update(_sensitive_keys(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.update(_sensitive_keys(nested))
    return found
