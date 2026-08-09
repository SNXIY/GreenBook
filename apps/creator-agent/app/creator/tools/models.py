from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.drafts.models import CreatorDraftStatus
from app.creator.memory.models import CreatorEngagementMetrics
from app.creator.providers.models import (
    CommunityComment,
    CommunityCommentSort,
    CommunityEngagementPoint,
)
from app.creator.retrieval.models import RetrievalIntent


class ToolModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatorToolRisk(str, Enum):
    READ = "READ"
    DRAFT_WRITE = "DRAFT_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"


class CreatorToolCallStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class CreatorToolResultStatus(str, Enum):
    SUCCESS = "SUCCESS"


class CreatorToolPrincipal(ToolModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    caller: str = Field(min_length=1, max_length=128)
    roles: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()

    def has_role(self, *roles: str) -> bool:
        normalized = {role.strip().upper() for role in self.roles}
        return bool(normalized.intersection(role.upper() for role in roles))


class CreatorToolCallContext(ToolModel):
    principal: CreatorToolPrincipal
    trace_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    remaining_call_budget: int = Field(default=1, ge=0)


class CreatorToolProvenance(ToolModel):
    source: str = Field(min_length=1, max_length=128)
    reference: str | None = Field(default=None, max_length=2_000)
    revision: str | None = Field(default=None, max_length=128)
    authority_verified: bool = True


class CreatorToolErrorData(ToolModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False


ToolDataT = TypeVar("ToolDataT", bound=BaseModel)


class CreatorToolResult(ToolModel, Generic[ToolDataT]):
    call_id: str = Field(min_length=1, max_length=64)
    status: CreatorToolResultStatus = CreatorToolResultStatus.SUCCESS
    data: ToolDataT
    error: CreatorToolErrorData | None = None
    provenance: tuple[CreatorToolProvenance, ...] = ()
    degraded_services: tuple[str, ...] = ()
    trace_id: str = Field(min_length=1, max_length=128)


class CreatorToolCallAudit(ToolModel):
    call_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    caller: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    risk: CreatorToolRisk
    arguments_sha256: str = Field(min_length=64, max_length=64)
    status: CreatorToolCallStatus
    started_at: datetime
    finished_at: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    result_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    result_size_bytes: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128)


class GetCreatorProfileInput(ToolModel):
    pass


class CreatorProfileToolData(ToolModel):
    creator_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    bio: str = Field(default="", max_length=2_000)
    expertise_tags: tuple[str, ...] = Field(default=(), max_length=100)
    audience_segments: tuple[str, ...] = Field(default=(), max_length=100)
    style_traits: tuple[str, ...] = Field(default=(), max_length=100)
    preferred_formats: tuple[str, ...] = Field(default=(), max_length=50)
    language: str = Field(default="zh-CN", max_length=32)
    follower_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)
    published_post_count: int = Field(default=0, ge=0)
    memory_version: int | None = Field(default=None, ge=1)
    availability: str = Field(default="EMPTY", max_length=32)


class PostReference(ToolModel):
    post_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    creator_name: str = Field(default="", max_length=128)
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    content_type: str = Field(default="image_text", max_length=64)
    visibility: str = Field(default="public", max_length=32)
    status: str = Field(default="published", max_length=32)
    source_url: str | None = Field(default=None, max_length=2_000)
    published_at: datetime | None = None
    metrics: CreatorEngagementMetrics = Field(default_factory=CreatorEngagementMetrics)


class GetUserHistoryInput(ToolModel):
    cursor: str | None = Field(default=None, max_length=2_000)
    limit: int = Field(default=20, ge=1, le=50)
    statuses: tuple[str, ...] = Field(
        default=("published",),
        min_length=1,
        max_length=5,
    )


class UserHistoryToolData(ToolModel):
    items: tuple[PostReference, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=2_000)
    has_more: bool = False


class SearchPostsInput(ToolModel):
    queries: tuple[str, ...] = Field(min_length=1, max_length=3)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    creator_ids: tuple[str, ...] = Field(default=(), max_length=20)
    content_types: tuple[str, ...] = Field(default=(), max_length=10)
    published_after: datetime | None = None
    published_before: datetime | None = None
    intent: RetrievalIntent = RetrievalIntent.TOPIC_RESEARCH
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_search(self) -> "SearchPostsInput":
        if any(not query.strip() or len(query) > 500 for query in self.queries):
            raise ValueError("Search queries must contain 1-500 characters")
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after >= self.published_before
        ):
            raise ValueError("published_after must be earlier than published_before")
        return self


class SearchPostItem(PostReference):
    excerpt: str = Field(default="", max_length=4_000)
    score: float = Field(ge=0.0, le=1.0)
    channels: tuple[str, ...] = Field(default=(), max_length=5)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    authority_verified: bool = True


class SearchPostsToolData(ToolModel):
    items: tuple[SearchPostItem, ...] = ()
    availability: str = Field(min_length=1, max_length=32)
    limitations: tuple[str, ...] = ()


class GetPostDetailInput(ToolModel):
    post_id: str = Field(min_length=1, max_length=128)


class PostDetailToolData(PostReference):
    body: str = Field(default="", max_length=500_000)


class GetCommentsInput(ToolModel):
    post_id: str = Field(min_length=1, max_length=128)
    cursor: str | None = Field(default=None, max_length=2_000)
    limit: int = Field(default=20, ge=1, le=50)
    parent_id: str | None = Field(default=None, max_length=128)
    sort: CommunityCommentSort = CommunityCommentSort.RECENT


class CommentsToolData(ToolModel):
    items: tuple[CommunityComment, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=2_000)
    has_more: bool = False


class GetPostMetricsInput(ToolModel):
    post_id: str = Field(min_length=1, max_length=128)


class PostMetricsToolData(ToolModel):
    post_id: str = Field(min_length=1, max_length=128)
    metrics: CreatorEngagementMetrics
    like_rate: float = Field(ge=0.0)
    favorite_rate: float = Field(ge=0.0)
    comment_rate: float = Field(ge=0.0)
    collected_at: datetime


class GetEngagementInput(ToolModel):
    post_ids: tuple[str, ...] = Field(default=(), max_length=20)
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "GetEngagementInput":
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self


class EngagementToolData(ToolModel):
    creator_id: str = Field(min_length=1, max_length=128)
    post_ids: tuple[str, ...] = Field(default=(), max_length=50)
    aggregate: CreatorEngagementMetrics
    time_series: tuple[CommunityEngagementPoint, ...] = Field(
        default=(),
        max_length=366,
    )
    generated_at: datetime


class SaveDraftInput(ToolModel):
    task_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


class UpdateDraftInput(ToolModel):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    content_markdown: str = Field(min_length=1, max_length=500_000)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


class DraftToolData(ToolModel):
    draft_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    status: CreatorDraftStatus
    current_version: int = Field(ge=1)
    version: int = Field(ge=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    source_artifact_id: str | None = Field(default=None, max_length=128)
    replayed: bool = False
    updated_at: datetime


class ToolHandlerResult(ToolModel):
    data: BaseModel
    provenance: tuple[CreatorToolProvenance, ...] = ()
    degraded_services: tuple[str, ...] = ()


class ToolServerInfo(ToolModel):
    name: str
    version: str
    tools: tuple[str, ...]
    publication_enabled: bool = False
    provider: str
    transport: str
    metadata: dict[str, Any] = Field(default_factory=dict)
