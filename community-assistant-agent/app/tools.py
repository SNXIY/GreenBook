from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.untrusted_content import guard_post_payload, inspect_untrusted_text
from app.artifact_contracts import ArtifactBinding, ArtifactKind


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class McpArguments(ToolArguments):
    model_config = ConfigDict(extra="allow")


class McpOutput(ToolOutput):
    server: str
    tool: str
    structured_content: Any = None
    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = False


class SearchPostsArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=10)


class PostArguments(ToolArguments):
    post_id: str | None = Field(default=None, max_length=64)
    focus: str | None = Field(default=None, max_length=500)


class CreateDraftArguments(ToolArguments):
    instruction: str = Field(min_length=1, max_length=10_000)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class ReviseDraftArguments(CreateDraftArguments):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class OwnDraftArguments(ToolArguments):
    draft_id: str = Field(min_length=1, max_length=64)


class ScheduleArguments(ToolArguments):
    run_at: str | None = Field(default=None, min_length=10, max_length=64)
    delay_seconds: int | None = Field(default=None, ge=15, le=518_400)
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_schedule_time(self) -> "ScheduleArguments":
        if (self.run_at is None) == (self.delay_seconds is None):
            raise ValueError("run_at 与 delay_seconds 必须且只能提供一个")
        return self


class ScheduleLookupArguments(ToolArguments):
    action_id: str = Field(min_length=1, max_length=64)


class ScheduleUpdateArguments(ToolArguments):
    action_id: str = Field(min_length=1, max_length=64)
    run_at: str | None = Field(default=None, min_length=10, max_length=64)
    delay_seconds: int | None = Field(default=None, ge=15, le=518_400)
    draft_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_content_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{64}$"
    )

    @model_validator(mode="after")
    def validate_update(self) -> "ScheduleUpdateArguments":
        if self.run_at is not None and self.delay_seconds is not None:
            raise ValueError("run_at 与 delay_seconds 不能同时提供")
        if (self.draft_id is None) != (self.expected_content_sha256 is None):
            raise ValueError("draft_id 与 expected_content_sha256 必须同时提供")
        if self.run_at is None and self.delay_seconds is None and self.draft_id is None:
            raise ValueError("至少需要修改发布时间或草稿版本")
        return self


class ScheduleBatchItem(ToolArguments):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ScheduleBatchArguments(ToolArguments):
    run_at: str = Field(min_length=10, max_length=64)
    interval_minutes: int = Field(default=30, ge=1, le=1_440)
    items: list[ScheduleBatchItem] = Field(default_factory=list, min_length=2, max_length=10)


class PublishArguments(ToolArguments):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplyCommentArguments(ToolArguments):
    post_id: str = Field(min_length=1, max_length=64)
    parent_comment_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=2_000)


class AnalyzeEngagementArguments(ToolArguments):
    topic: str | None = Field(default=None, max_length=100)
    days: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=20)


class ListActiveUsersArguments(ToolArguments):
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=20)


class UserScopedAnalyticsArguments(ToolArguments):
    user_ids: list[str] = Field(min_length=1, max_length=20)
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=10, ge=1, le=20)


class DeletePostArguments(ToolArguments):
    post_id: str = Field(min_length=1, max_length=64)


class ListOwnPostsArguments(ToolArguments):
    max_items: int = Field(default=1_000, ge=1, le=1_000)


class DeleteOwnPostsBatchArguments(ToolArguments):
    post_ids: list[str] = Field(min_length=1, max_length=1_000)


class SearchPostItem(ToolOutput):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    creator_id: str = Field(
        validation_alias=AliasChoices("creator_id", "creatorId", "authorId"),
        min_length=1,
        max_length=64,
    )
    author_nickname: str | None = Field(
        default=None,
        validation_alias=AliasChoices("author_nickname", "authorNickname"),
        max_length=128,
    )
    publish_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("publish_time", "publishTime"),
    )
    untrusted_content: bool = True
    injection_signals: list[str] = Field(default_factory=list, max_length=10)


class SearchPostsOutput(ToolOutput):
    query: str = Field(min_length=1, max_length=200)
    results: list[SearchPostItem] = Field(default_factory=list, max_length=10)
    truncated: bool = False
    search_complete: bool = True
    stop_reason: str | None = Field(default=None, max_length=64)


class PostContextOutput(ToolOutput):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1_000)
    body_markdown: str = Field(
        validation_alias=AliasChoices("body_markdown", "bodyMarkdown"),
        max_length=524_288,
    )
    tags: list[str] = Field(default_factory=list, max_length=30)
    creator_id: str = Field(
        validation_alias=AliasChoices("creator_id", "creatorId", "authorId"),
        min_length=1,
        max_length=64,
    )
    author_nickname: str | None = Field(
        default=None,
        validation_alias=AliasChoices("author_nickname", "authorNickname"),
        max_length=128,
    )
    publish_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("publish_time", "publishTime"),
    )
    content_origin: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_origin", "contentOrigin"),
        max_length=32,
    )
    content_sha256: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_sha256", "contentSha256"),
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    untrusted_content: bool = True
    injection_signals: list[str] = Field(default_factory=list, max_length=10)


class SummaryOutput(ToolOutput):
    post_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=256)
    summary: str = Field(min_length=1, max_length=10_000)
    source_content_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{64}$"
    )


class DraftOutput(ToolOutput):
    task_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=256)
    handoff_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    description: str | None = Field(default=None, max_length=2_000)
    body_markdown: str | None = Field(default=None, max_length=524_288)
    supersedes_draft_id: str | None = Field(default=None, max_length=64)


class OwnedDraftOutput(ToolOutput):
    draft_id: str = Field(
        validation_alias=AliasChoices("draft_id", "draftId", "id"),
        min_length=1,
        max_length=64,
    )
    title: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=1_000)
    body_markdown: str | None = Field(default=None, max_length=524_288)
    tags: list[str] = Field(default_factory=list, max_length=20)
    status: str = Field(pattern=r"^READY$")
    content_sha256: str = Field(
        validation_alias=AliasChoices("content_sha256", "contentSha256"),
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    untrusted_content: bool = True
    injection_signals: list[str] = Field(default_factory=list, max_length=10)


class ScheduleOutput(ToolOutput):
    action_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    run_at: datetime
    status: str = Field(
        pattern=r"^(SCHEDULED|RETRYING|RUNNING|COMPLETED|CANCELLED|FAILED)$"
    )


class ScheduleCancelledOutput(ToolOutput):
    action_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    run_at: datetime
    status: str = Field(pattern=r"^CANCELLED$")


class ScheduleBatchOutput(ToolOutput):
    status: str = Field(pattern=r"^SCHEDULED$")
    actions: list[ScheduleOutput] = Field(min_length=2, max_length=10)


class PublishOutput(ToolOutput):
    post_id: str = Field(
        validation_alias=AliasChoices("post_id", "postId", "id"),
        min_length=1,
        max_length=64,
    )
    status: str = Field(pattern=r"^published$")
    replayed: bool


class ReplyCommentOutput(ToolOutput):
    id: str = Field(min_length=1, max_length=64)
    post_id: str = Field(validation_alias=AliasChoices("post_id", "postId"))
    parent_id: str = Field(validation_alias=AliasChoices("parent_id", "parentId"))
    root_id: str | None = Field(
        default=None, validation_alias=AliasChoices("root_id", "rootId")
    )
    user_id: str = Field(validation_alias=AliasChoices("user_id", "userId"))
    author_nickname: str = Field(
        validation_alias=AliasChoices("author_nickname", "authorNickname")
    )
    author_avatar: str | None = Field(
        default=None, validation_alias=AliasChoices("author_avatar", "authorAvatar")
    )
    content: str = Field(min_length=1, max_length=1_000)
    top: bool
    reply_count: int = Field(
        validation_alias=AliasChoices("reply_count", "replyCount"), ge=0
    )
    like_count: int = Field(
        validation_alias=AliasChoices("like_count", "likeCount"), ge=0
    )
    liked: bool
    assistant: bool
    assistant_run_id: str = Field(
        validation_alias=AliasChoices("assistant_run_id", "assistantRunId")
    )
    create_time: datetime = Field(
        validation_alias=AliasChoices("create_time", "createTime")
    )


class EngagementPost(ToolOutput):
    id: str
    title: str
    description: str | None = None
    author_id: str = Field(validation_alias=AliasChoices("author_id", "authorId"))
    author_nickname: str | None = Field(
        default=None,
        validation_alias=AliasChoices("author_nickname", "authorNickname"),
    )
    publish_time: datetime | None = Field(
        default=None, validation_alias=AliasChoices("publish_time", "publishTime")
    )
    comment_count: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("comment_count", "commentCount")
    )


class ContributorInsight(ToolOutput):
    user_id: str = Field(validation_alias=AliasChoices("user_id", "userId"))
    nickname: str | None = None
    comment_count: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("comment_count", "commentCount")
    )


class EngagementAnalyticsOutput(ToolOutput):
    topic: str | None = None
    period_start: datetime = Field(
        validation_alias=AliasChoices("period_start", "periodStart")
    )
    period_end: datetime = Field(
        validation_alias=AliasChoices("period_end", "periodEnd")
    )
    published_post_count: int = Field(
        ge=0, validation_alias=AliasChoices("published_post_count", "publishedPostCount")
    )
    comment_count: int = Field(
        ge=0, validation_alias=AliasChoices("comment_count", "commentCount")
    )
    active_creator_count: int = Field(
        ge=0, validation_alias=AliasChoices("active_creator_count", "activeCreatorCount")
    )
    interacting_user_count: int = Field(
        ge=0,
        validation_alias=AliasChoices(
            "interacting_user_count", "interactingUserCount"
        ),
    )
    top_posts: list[EngagementPost] = Field(
        default_factory=list, validation_alias=AliasChoices("top_posts", "topPosts")
    )
    top_contributors: list[ContributorInsight] = Field(
        default_factory=list,
        validation_alias=AliasChoices("top_contributors", "topContributors"),
    )
    available_signals: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("available_signals", "availableSignals"),
    )
    limitations: list[str] = Field(default_factory=list)


class ActiveUserInsight(ToolOutput):
    user_id: str = Field(validation_alias=AliasChoices("user_id", "userId"))
    nickname: str | None = None
    published_post_count: int = Field(
        ge=0,
        validation_alias=AliasChoices(
            "published_post_count", "publishedPostCount"
        ),
    )
    comment_count: int = Field(
        ge=0,
        validation_alias=AliasChoices("comment_count", "commentCount"),
    )
    activity_score: int = Field(
        ge=0,
        validation_alias=AliasChoices("activity_score", "activityScore"),
    )


class ActiveUsersOutput(ToolOutput):
    period_start: datetime = Field(
        validation_alias=AliasChoices("period_start", "periodStart")
    )
    period_end: datetime = Field(
        validation_alias=AliasChoices("period_end", "periodEnd")
    )
    users: list[ActiveUserInsight] = Field(default_factory=list, max_length=20)


class UserPostInsight(ToolOutput):
    post_id: str = Field(validation_alias=AliasChoices("post_id", "postId"))
    author_id: str = Field(validation_alias=AliasChoices("author_id", "authorId"))
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    type: str | None = Field(default=None, max_length=64)
    publish_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("publish_time", "publishTime"),
    )


class UserPostsOutput(ToolOutput):
    posts: list[UserPostInsight] = Field(default_factory=list, max_length=400)


class TopicInsight(ToolOutput):
    topic: str = Field(min_length=1, max_length=100)
    post_count: int = Field(
        ge=0,
        validation_alias=AliasChoices("post_count", "postCount"),
    )
    creator_count: int = Field(
        ge=0,
        validation_alias=AliasChoices("creator_count", "creatorCount"),
    )


class PostTopicsOutput(ToolOutput):
    topics: list[TopicInsight] = Field(default_factory=list, max_length=20)


class DeletePostOutput(ToolOutput):
    post_id: str
    status: str = Field(pattern=r"^deleted$")


class OwnPostItem(ToolOutput):
    id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=256)
    status: str = Field(min_length=1, max_length=32)
    visible: str | None = Field(default=None, max_length=32)
    create_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("create_time", "createTime"),
    )
    publish_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("publish_time", "publishTime"),
    )
    untrusted_content: bool = True
    injection_signals: list[str] = Field(default_factory=list, max_length=10)


class ListOwnPostsOutput(ToolOutput):
    posts: list[OwnPostItem] = Field(default_factory=list, max_length=1_000)
    count: int = Field(ge=0, le=1_000)
    truncated: bool = False


class DeleteOwnPostsBatchOutput(ToolOutput):
    post_ids: list[str] = Field(
        validation_alias=AliasChoices("post_ids", "postIds"),
        min_length=1,
        max_length=1_000,
    )
    deleted_count: int = Field(
        validation_alias=AliasChoices("deleted_count", "deletedCount"),
        ge=0,
    )
    already_deleted_count: int = Field(
        validation_alias=AliasChoices(
            "already_deleted_count", "alreadyDeletedCount"
        ),
        ge=0,
    )
    status: str = Field(pattern=r"^deleted$")


class RiskLevel(StrEnum):
    READ = "READ"
    REVERSIBLE = "REVERSIBLE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"


class ExecutionMode(StrEnum):
    INLINE = "INLINE"
    ASYNC = "ASYNC"


class TransportType(StrEnum):
    LEGACY_BUILTIN = "LEGACY_BUILTIN"
    BUILTIN = "BUILTIN"
    HTTP = "HTTP"
    MCP = "MCP"
    ASYNC_JOB = "ASYNC_JOB"


class IdempotencyMode(StrEnum):
    NONE = "NONE"
    READ_DEDUP = "READ_DEDUP"
    SIDE_EFFECT_REQUIRED = "SIDE_EFFECT_REQUIRED"


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative retry contract. Step 1 defaults preserve legacy behavior."""

    max_attempts: int = 1
    initial_backoff_ms: int = 200
    max_backoff_ms: int = 2_000
    retryable_http_statuses: tuple[int, ...] = ()
    retryable_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityBudget:
    """Logical invocation capability budget. Step 2+ applies this formally."""

    base_uses: int = 1
    max_internal_calls: int = 1


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    label: str
    description: str
    arguments_model: type[ToolArguments]
    output_model: type[ToolOutput]
    risk: RiskLevel
    timeout_seconds: int
    planner_visible: bool = True
    side_effecting: bool = False
    execution_mode: ExecutionMode = ExecutionMode.INLINE
    artifact_type: str = ArtifactKind.TOOL_RESULT
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    required_target_roles: frozenset[str] = frozenset()
    optional_target_roles: frozenset[str] = frozenset()
    argument_defaults: dict[str, Any] = field(default_factory=dict)
    prompt_argument: str | None = None
    context_arguments: dict[str, str] = field(default_factory=dict)
    requires_progress_review: bool = True
    transport: TransportType = TransportType.LEGACY_BUILTIN
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    idempotency_mode: IdempotencyMode = IdempotencyMode.NONE
    capability_budget: CapabilityBudget = field(default_factory=CapabilityBudget)

    @property
    def runtime_bound_arguments(self) -> frozenset[str]:
        return frozenset(
            binding.argument
            for binding in self.artifact_bindings
            if not binding.allow_planner_value
        )

    @property
    def runtime_argument_examples(self) -> dict[str, Any]:
        return {
            binding.argument: binding.validation_example
            for binding in self.artifact_bindings
        }


# Tool implementations are deliberately kept out of the model-facing schema.
# The registry owns the execution boundary so new tools can be added without
# changing the worker's orchestration loop.
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition] | None = None) -> None:
        definitions = list((definitions if definitions is not None else [
            ToolDefinition(
                "community.search_posts",
                "检索社区",
                "按关键词检索已发布帖子，参数 query、limit。",
                SearchPostsArguments,
                SearchPostsOutput,
                RiskLevel.READ,
                15,
                artifact_type=ArtifactKind.POST_SEARCH_RESULTS,
                argument_defaults={"limit": 5},
                prompt_argument="query",
                transport=TransportType.HTTP,
                idempotency_mode=IdempotencyMode.READ_DEDUP,
                capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=5),
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(429, 502, 503, 504),
                    retryable_error_codes=(
                        "TIMEOUT",
                        "RATE_LIMITED",
                        "TRANSIENT_UPSTREAM",
                    ),
                ),
            ),
            ToolDefinition(
                "community.get_post",
                "读取帖子",
                "读取指定或上下文帖子，参数 post_id 可省略。",
                PostArguments,
                PostContextOutput,
                RiskLevel.READ,
                15,
                artifact_type=ArtifactKind.POST_CONTENT,
                context_arguments={"post_id": "context_post_id"},
            ),
            ToolDefinition(
                "community.analyze_engagement",
                "分析社区活跃度",
                "按主题和时间窗口分析发帖、评论、活跃创作者及贡献者，参数 topic、days、limit。",
                AnalyzeEngagementArguments,
                EngagementAnalyticsOutput,
                RiskLevel.READ,
                30,
                artifact_type=ArtifactKind.ENGAGEMENT_ANALYSIS,
                argument_defaults={"topic": None, "days": 7, "limit": 10},
            ),
            ToolDefinition(
                "community.list_active_users",
                "列出社区活跃用户",
                "按公开发帖数和评论数列出指定时间窗口内的活跃用户，参数 days、limit。",
                ListActiveUsersArguments,
                ActiveUsersOutput,
                RiskLevel.READ,
                30,
                artifact_type=ArtifactKind.USER_SET,
                argument_defaults={"days": 30, "limit": 10},
            ),
            ToolDefinition(
                "community.list_posts_by_users",
                "读取活跃用户的公开帖子",
                "批量读取上一步活跃用户在指定时间窗口内的公开帖子，user_ids 由执行器绑定真实结果。",
                UserScopedAnalyticsArguments,
                UserPostsOutput,
                RiskLevel.READ,
                30,
                artifact_type=ArtifactKind.POST_COLLECTION,
                artifact_bindings=(ArtifactBinding(
                    "user_ids",
                    frozenset({ArtifactKind.USER_SET}),
                    "user_ids",
                    ["1"],
                ),),
                argument_defaults={"days": 30, "limit": 10},
            ),
            ToolDefinition(
                "community.aggregate_post_topics",
                "聚合用户发帖主题",
                "按公开帖子的标签和类型聚合活跃用户的主题分布，user_ids 由执行器绑定真实结果。",
                UserScopedAnalyticsArguments,
                PostTopicsOutput,
                RiskLevel.READ,
                30,
                artifact_type=ArtifactKind.TOPIC_ANALYSIS,
                artifact_bindings=(ArtifactBinding(
                    "user_ids",
                    frozenset({ArtifactKind.USER_SET}),
                    "user_ids",
                    ["1"],
                ),),
                argument_defaults={"days": 30, "limit": 10},
            ),
            ToolDefinition(
                "community.summarize_post",
                "总结帖子",
                "忠实总结指定或上下文帖子，参数 post_id、focus 可省略。",
                PostArguments,
                SummaryOutput,
                RiskLevel.READ,
                90,
                artifact_type=ArtifactKind.POST_SUMMARY,
                context_arguments={"post_id": "context_post_id"},
            ),
            ToolDefinition(
                "creator.create_draft",
                "调用创作 Agent",
                "创建并交接 Java 草稿，参数 instruction、references。",
                CreateDraftArguments,
                DraftOutput,
                RiskLevel.REVERSIBLE,
                300,
                side_effecting=True,
                artifact_type=ArtifactKind.CONTENT_DRAFT,
                artifact_bindings=(ArtifactBinding(
                    "references",
                    frozenset({
                        ArtifactKind.POST_SEARCH_RESULTS,
                        ArtifactKind.POST_CONTENT,
                        ArtifactKind.POST_SUMMARY,
                        ArtifactKind.ENGAGEMENT_ANALYSIS,
                        ArtifactKind.USER_SET,
                        ArtifactKind.POST_COLLECTION,
                        ArtifactKind.TOPIC_ANALYSIS,
                        ArtifactKind.CONTENT_DRAFT,
                    }),
                    "creator_references",
                    [],
                    required=False,
                ),),
                prompt_argument="instruction",
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.SIDE_EFFECT_REQUIRED,
                capability_budget=CapabilityBudget(base_uses=0, max_internal_calls=0),
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(),
                    retryable_error_codes=(),
                ),
            ),
            ToolDefinition(
                "creator.revise_draft",
                "修订现有草稿",
                "基于重新核验的当前草稿调用创作 Agent 生成新版本；instruction 描述本轮修改要求，草稿和版本由执行器绑定。"
                "（Assistant 修订语义；Creator 当前仍提交 CREATE_CONTENT，原生 REVISE_CONTENT 为后续协议债务。）",
                ReviseDraftArguments,
                DraftOutput,
                RiskLevel.REVERSIBLE,
                300,
                side_effecting=True,
                artifact_type=ArtifactKind.CONTENT_DRAFT,
                artifact_bindings=(
                    ArtifactBinding(
                        "draft_id",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_draft_id",
                        "1",
                        target_role="CONTENT",
                    ),
                    ArtifactBinding(
                        "expected_content_sha256",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_content_sha256",
                        "0" * 64,
                        target_role="CONTENT",
                    ),
                    ArtifactBinding(
                        "references",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "creator_references",
                        [],
                    ),
                ),
                prompt_argument="instruction",
                required_target_roles=frozenset({"CONTENT"}),
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.SIDE_EFFECT_REQUIRED,
                capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=1),
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(),
                    retryable_error_codes=(),
                ),
            ),
            ToolDefinition(
                "community.get_own_draft",
                "核验我的 AI 草稿",
                "按草稿号从 Java 重新读取当前登录用户自己的 AI 草稿，确认草稿状态和最新内容版本；用于跨轮次继续发布或定时发布。",
                OwnDraftArguments,
                OwnedDraftOutput,
                RiskLevel.READ,
                30,
                artifact_type=ArtifactKind.CONTENT_DRAFT,
                requires_progress_review=False,
                required_target_roles=frozenset({"CONTENT"}),
            ),
            ToolDefinition(
                "community.delete_post",
                "删除自己的帖子",
                "软删除当前用户自己的帖子，参数 post_id；执行前必须人工确认。",
                DeletePostArguments,
                DeletePostOutput,
                RiskLevel.EXTERNAL_WRITE,
                30,
                side_effecting=True,
                artifact_type=ArtifactKind.DELETION_RECEIPT,
                context_arguments={"post_id": "context_post_id"},
            ),
            ToolDefinition(
                "community.list_own_posts",
                "列出我的全部帖子",
                "通过 Java 用户身份分页列出当前登录用户所有未删除帖子；不使用公开搜索，参数 max_items。",
                ListOwnPostsArguments,
                ListOwnPostsOutput,
                RiskLevel.READ,
                60,
                artifact_type=ArtifactKind.OWNED_POST_SET,
                argument_defaults={"max_items": 1_000},
                transport=TransportType.HTTP,
                idempotency_mode=IdempotencyMode.READ_DEDUP,
                # Java caps maxUses at 5; one grant covers up to 5 pages (≤500 items).
                capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=5),
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(429, 502, 503, 504),
                    retryable_error_codes=(
                        "TIMEOUT",
                        "RATE_LIMITED",
                        "TRANSIENT_UPSTREAM",
                    ),
                ),
            ),
            ToolDefinition(
                "community.delete_own_posts_batch",
                "批量删除我的帖子",
                "软删除由 community.list_own_posts 绑定的当前用户帖子清单；执行器忽略模型提供的 ID，执行前只进行一次精确清单审批。",
                DeleteOwnPostsBatchArguments,
                DeleteOwnPostsBatchOutput,
                RiskLevel.EXTERNAL_WRITE,
                120,
                side_effecting=True,
                artifact_type=ArtifactKind.DELETION_RECEIPT,
                artifact_bindings=(ArtifactBinding(
                    "post_ids",
                    frozenset({ArtifactKind.OWNED_POST_SET}),
                    "owned_post_ids",
                    ["1"],
                ),),
            ),
            ToolDefinition(
                "publication.schedule",
                "安排定时发布",
                "为草稿创建可取消的定时发布任务。相对时间使用 delay_seconds，绝对时间使用 run_at；两者只能提供一个。草稿ID和版本由执行器绑定。",
                ScheduleArguments,
                ScheduleOutput,
                RiskLevel.REVERSIBLE,
                20,
                side_effecting=True,
                artifact_type=ArtifactKind.SCHEDULE_RECEIPT,
                artifact_bindings=(
                    ArtifactBinding(
                        "draft_id", frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_draft_id", "1",
                        target_role="CONTENT",
                    ),
                    ArtifactBinding(
                        "expected_content_sha256",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_content_sha256", "0" * 64,
                        target_role="CONTENT",
                    ),
                ),
                required_target_roles=frozenset({"CONTENT"}),
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.SIDE_EFFECT_REQUIRED,
                capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=2),
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(),
                    retryable_error_codes=(),
                ),
            ),
            ToolDefinition(
                "publication.get_schedule",
                "核验定时发布任务",
                "按 action_id 重新读取当前用户的定时发布任务，确认状态、发布时间和所绑定草稿。",
                ScheduleLookupArguments,
                ScheduleOutput,
                RiskLevel.READ,
                15,
                artifact_type=ArtifactKind.SCHEDULE_RECEIPT,
                requires_progress_review=False,
                required_target_roles=frozenset({"SCHEDULE"}),
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.READ_DEDUP,
                # Local DB lookup — no Java capability grant.
                capability_budget=CapabilityBudget(base_uses=0, max_internal_calls=0),
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(429, 502, 503, 504),
                    retryable_error_codes=(
                        "TIMEOUT",
                        "RATE_LIMITED",
                        "TRANSIENT_UPSTREAM",
                    ),
                ),
            ),
            ToolDefinition(
                "publication.update_schedule",
                "修改定时发布任务",
                "原子修改已核验定时任务的发布时间和/或替换为当前任务中新修订的草稿。action_id 由上游定时任务绑定；相对时间用 delay_seconds。",
                ScheduleUpdateArguments,
                ScheduleOutput,
                RiskLevel.REVERSIBLE,
                30,
                side_effecting=True,
                artifact_type=ArtifactKind.SCHEDULE_RECEIPT,
                artifact_bindings=(
                    ArtifactBinding(
                        "action_id",
                        frozenset({ArtifactKind.SCHEDULE_RECEIPT}),
                        "target_schedule_action_id",
                        "00000000-0000-0000-0000-000000000000",
                        target_role="SCHEDULE",
                    ),
                    ArtifactBinding(
                        "draft_id",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_draft_id",
                        "1",
                        target_role="CONTENT",
                        required=False,
                    ),
                    ArtifactBinding(
                        "expected_content_sha256",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_content_sha256",
                        "0" * 64,
                        target_role="CONTENT",
                        required=False,
                    ),
                ),
                required_target_roles=frozenset({"SCHEDULE"}),
                optional_target_roles=frozenset({"CONTENT"}),
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.SIDE_EFFECT_REQUIRED,
                capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=2),
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(),
                    retryable_error_codes=(),
                ),
            ),
            ToolDefinition(
                "publication.cancel_schedule",
                "取消定时发布任务",
                "取消已核验且仍未执行的定时发布任务；action_id 由上游定时任务绑定。已取消任务再次取消返回幂等成功。",
                ScheduleLookupArguments,
                ScheduleCancelledOutput,
                RiskLevel.REVERSIBLE,
                30,
                side_effecting=True,
                artifact_type=ArtifactKind.SCHEDULE_RECEIPT,
                artifact_bindings=(ArtifactBinding(
                    "action_id",
                    frozenset({ArtifactKind.SCHEDULE_RECEIPT}),
                        "target_schedule_action_id",
                        "00000000-0000-0000-0000-000000000000",
                        target_role="SCHEDULE",
                ),),
                required_target_roles=frozenset({"SCHEDULE"}),
                transport=TransportType.BUILTIN,
                idempotency_mode=IdempotencyMode.SIDE_EFFECT_REQUIRED,
                capability_budget=CapabilityBudget(base_uses=0, max_internal_calls=1),
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    initial_backoff_ms=200,
                    max_backoff_ms=2_000,
                    retryable_http_statuses=(),
                    retryable_error_codes=(),
                ),
            ),
            ToolDefinition(
                "publication.schedule_batch",
                "批量安排定时发布",
                "将 2—10 篇已创作且已审核草稿按间隔定时发布；一次审批绑定全部草稿版本，参数 run_at、interval_minutes，items 由执行器绑定真实产物。",
                ScheduleBatchArguments,
                ScheduleBatchOutput,
                RiskLevel.EXTERNAL_WRITE,
                60,
                side_effecting=True,
                artifact_type=ArtifactKind.SCHEDULE_RECEIPT,
                artifact_bindings=(ArtifactBinding(
                    "items", frozenset({ArtifactKind.CONTENT_DRAFT}),
                    "draft_items",
                    [
                        {"draft_id": "1", "expected_content_sha256": "0" * 64},
                        {"draft_id": "2", "expected_content_sha256": "1" * 64},
                    ],
                ),),
                argument_defaults={"interval_minutes": 30},
            ),
            ToolDefinition(
                "publication.publish_now",
                "立即发布帖子",
                "立即公开发布草稿；这是外部写入，执行前必须获得用户批准。",
                PublishArguments,
                PublishOutput,
                RiskLevel.EXTERNAL_WRITE,
                30,
                side_effecting=True,
                artifact_type=ArtifactKind.PUBLICATION_RECEIPT,
                artifact_bindings=(
                    ArtifactBinding(
                        "draft_id", frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_draft_id", "1",
                        target_role="CONTENT",
                    ),
                    ArtifactBinding(
                        "expected_content_sha256",
                        frozenset({ArtifactKind.CONTENT_DRAFT}),
                        "target_content_sha256", "0" * 64,
                        target_role="CONTENT",
                    ),
                ),
                required_target_roles=frozenset({"CONTENT"}),
                optional_target_roles=frozenset({"SCHEDULE"}),
            ),
            ToolDefinition(
                "community.reply_comment",
                "回复评论",
                "以知光助手系统身份持久化回复 @助手 的评论。",
                ReplyCommentArguments,
                ReplyCommentOutput,
                RiskLevel.REVERSIBLE,
                20,
                False,
                True,
                artifact_type=ArtifactKind.COMMENT_RECEIPT,
                artifact_bindings=(ArtifactBinding(
                    "content",
                    frozenset({ArtifactKind.POST_SUMMARY}),
                    "summary_reply",
                    "基于上游总结生成的回复",
                    required=False,
                    allow_planner_value=True,
                ),),
                context_arguments={
                    "post_id": "context_post_id",
                    "parent_comment_id": "context_comment_id",
                },
            ),
        ]))
        self._definitions = {item.name: item for item in definitions}
        # Compat staging only: MCP discover / bootstrap may park handlers here
        # before a ToolRuntime instance adopts them. Authoritative mutable
        # handlers live on ToolRuntime — not on this shared definition registry.
        self._handlers: dict[str, ToolHandler] = {}

    def register_definition(self, definition: ToolDefinition) -> None:
        """Register an immutable tool definition without a handler."""
        if definition.name in self._definitions:
            raise ValueError(f"Duplicate tool: {definition.name}")
        self._definitions[definition.name] = definition

    def get_definition(self, name: str) -> ToolDefinition:
        return self.get(name)

    def list_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            sorted(self._definitions.values(), key=lambda item: item.name)
        )

    def register_handler(self, name: str, handler: ToolHandler) -> None:
        """Park a handler for later adoption by a ToolRuntime instance.

        Prefer ``ToolRuntime.register_handler``. This method remains for MCP
        bootstrap and tests that register before a Worker exists.
        """
        self.get(name)
        if name in self._handlers:
            raise ValueError(f"Duplicate tool handler: {name}")
        self._handlers[name] = handler

    def register(self, definition: ToolDefinition, handler: ToolHandler | None = None) -> None:
        """Register a new tool contract and optionally stage its handler."""
        if definition.name in self._definitions:
            raise ValueError(f"Duplicate tool: {definition.name}")
        self._definitions[definition.name] = definition
        if handler is not None:
            self._handlers[definition.name] = handler

    def handler_for(self, name: str) -> ToolHandler | None:
        """Return a staged (pre-runtime) handler, if any."""
        self.get(name)
        return self._handlers.get(name)

    def take_handler(self, name: str) -> ToolHandler | None:
        """Remove and return a staged handler for ToolRuntime adoption."""
        self.get(name)
        return self._handlers.pop(name, None)

    def drain_handlers(self) -> dict[str, ToolHandler]:
        """Move all staged handlers out of the shared definition registry."""
        handlers = dict(self._handlers)
        self._handlers.clear()
        return handlers

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def register_mcp_tool(
        self, *, name: str, label: str, description: str,
        risk: RiskLevel = RiskLevel.READ,
        side_effecting: bool = False,
        handler: ToolHandler | None = None,
    ) -> None:
        if not name.startswith("mcp."):
            raise ValueError("MCP tool name must use the mcp.* namespace")
        if name in self._definitions:
            raise ValueError(f"Duplicate tool: {name}")
        self.register(
            ToolDefinition(
                name,
                label,
                description,
                McpArguments,
                McpOutput,
                risk,
                60,
                side_effecting=side_effecting,
                execution_mode=ExecutionMode.ASYNC,
                artifact_type=ArtifactKind.MCP_RESULT,
            ),
            handler=handler,
        )

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"未知或未授权工具：{name}") from exc

    def validate(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        definition = self.get(name)
        return definition.arguments_model.model_validate(arguments).model_dump(
            mode="json", exclude_none=True
        )

    def validate_output(
        self,
        name: str,
        output: dict[str, Any],
        arguments: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any]:
        definition = self.get(name)
        candidate = dict(output)
        if name == "community.get_post":
            candidate = guard_post_payload(candidate)
        elif name == "community.search_posts":
            guarded_results = []
            for raw in candidate.get("results") or []:
                item = dict(raw)
                title = inspect_untrusted_text(item.get("title"), max_chars=256)
                description = inspect_untrusted_text(
                    item.get("description"),
                    max_chars=1_000,
                )
                item["title"] = title.text
                if item.get("description") is not None:
                    item["description"] = description.text
                item["untrusted_content"] = True
                item["injection_signals"] = list(
                    dict.fromkeys((*title.signals, *description.signals))
                )
                guarded_results.append(item)
            candidate["results"] = guarded_results
        elif name == "community.list_own_posts":
            guarded_posts = []
            for raw in candidate.get("posts") or []:
                item = dict(raw)
                title = inspect_untrusted_text(item.get("title"), max_chars=256)
                if item.get("title") is not None:
                    item["title"] = title.text
                item["untrusted_content"] = True
                item["injection_signals"] = list(title.signals)
                guarded_posts.append(item)
            candidate["posts"] = guarded_posts
        elif name == "community.list_posts_by_users":
            guarded_posts = []
            for raw in candidate.get("posts") or []:
                item = dict(raw)
                title = inspect_untrusted_text(item.get("title"), max_chars=256)
                description = inspect_untrusted_text(
                    item.get("description"),
                    max_chars=1_000,
                )
                item["title"] = title.text
                if item.get("description") is not None:
                    item["description"] = description.text
                guarded_posts.append(item)
            candidate["posts"] = guarded_posts
        validated = definition.output_model.model_validate(candidate)
        data = validated.model_dump(mode="json", exclude_none=True)
        self._validate_semantics(name, data, arguments, run_id)
        return data

    def catalog_prompt(self) -> str:
        return "\n".join(
            (
                f"- {item.name}：{item.description} 风险={item.risk.value} "
                f"执行={item.execution_mode.value}"
            )
            for item in self._definitions.values()
            if item.planner_visible
        )

    def signature(self) -> str:
        payload = [
            {
                "name": item.name,
                "arguments": item.arguments_model.model_json_schema(),
                "output": item.output_model.model_json_schema(),
                "risk": item.risk.value,
                "side_effecting": item.side_effecting,
                "execution_mode": item.execution_mode.value,
                "planner_visible": item.planner_visible,
                "requires_progress_review": item.requires_progress_review,
                "artifact_type": item.artifact_type,
                "required_target_roles": sorted(item.required_target_roles),
                "optional_target_roles": sorted(item.optional_target_roles),
                "runtime_bound_arguments": sorted(item.runtime_bound_arguments),
                "runtime_argument_examples": item.runtime_argument_examples,
                "artifact_bindings": [
                    {
                        "argument": binding.argument,
                        "accepts": sorted(binding.accepts),
                        "resolver": binding.resolver,
                        "required": binding.required,
                        "allow_planner_value": binding.allow_planner_value,
                        "target_role": binding.target_role,
                    }
                    for binding in item.artifact_bindings
                ],
                "argument_defaults": item.argument_defaults,
                "prompt_argument": item.prompt_argument,
                "context_arguments": item.context_arguments,
            }
            for item in sorted(self._definitions.values(), key=lambda value: value.name)
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_semantics(
        name: str, output: dict[str, Any], arguments: dict[str, Any], run_id: str
    ) -> None:
        if name == "community.search_posts":
            if len(output["results"]) > int(arguments["limit"]):
                raise ValueError("检索结果超过请求上限")
            ids = [item["id"] for item in output["results"]]
            if len(ids) != len(set(ids)):
                raise ValueError("检索结果包含重复帖子")
        elif name == "community.get_post":
            if output["id"] != str(arguments["post_id"]):
                raise ValueError("返回帖子与请求资源不一致")
        elif name == "community.summarize_post":
            if output["post_id"] != str(arguments["post_id"]):
                raise ValueError("总结结果与请求帖子不一致")
        elif name in {"publication.schedule", "publication.publish_now"}:
            if output["draft_id" if name.endswith("schedule") else "post_id"] != str(
                arguments["draft_id"]
            ):
                raise ValueError("发布结果与批准草稿不一致")
        elif name in {"publication.get_schedule", "publication.update_schedule", "publication.cancel_schedule"}:
            if output["action_id"] != str(arguments["action_id"]):
                raise ValueError("定时任务结果与请求对象不一致")
            if name == "publication.update_schedule" and arguments.get("draft_id"):
                if output["draft_id"] != str(arguments["draft_id"]):
                    raise ValueError("改期结果没有绑定修订后的草稿")
        elif name == "publication.schedule_batch":
            expected = {
                str(item["draft_id"]): str(item["expected_content_sha256"]).lower()
                for item in arguments["items"]
            }
            actual = {
                str(item["draft_id"]) for item in output.get("actions", [])
            }
            if actual != set(expected):
                raise ValueError("批量定时结果与批准的草稿清单不一致")
        elif name == "community.list_posts_by_users":
            allowed_users = {str(value) for value in arguments["user_ids"]}
            if any(
                str(item["author_id"]) not in allowed_users
                for item in output.get("posts", [])
            ):
                raise ValueError("用户帖子分析返回了请求范围外的作者")
        elif name == "community.delete_post":
            if output["post_id"] != str(arguments["post_id"]):
                raise ValueError("删除结果与批准的帖子不一致")
        elif name == "community.list_own_posts":
            if output["count"] != len(output["posts"]):
                raise ValueError("本人帖子列表计数不一致")
            ids = [item["id"] for item in output["posts"]]
            if len(ids) != len(set(ids)):
                raise ValueError("本人帖子列表包含重复 ID")
        elif name == "community.delete_own_posts_batch":
            expected = {str(value) for value in arguments["post_ids"]}
            actual = {str(value) for value in output["post_ids"]}
            if actual != expected:
                raise ValueError("批量删除结果与批准的帖子清单不一致")
            if (
                output["deleted_count"] + output["already_deleted_count"]
                != len(expected)
            ):
                raise ValueError("批量删除结果数量不一致")
        elif name == "community.reply_comment":
            if (
                output["post_id"] != str(arguments["post_id"])
                or output["parent_id"] != str(arguments["parent_comment_id"])
                or output["assistant_run_id"] != run_id
                or not output["assistant"]
            ):
                raise ValueError("助手评论来源校验失败")


tool_registry = ToolRegistry()
