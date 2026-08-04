"""Control-plane mode router: QUERY / ACTION / CHAT only.

This layer classifies user turns before Adaptive Router, GoalResolver, Planner,
or Tool execution. It must never produce a plan, call tools, or mutate storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RouteMode = Literal["QUERY", "ACTION", "CHAT"]
RouteDomain = Literal["content", "schedule", "community", "general"]


@dataclass(frozen=True)
class RouteDecision:
    mode: RouteMode
    domain: RouteDomain
    confidence: float
    summary: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "domain": self.domain,
            "confidence": self.confidence,
            "summary": self.summary,
        }


def _normalize(text: str) -> str:
    return "".join(str(text or "").lower().split())


_INVENTORY_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(发布|发了|发过).{0,12}(多少|几条|几个|几篇)"),
    re.compile(r"(多少|几条|几个|几篇).{0,12}(帖子|帖|文章|草稿|内容)"),
    re.compile(r"(帖子|帖|文章|草稿).{0,12}(多少|几条|几个|几篇|数量|总数)"),
    re.compile(r"(我的|有哪些|哪些|列出|罗列|统计|最近).{0,16}(帖子|帖|文章|草稿)"),
    re.compile(r"(帖子|帖|文章).{0,12}(列表|清单)"),
    re.compile(r"(how\s*many|count).{0,20}(post|posts|draft)"),
)

_STATUS_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(查询|查看|看看|告诉我).{0,16}(状态|进度|定时|发布时间)"),
    re.compile(r"(什么时候|几点).{0,12}(发布|发)"),
    re.compile(r"(发布|定时).{0,12}(了吗|了没|成功了吗|状态)"),
)

_SEARCH_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(搜索|检索|找几篇|找一些|查找).{0,20}(帖|文章|内容)?"),
    re.compile(r"(分析|统计).{0,16}(互动|活跃|用户|数据|趋势)"),
)

_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(发布|发一篇|写一篇|写个|创建|生成).{0,24}(帖子|帖|草稿|文章|内容)?"
    ),
    re.compile(r"(修改|改一下|改成|改为|调整|追加|替换|重写|更新).{0,24}"),
    re.compile(r"(删除|取消|撤销).{0,16}(帖|定时|任务|草稿)?"),
    re.compile(r"(定时|推迟|延后|提前|立刻发布|立即发布|现在发布)"),
    re.compile(r"(回复|评论).{0,16}"),
    re.compile(r"(publish|create|schedule|delete|update|revise).{0,24}(post|draft)?"),
)

_CHAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(介绍|讲解|解释|说说|聊聊|是什么|什么是|帮我了解)"),
    re.compile(r"(怎么理解|如何理解|科普|概述)"),
    re.compile(r"\b(what\s+is|explain|introduce|tell\s+me\s+about)\b"),
)


def _domain_for(mode: RouteMode, text: str) -> RouteDomain:
    if any(token in text for token in ("定时", "时间", "schedule", "几点")):
        if mode == "ACTION":
            return "schedule"
    if any(
        token in text
        for token in ("帖", "草稿", "文章", "发布", "内容", "post", "draft")
    ):
        return "content"
    if any(
        token in text
        for token in ("搜索", "活跃", "用户", "互动", "社区", "分析", "search")
    ):
        return "community"
    if mode == "CHAT":
        return "general"
    return "content" if mode == "ACTION" else "community"


class ControlPlaneRouter:
    """Deterministic QUERY / ACTION / CHAT classifier."""

    def classify(self, message: str) -> RouteDecision:
        text = _normalize(message)
        if not text:
            return RouteDecision(
                mode="CHAT",
                domain="general",
                confidence=0.5,
                summary="空消息，按闲聊处理",
            )

        # Inventory / count questions must win over bare "发布" tokens.
        if any(pattern.search(text) for pattern in _INVENTORY_QUERY_PATTERNS):
            return RouteDecision(
                mode="QUERY",
                domain=_domain_for("QUERY", text),
                confidence=0.95,
                summary="识别为库存/计数类查询",
            )
        if any(pattern.search(text) for pattern in _STATUS_QUERY_PATTERNS):
            return RouteDecision(
                mode="QUERY",
                domain=_domain_for("QUERY", text),
                confidence=0.9,
                summary="识别为状态查询",
            )
        if any(pattern.search(text) for pattern in _SEARCH_QUERY_PATTERNS):
            return RouteDecision(
                mode="QUERY",
                domain=_domain_for("QUERY", text),
                confidence=0.88,
                summary="识别为检索/分析查询",
            )

        action_hit = any(pattern.search(text) for pattern in _ACTION_PATTERNS)
        chat_hit = any(pattern.search(text) for pattern in _CHAT_PATTERNS)

        if action_hit and not chat_hit:
            return RouteDecision(
                mode="ACTION",
                domain=_domain_for("ACTION", text),
                confidence=0.93,
                summary="识别为任务/副作用操作",
            )
        if chat_hit and not action_hit:
            return RouteDecision(
                mode="CHAT",
                domain=_domain_for("CHAT", text),
                confidence=0.92,
                summary="识别为知识/闲聊问答",
            )
        if action_hit and chat_hit:
            # "介绍一下并帮我发帖" → prefer ACTION.
            return RouteDecision(
                mode="ACTION",
                domain=_domain_for("ACTION", text),
                confidence=0.8,
                summary="同时含操作与闲聊标记，优先 ACTION",
            )

        # Conservative default: treat unknown as ACTION so mutations are not dropped.
        return RouteDecision(
            mode="ACTION",
            domain=_domain_for("ACTION", text),
            confidence=0.55,
            summary="未命中强规则，默认 ACTION",
        )


control_plane_router = ControlPlaneRouter()
