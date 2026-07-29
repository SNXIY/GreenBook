from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.memory.models import CreatorEngagementMetrics


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CommunityModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CommunityAccessScope(CommunityModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    roles: frozenset[str] = frozenset()
    trace_id: str = Field(min_length=1, max_length=128)

    def has_role(self, *roles: str) -> bool:
        normalized = {role.strip().upper() for role in self.roles}
        return bool(normalized.intersection(role.upper() for role in roles))


class CommunityCreatorProfile(CommunityModel):
    creator_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    bio: str = Field(default="", max_length=2_000)
    expertise_tags: tuple[str, ...] = Field(default=(), max_length=100)
    follower_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)
    published_post_count: int = Field(default=0, ge=0)
    source_system: str = Field(default="zhiguang", max_length=64)
    source_revision: str | None = Field(default=None, max_length=128)


class CommunityPost(CommunityModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    post_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    creator_name: str = Field(default="", max_length=128)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=500_000)
    description: str = Field(default="", max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    content_type: str = Field(default="image_text", max_length=64)
    visibility: str = Field(default="public", max_length=32)
    status: str = Field(default="published", max_length=32)
    source_url: str | None = Field(default=None, max_length=2_000)
    published_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    metrics: CreatorEngagementMetrics = Field(default_factory=CreatorEngagementMetrics)
    source_system: str = Field(default="zhiguang", max_length=64)
    source_revision: str | None = Field(default=None, max_length=128)

    @property
    def is_public_and_published(self) -> bool:
        return (
            self.visibility.strip().lower() == "public"
            and self.status.strip().lower() == "published"
        )


class CommunityPostPage(CommunityModel):
    items: tuple[CommunityPost, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=2_000)
    has_more: bool = False


class CommunityComment(CommunityModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    comment_id: str = Field(min_length=1, max_length=128)
    post_id: str = Field(min_length=1, max_length=128)
    author_id: str = Field(min_length=1, max_length=128)
    author_name: str = Field(default="", max_length=128)
    content: str = Field(min_length=1, max_length=20_000)
    parent_id: str | None = Field(default=None, max_length=128)
    root_id: str | None = Field(default=None, max_length=128)
    reply_count: int = Field(default=0, ge=0)
    like_count: int = Field(default=0, ge=0)
    is_top: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class CommunityCommentSort(str, Enum):
    RECENT = "RECENT"
    HOT = "HOT"


class CommunityCommentPage(CommunityModel):
    items: tuple[CommunityComment, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=2_000)
    has_more: bool = False


class CommunitySearchRequest(CommunityModel):
    queries: tuple[str, ...] = Field(min_length=1, max_length=3)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    creator_ids: tuple[str, ...] = Field(default=(), max_length=20)
    content_types: tuple[str, ...] = Field(default=(), max_length=10)
    published_after: datetime | None = None
    published_before: datetime | None = None
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def validate_request(self) -> "CommunitySearchRequest":
        if any(not query.strip() or len(query) > 500 for query in self.queries):
            raise ValueError("Search queries must contain 1-500 characters")
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after >= self.published_before
        ):
            raise ValueError("published_after must be earlier than published_before")
        return self


class CommunitySearchCandidate(CommunityModel):
    post: CommunityPost
    score: float = Field(ge=0.0)
    rank: int = Field(ge=1)
    channel: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommunitySearchResult(CommunityModel):
    candidates: tuple[CommunitySearchCandidate, ...] = ()
    degraded_services: tuple[str, ...] = ()


class CommunityPostMetrics(CommunityModel):
    post_id: str = Field(min_length=1, max_length=128)
    metrics: CreatorEngagementMetrics
    like_rate: float = Field(default=0.0, ge=0.0)
    favorite_rate: float = Field(default=0.0, ge=0.0)
    comment_rate: float = Field(default=0.0, ge=0.0)
    collected_at: datetime = Field(default_factory=utc_now)
    source_system: str = Field(default="zhiguang", max_length=64)


class CommunityEngagementPoint(CommunityModel):
    period_start: datetime
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    favorites: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)


class CommunityEngagementReport(CommunityModel):
    creator_id: str = Field(min_length=1, max_length=128)
    post_ids: tuple[str, ...] = Field(default=(), max_length=50)
    aggregate: CreatorEngagementMetrics
    time_series: tuple[CommunityEngagementPoint, ...] = Field(
        default=(),
        max_length=366,
    )
    generated_at: datetime = Field(default_factory=utc_now)
    source_system: str = Field(default="zhiguang", max_length=64)
