"""Query Agent: read-only community queries outside the ACTION/Task path.

Pipeline: Router(QUERY) → QueryAgent → QueryCatalog → Read Tool → Answer

Hard constraints:
- never create ConversationGoal / IntentDelta
- never call GoalResolver / TargetResolver / TaskManager
- never mutate tasks or schedules
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from app.router import RouteDecision

QueryKind = Literal[
    "OWN_POST_COUNT",
    "OWN_POST_LIST",
    "SCHEDULE_STATUS",
    "ENGAGEMENT",
    "UNSUPPORTED",
]

ReadToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class QuerySpec:
    kind: QueryKind
    tool: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True)
class QueryResult:
    kind: QueryKind
    data: dict[str, Any]
    answer: str
    tool_name: str | None = None
    created_goal: bool = False
    touched_task: bool = False
    used_goal_resolver: bool = False
    used_target_resolver: bool = False
    created_intent_delta: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "data": self.data,
            "answer": self.answer,
            "tool_name": self.tool_name,
            "created_goal": self.created_goal,
            "touched_task": self.touched_task,
            "used_goal_resolver": self.used_goal_resolver,
            "used_target_resolver": self.used_target_resolver,
            "created_intent_delta": self.created_intent_delta,
        }


def _normalize(text: str) -> str:
    return "".join(str(text or "").lower().split())


_COUNT = re.compile(
    r"(多少|几条|几个|几篇|数量|总数|count|howmany)",
    re.IGNORECASE,
)
_LIST = re.compile(
    r"(哪些|有哪些|列出|罗列|列表|清单|最近.{0,8}(帖|文章)|最近发布)",
    re.IGNORECASE,
)
_SCHEDULE = re.compile(
    r"(定时|发布时间|schedule).{0,12}(状态|进度|情况)?|"
    r"(状态|进度).{0,12}(定时|发布)|"
    r"查询定时|查看定时|看看定时",
    re.IGNORECASE,
)
_ENGAGEMENT = re.compile(
    r"(互动|活跃|engagement|趋势).{0,12}(数据|分析|统计)?|"
    r"(分析|统计).{0,12}(互动|活跃|用户|数据|趋势)",
    re.IGNORECASE,
)


class QueryCatalog:
    """Map a QUERY message to a single read-tool invocation."""

    def resolve(self, message: str, route: RouteDecision | None = None) -> QuerySpec:
        del route
        text = message or ""
        lowered = _normalize(text)

        if _SCHEDULE.search(text) or _SCHEDULE.search(lowered):
            return QuerySpec(
                kind="SCHEDULE_STATUS",
                tool=None,
                arguments={},
                summary="查询定时发布状态",
            )
        if _ENGAGEMENT.search(text):
            return QuerySpec(
                kind="ENGAGEMENT",
                tool="community.analyze_engagement",
                arguments={"days": 7, "limit": 20},
                summary="查询互动数据",
            )
        if _COUNT.search(lowered) and any(
            token in lowered for token in ("帖", "文章", "草稿", "post", "发布")
        ):
            return QuerySpec(
                kind="OWN_POST_COUNT",
                tool="community.list_own_posts",
                arguments={"max_items": 1_000},
                summary="查询已发布帖子数量",
            )
        if _LIST.search(text) or _LIST.search(lowered):
            return QuerySpec(
                kind="OWN_POST_LIST",
                tool="community.list_own_posts",
                arguments={"max_items": 20},
                summary="查询最近发布的帖子列表",
            )
        if any(token in lowered for token in ("帖", "文章", "post", "草稿")):
            return QuerySpec(
                kind="OWN_POST_LIST",
                tool="community.list_own_posts",
                arguments={"max_items": 20},
                summary="查询帖子列表",
            )
        return QuerySpec(
            kind="UNSUPPORTED",
            tool=None,
            arguments={},
            summary="暂不支持的查询",
        )


class QueryAgent:
    """Execute catalogued read queries and render answers."""

    def __init__(self, catalog: QueryCatalog | None = None) -> None:
        self.catalog = catalog or QueryCatalog()

    async def handle(
        self,
        *,
        message: str,
        route: RouteDecision | None = None,
        execute_tool: ReadToolExecutor | None = None,
        schedules: list[dict[str, Any]] | None = None,
    ) -> QueryResult:
        spec = self.catalog.resolve(message, route)
        if spec.kind == "OWN_POST_COUNT":
            return await self._own_post_count(spec, execute_tool)
        if spec.kind == "OWN_POST_LIST":
            return await self._own_post_list(spec, execute_tool)
        if spec.kind == "SCHEDULE_STATUS":
            return self._schedule_status(spec, schedules or [])
        if spec.kind == "ENGAGEMENT":
            return await self._engagement(spec, execute_tool)
        return QueryResult(
            kind="UNSUPPORTED",
            data={},
            answer="这是查询请求，但我还没有对应的只读查询能力。请换一种问法，例如“我发布多少帖子”。",
        )

    async def _own_post_count(
        self,
        spec: QuerySpec,
        execute_tool: ReadToolExecutor | None,
    ) -> QueryResult:
        if execute_tool is None:
            raise RuntimeError("QueryAgent 缺少只读工具执行器")
        assert spec.tool == "community.list_own_posts"
        output = await execute_tool(spec.tool, dict(spec.arguments))
        posts = list(output.get("posts") or [])
        truncated = bool(output.get("truncated"))
        count = int(output.get("count") if output.get("count") is not None else len(posts))
        data = {"count": count, "truncated": truncated, "posts": posts[:5]}
        if truncated:
            answer = f"你目前至少已发布 {count} 条帖子（列表已截断，实际可能更多）。"
        else:
            answer = f"你目前已发布 {count} 条帖子。"
        return QueryResult(
            kind="OWN_POST_COUNT",
            data=data,
            answer=answer,
            tool_name=spec.tool,
        )

    async def _own_post_list(
        self,
        spec: QuerySpec,
        execute_tool: ReadToolExecutor | None,
    ) -> QueryResult:
        if execute_tool is None:
            raise RuntimeError("QueryAgent 缺少只读工具执行器")
        assert spec.tool == "community.list_own_posts"
        output = await execute_tool(spec.tool, dict(spec.arguments))
        posts = list(output.get("posts") or [])
        count = int(output.get("count") if output.get("count") is not None else len(posts))
        lines: list[str] = []
        for index, post in enumerate(posts[:20], start=1):
            post_id = str(post.get("id") or post.get("postId") or "")
            title = str(post.get("title") or "未命名帖子").strip() or "未命名帖子"
            status = str(post.get("status") or "").strip()
            suffix = f"（{status}）" if status else ""
            identity = f" #{post_id}" if post_id else ""
            lines.append(f"{index}. {title}{identity}{suffix}")
        data = {"count": count, "posts": posts, "truncated": bool(output.get("truncated"))}
        if not lines:
            answer = "最近没有查到你发布的帖子。"
        else:
            answer = "最近发布的帖子：\n" + "\n".join(lines)
            if data["truncated"]:
                answer += "\n（仅展示部分结果）"
        return QueryResult(
            kind="OWN_POST_LIST",
            data=data,
            answer=answer,
            tool_name=spec.tool,
        )

    def _schedule_status(
        self,
        spec: QuerySpec,
        schedules: list[dict[str, Any]],
    ) -> QueryResult:
        del spec
        active = [
            item
            for item in schedules
            if str(item.get("status") or "").upper()
            in {"SCHEDULED", "RETRYING", "RUNNING"}
        ]
        data = {
            "count": len(schedules),
            "active_count": len(active),
            "schedules": schedules[:20],
        }
        if not schedules:
            answer = "当前没有查到定时发布任务。"
        else:
            lines: list[str] = []
            for index, item in enumerate(schedules[:10], start=1):
                action_id = str(item.get("id") or item.get("action_id") or "")
                status = str(item.get("status") or "UNKNOWN")
                run_at = str(item.get("run_at") or item.get("runAt") or "")
                draft_id = str(item.get("draft_id") or item.get("draftId") or "")
                parts = [f"{index}. 定时 {action_id or '未知'}：{status}"]
                if run_at:
                    parts.append(f"时间 {run_at}")
                if draft_id:
                    parts.append(f"草稿 {draft_id}")
                lines.append("，".join(parts))
            answer = "定时发布状态：\n" + "\n".join(lines)
        return QueryResult(
            kind="SCHEDULE_STATUS",
            data=data,
            answer=answer,
            tool_name=None,
        )

    async def _engagement(
        self,
        spec: QuerySpec,
        execute_tool: ReadToolExecutor | None,
    ) -> QueryResult:
        if execute_tool is None:
            raise RuntimeError("QueryAgent 缺少只读工具执行器")
        assert spec.tool == "community.analyze_engagement"
        output = await execute_tool(spec.tool, dict(spec.arguments))
        return QueryResult(
            kind="ENGAGEMENT",
            data=dict(output),
            answer="已完成互动数据分析，详情见查询结果。",
            tool_name=spec.tool,
        )


query_agent = QueryAgent()
