from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.untrusted_content import guard_post_payload, inspect_untrusted_text


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


class ScheduleArguments(ToolArguments):
    run_at: str = Field(min_length=10, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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


class ModerationDraftArguments(ToolArguments):
    draft_id: str = Field(min_length=1, max_length=64)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


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


class ScheduleOutput(ToolOutput):
    action_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    run_at: datetime
    status: str = Field(pattern=r"^SCHEDULED$")


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


class ModerationOutput(ToolOutput):
    task_id: str
    draft_id: str
    status: str
    final_action: str | None = None
    risk_type: str | None = None
    risk_score: float | None = Field(default=None, ge=0, le=1)
    requires_human_review: bool = False
    reason: str | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


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


class ToolRegistry:
    def __init__(self) -> None:
        definitions = [
            ToolDefinition(
                "community.search_posts",
                "检索社区",
                "按关键词检索已发布帖子，参数 query、limit。",
                SearchPostsArguments,
                SearchPostsOutput,
                RiskLevel.READ,
                15,
            ),
            ToolDefinition(
                "community.get_post",
                "读取帖子",
                "读取指定或上下文帖子，参数 post_id 可省略。",
                PostArguments,
                PostContextOutput,
                RiskLevel.READ,
                15,
            ),
            ToolDefinition(
                "community.analyze_engagement",
                "分析社区活跃度",
                "按主题和时间窗口分析发帖、评论、活跃创作者及贡献者，参数 topic、days、limit。",
                AnalyzeEngagementArguments,
                EngagementAnalyticsOutput,
                RiskLevel.READ,
                30,
            ),
            ToolDefinition(
                "community.summarize_post",
                "总结帖子",
                "忠实总结指定或上下文帖子，参数 post_id、focus 可省略。",
                PostArguments,
                SummaryOutput,
                RiskLevel.READ,
                90,
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
            ),
            ToolDefinition(
                "moderation.check_draft",
                "调用审核 Agent",
                "读取当前用户的 AI 草稿并提交真实审核 Agent，参数 draft_id、expected_content_sha256。",
                ModerationDraftArguments,
                ModerationOutput,
                RiskLevel.REVERSIBLE,
                180,
                side_effecting=True,
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
            ),
            ToolDefinition(
                "community.list_own_posts",
                "列出我的全部帖子",
                "通过 Java 用户身份分页列出当前登录用户所有未删除帖子；不使用公开搜索，参数 max_items。",
                ListOwnPostsArguments,
                ListOwnPostsOutput,
                RiskLevel.READ,
                60,
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
            ),
            ToolDefinition(
                "publication.schedule",
                "安排定时发布",
                "为草稿创建可取消的定时发布任务，参数 run_at、draft_id。",
                ScheduleArguments,
                ScheduleOutput,
                RiskLevel.REVERSIBLE,
                20,
                side_effecting=True,
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
            ),
        ]
        self._definitions = {item.name: item for item in definitions}

    def register_mcp_tool(
        self, *, name: str, label: str, description: str
    ) -> None:
        if not name.startswith("mcp."):
            raise ValueError("MCP tool name must use the mcp.* namespace")
        if name in self._definitions:
            raise ValueError(f"Duplicate tool: {name}")
        self._definitions[name] = ToolDefinition(
            name,
            label,
            description,
            McpArguments,
            McpOutput,
            RiskLevel.READ,
            60,
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
        validated = definition.output_model.model_validate(candidate)
        data = validated.model_dump(mode="json", exclude_none=True)
        self._validate_semantics(name, data, arguments, run_id)
        return data

    def catalog_prompt(self) -> str:
        return "\n".join(
            f"- {item.name}：{item.description} 风险={item.risk.value}"
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
                "planner_visible": item.planner_visible,
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
        elif name == "moderation.check_draft":
            if (
                output["draft_id"] != str(arguments["draft_id"])
                or output["content_sha256"].lower()
                != str(arguments["expected_content_sha256"]).lower()
            ):
                raise ValueError("审核结果与绑定草稿版本不一致")
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
