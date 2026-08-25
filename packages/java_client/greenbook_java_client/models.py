"""Pydantic models matching the Java Agent Facade OpenAPI contract.

Single source of truth: contracts/java-openapi.yaml
SHA256: 1409b6d825a11dc161b501668ac09e07349a38b0690f060396ac77c60668eeef
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Matches know_posts.description and the Java Agent Facade DTO contract.
DESCRIPTION_MAX_LENGTH = 50

# ── Enums ────────────────────────────────────────────────────────────

class SortMode(StrEnum):
    hot = "hot"
    latest = "latest"
    relevant = "relevant"


class Visibility(StrEnum):
    public = "public"
    followers = "followers"
    school = "school"
    private = "private"
    unlisted = "unlisted"


class ContentOrigin(StrEnum):
    MANUAL = "MANUAL"
    AI_ASSISTED = "AI_ASSISTED"


class ScheduleStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    FIELD_TOO_LONG = "FIELD_TOO_LONG"
    INVALID_DRAFT_METADATA = "INVALID_DRAFT_METADATA"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DRAFT_VERSION_CONFLICT = "DRAFT_VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BUSINESS_REJECTED = "BUSINESS_REJECTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ── Search ───────────────────────────────────────────────────────────

class SearchPostItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str = Field(alias="postId")
    author_id: str | None = Field(default=None, alias="authorId")
    title: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    like_count: int = Field(default=0, alias="likeCount")
    comment_count: int = Field(default=0, alias="commentCount")
    favorite_count: int = Field(default=0, alias="favoriteCount")
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    hot_score: float | None = Field(default=None, alias="hotScore")


class SearchPageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SearchPostItem] = Field(default_factory=list)
    page: int = 1
    size: int = 20
    total: int = 0
    total_pages: int = Field(default=0, alias="totalPages")
    has_more: bool = Field(default=False, alias="hasMore")
    sort: str | None = None
    provider: str | None = None
    degraded: bool = False


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(alias="chunkId")
    post_id: str = Field(alias="postId")
    title: str | None = None
    content: str
    score: float
    start_offset: int = Field(alias="startOffset")
    end_offset: int = Field(alias="endOffset")
    event_version: int = Field(default=0, alias="eventVersion")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class KnowledgeEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunks: list[EvidenceChunk] = Field(default_factory=list)
    candidate_post_count: int = Field(default=0, alias="candidatePostCount")
    embedding_latency_ms: int = Field(default=0, alias="embeddingLatencyMs")
    chunk_retrieval_latency_ms: int = Field(default=0, alias="chunkRetrievalLatencyMs")
    degraded: bool = False


# ── Post ─────────────────────────────────────────────────────────────

class AgentPostContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str | None = Field(default=None, alias="postId")
    title: str | None = None
    description: str | None = None
    body: str | None = None
    tags: list[str] = Field(default_factory=list)
    author_id: str | None = Field(default=None, alias="authorId")
    author_nickname: str | None = Field(default=None, alias="authorNickname")
    publish_time: datetime | None = Field(default=None, alias="publishTime")
    content_origin: str | None = Field(default=None, alias="contentOrigin")


# ── Own Posts ────────────────────────────────────────────────────────

class AgentOwnPostSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str = Field(alias="postId")
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    visible: str | None = None
    content_origin: str | None = Field(default=None, alias="contentOrigin")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    published_at: datetime | None = Field(default=None, alias="publishedAt")


# ── Draft ────────────────────────────────────────────────────────────

class AgentDraftCreateRequest(BaseModel):
    title: str = Field(max_length=256)
    content: str
    summary: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    visibility: Visibility | None = None


class AgentDraftUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    summary: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    tags: list[str] | None = None
    visibility: str | None = None
    expected_version: str | None = Field(
        default=None,
        alias="expectedVersion",
        description="ISO-8601 instant from DraftResponse.updatedAt for optimistic locking",
    )


class DraftResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft_id: str = Field(alias="draftId")
    owner_id: str | None = Field(default=None, alias="ownerId")
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    visibility: str | None = None
    version: int | None = None
    status: str | None = None
    content_origin: str | None = Field(default=None, alias="contentOrigin")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


# ── Schedule ─────────────────────────────────────────────────────────

class ScheduleCreateRequest(BaseModel):
    draft_id: str = Field(alias="draftId")
    run_at: str = Field(alias="runAt", description="ISO-8601 datetime with timezone")
    timezone: str = "Asia/Shanghai"


class ScheduleUpdateRequest(BaseModel):
    run_at: str = Field(alias="runAt", description="ISO-8601 datetime with timezone")
    version: int = Field(description="Current version for optimistic locking")


class ScheduledPublicationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schedule_id: str = Field(alias="scheduleId")
    draft_id: str | None = Field(default=None, alias="draftId")
    run_at: datetime | None = Field(default=None, alias="runAt")
    timezone: str | None = None
    status: str | None = None
    version: int | None = None
    published_post_id: str | None = Field(default=None, alias="publishedPostId")
    failure_code: str | None = Field(default=None, alias="failureCode")
    failure_message: str | None = Field(default=None, alias="failureMessage")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


# ── Publish ──────────────────────────────────────────────────────────

class PublishNowRequest(BaseModel):
    draft_id: str = Field(alias="draftId")


class PublishResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str | None = Field(default=None, alias="postId")
    status: str | None = None
    already_published: bool = Field(default=False, alias="alreadyPublished")
    published_at: datetime | None = Field(default=None, alias="publishedAt")


# ── Comments ─────────────────────────────────────────────────────────

class AgentCommentResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    post_id: str = Field(alias="postId")
    parent_id: str | None = Field(default=None, alias="parentId")
    root_id: str | None = Field(default=None, alias="rootId")
    user_id: str | None = Field(default=None, alias="userId")
    author_nickname: str | None = Field(default=None, alias="authorNickname")
    author_avatar: str | None = Field(default=None, alias="authorAvatar")
    content: str | None = None
    is_top: bool = Field(default=False, alias="isTop")
    reply_count: int = Field(default=0, alias="replyCount")
    like_count: int = Field(default=0, alias="likeCount")
    assistant: bool = False
    created_at: datetime | None = Field(default=None, alias="createdAt")


class AgentCommentPageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[AgentCommentResponse] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    has_more: bool = Field(default=False, alias="hasMore")


class AgentCommentReplyRequest(BaseModel):
    post_id: str = Field(alias="postId")
    parent_comment_id: str = Field(alias="parentCommentId")
    content: str


# ── Analytics ────────────────────────────────────────────────────────

class PostAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str = Field(alias="postId")
    like_count: int = Field(default=0, alias="likeCount")
    comment_count: int = Field(default=0, alias="commentCount")
    favorite_count: int = Field(default=0, alias="favoriteCount")
    share_count: int = Field(default=0, alias="shareCount")
    view_count: int = Field(default=0, alias="viewCount")


class UserAnalyticsSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_published: int = Field(default=0, alias="totalPublished")
    total_likes_received: int = Field(default=0, alias="totalLikesReceived")
    total_comments: int = Field(default=0, alias="totalComments")
    total_favorites: int = Field(default=0, alias="totalFavorites")
    follower_count: int = Field(default=0, alias="followerCount")
    following_count: int = Field(default=0, alias="followingCount")


# ── Error ────────────────────────────────────────────────────────────

class AgentErrorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str | None = None
    user_message: str | None = Field(default=None, alias="userMessage")
    retryable: bool = False
    request_committed: bool = Field(default=False, alias="requestCommitted")
    trace_id: str | None = Field(default=None, alias="traceId")
    field: str | None = None
    max_length: int | None = Field(default=None, alias="maxLength")
    actual_length: int | None = Field(default=None, alias="actualLength")
    execution_id: str | None = Field(default=None, alias="executionId")
