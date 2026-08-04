from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

from app.capability_graph import CapabilityGraph, capability_graph
from app.domain import AgentPlan, AgentPlanStep


@dataclass(frozen=True)
class AgentDescriptor:
    name: str
    description: str
    capabilities: frozenset[str]
    tools: frozenset[str]
    max_parallel_tasks: int = 1

    def supports_tool(self, tool: str) -> bool:
        return tool in self.tools or any(
            pattern.endswith(".*") and tool.startswith(pattern[:-1])
            for pattern in self.tools
        )

    def supports(
        self,
        step: AgentPlanStep,
        graph: CapabilityGraph = capability_graph,
    ) -> bool:
        return self.supports_tool(step.tool) and (
            step.primary_capability is None
            or graph.covers(self.capabilities, step.primary_capability)
        )


class AgentRegistry:
    """Capability-based routing over a versioned, loadable Agent manifest."""

    def __init__(
        self,
        agents: Iterable[AgentDescriptor],
        *,
        capabilities: CapabilityGraph = capability_graph,
    ) -> None:
        materialized = list(agents)
        self._agents = {agent.name: agent for agent in materialized}
        if len(self._agents) != len(materialized):
            raise ValueError("Agent Registry contains duplicate names")
        self.capability_graph = capabilities

    @classmethod
    def from_manifest(cls, path: str | Path) -> "AgentRegistry":
        manifest_path = Path(path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Agent manifest must be a JSON array")
        agents: list[AgentDescriptor] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Each Agent manifest entry must be an object")
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
            capabilities = item.get("capabilities")
            tools = item.get("tools")
            if (
                not name
                or not description
                or not isinstance(capabilities, list)
                or not isinstance(tools, list)
            ):
                raise ValueError(
                    "Agent manifest entries require name, description, capabilities and tools"
                )
            agents.append(
                AgentDescriptor(
                    name=name,
                    description=description,
                    capabilities=frozenset(str(value) for value in capabilities),
                    tools=frozenset(str(value) for value in tools),
                    max_parallel_tasks=max(1, int(item.get("max_parallel_tasks", 1))),
                )
            )
        return cls(agents)

    def get(self, name: str) -> AgentDescriptor:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent: {name}") from exc

    def candidates(self, step: AgentPlanStep) -> list[AgentDescriptor]:
        candidates = [
            agent
            for agent in self._agents.values()
            if agent.supports_tool(step.tool)
            and self.capability_graph.covers(
                agent.capabilities,
                step.primary_capability,
            )
        ]
        candidates.sort(
            key=lambda agent: (
                0 if agent.name == step.agent else 1,
                -len(set(step.capabilities) & agent.capabilities),
                len(agent.capabilities - set(step.capabilities)),
                len(agent.tools),
                agent.name,
            )
        )
        return candidates

    def route(self, step: AgentPlanStep) -> AgentDescriptor:
        requested = self._agents.get(step.agent)
        if (
            requested
            and requested.supports_tool(step.tool)
            and self.capability_graph.covers(
                requested.capabilities,
                step.primary_capability,
            )
        ):
            return requested
        candidates = self.candidates(step)
        if not candidates:
            raise ValueError(
                f"No registered agent can execute {step.tool} "
                f"with primary capability {step.primary_capability}"
            )
        return candidates[0]

    def route_plan(self, plan: AgentPlan) -> AgentPlan:
        return plan.model_copy(
            update={
                "steps": [
                    step.model_copy(update={"agent": self.route(step).name})
                    for step in plan.steps
                ]
            }
        )

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
            {
                "agents": self.public_catalog(),
                "capability_graph": self.capability_graph.signature(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


agent_registry = AgentRegistry.from_manifest(
    Path(__file__).with_name("agent_manifest.json")
)
