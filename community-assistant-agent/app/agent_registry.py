from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from app.domain import AgentPlan, AgentPlanStep


@dataclass(frozen=True)
class AgentDescriptor:
    name: str
    description: str
    capabilities: frozenset[str]
    tools: frozenset[str]
    max_parallel_tasks: int = 1

    def supports(self, step: AgentPlanStep) -> bool:
        tool_supported = (
            step.tool in self.tools
            or any(
                pattern.endswith(".*") and step.tool.startswith(pattern[:-1])
                for pattern in self.tools
            )
        )
        return tool_supported and set(step.capabilities).issubset(self.capabilities)


class AgentRegistry:
    def __init__(self, agents: Iterable[AgentDescriptor]) -> None:
        materialized = list(agents)
        self._agents = {agent.name: agent for agent in materialized}
        if len(self._agents) != len(materialized):
            raise ValueError("Agent Registry contains duplicate names")

    def get(self, name: str) -> AgentDescriptor:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent: {name}") from exc

    def route(self, step: AgentPlanStep) -> AgentDescriptor:
        requested = self._agents.get(step.agent)
        if requested and requested.supports(step):
            return requested
        candidates = [agent for agent in self._agents.values() if agent.supports(step)]
        if not candidates:
            raise ValueError(
                f"No registered agent can execute {step.tool} "
                f"with capabilities {step.capabilities}"
            )
        candidates.sort(
            key=lambda agent: (
                len(agent.capabilities - set(step.capabilities)),
                len(agent.tools),
                agent.name,
            )
        )
        return candidates[0]

    def route_plan(self, plan: AgentPlan) -> AgentPlan:
        routed_steps = []
        for step in plan.steps:
            selected = self.route(step)
            routed_steps.append(step.model_copy(update={"agent": selected.name}))
        return plan.model_copy(update={"steps": routed_steps})

    def catalog_prompt(self) -> str:
        return "\n".join(
            (
                f"- {agent.name}: {agent.description}; "
                f"capabilities={sorted(agent.capabilities)}; tools={sorted(agent.tools)}"
            )
            for agent in sorted(self._agents.values(), key=lambda item: item.name)
        )

    def public_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "name": agent.name,
                "description": agent.description,
                "capabilities": sorted(agent.capabilities),
                "tools": sorted(agent.tools),
                "max_parallel_tasks": agent.max_parallel_tasks,
            }
            for agent in sorted(self._agents.values(), key=lambda item: item.name)
        ]

    def signature(self) -> str:
        encoded = json.dumps(
            self.public_catalog(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


agent_registry = AgentRegistry(
    [
        AgentDescriptor(
            "SearchAgent",
            "检索、读取并基于社区证据回答问题。",
            frozenset({"search", "read_post", "summarize"}),
            frozenset(
                {
                    "community.search_posts",
                    "community.get_post",
                    "community.summarize_post",
                }
            ),
            max_parallel_tasks=4,
        ),
        AgentDescriptor(
            "AnalyticsAgent",
            "分析社区主题趋势、帖子表现和时间窗口指标。",
            frozenset({"analysis", "trend_analysis"}),
            frozenset({"community.analyze_engagement"}),
            max_parallel_tasks=2,
        ),
        AgentDescriptor(
            "UserInsightAgent",
            "分析用户互动、贡献者和受众活跃结构。",
            frozenset({"analysis", "user_insight"}),
            frozenset({"community.analyze_engagement"}),
            max_parallel_tasks=2,
        ),
        AgentDescriptor(
            "ContentCreationAgent",
            "委派 Creator Agent 生成或改写社区内容草稿。",
            frozenset({"generation", "rewrite_content"}),
            frozenset({"creator.create_draft"}),
        ),
        AgentDescriptor(
            "ModerationAgent",
            "委派内容审核 Agent 对待发布草稿进行风险检查。",
            frozenset({"moderation", "risk_check"}),
            frozenset({"moderation.check_draft"}),
        ),
        AgentDescriptor(
            "PublishAgent",
            "在人工确认和版本边界内立即或定时发布。",
            frozenset({"publishing", "schedule_publish"}),
            frozenset(
                {
                    "publication.publish_now",
                    "publication.schedule",
                    "publication.schedule_batch",
                }
            ),
        ),
        AgentDescriptor(
            "InteractionAgent",
            "处理评论区助手回复。",
            frozenset({"comment_interaction"}),
            frozenset({"community.reply_comment"}),
        ),
        AgentDescriptor(
            "ContentManagementAgent",
            "在当前用户身份和精确资源清单内查询、删除社区内容。",
            frozenset({"list_own_content", "delete_content"}),
            frozenset(
                {
                    "community.list_own_posts",
                    "community.delete_post",
                    "community.delete_own_posts_batch",
                }
            ),
        ),
        AgentDescriptor(
            "MCPAgent",
            "通过白名单 MCP Server 使用受控的外部工具。",
            frozenset({"external_research", "mcp_tool"}),
            frozenset({"mcp.*"}),
            max_parallel_tasks=2,
        ),
    ]
)
