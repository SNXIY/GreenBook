"""Semantic conversation goal graph construction.

The graph builder consumes IntentSpec-shaped proposals.  It deliberately does
not infer boundaries from words such as "then" or "also"; boundary detection
is delegated to the semantic provider and dependencies are explicit graph
edges.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .intent_models import ActionType, IntentSpec


class ConversationGoalNode(BaseModel):
    node_id: str
    text: str = ""
    goal: str = ""
    intent: IntentSpec
    depends_on: list[str] = []
    read_only: bool = False
    create_task: bool = True
    artifact_inputs: list[str] = []
    artifact_outputs: list[str] = []


class ConversationTaskGraph(BaseModel):
    nodes: list[ConversationGoalNode] = []
    edges: list[tuple[str, str]] = []

    def validate_graph(self) -> None:
        node_ids = {node.node_id for node in self.nodes}
        for node in self.nodes:
            missing = [dep for dep in node.depends_on if dep not in node_ids]
            if missing:
                raise ValueError(f"Unknown graph dependency: {missing[0]}")
        self._topological_order()

    def topological_order(self) -> list[ConversationGoalNode]:
        return self._topological_order()

    def _topological_order(self) -> list[ConversationGoalNode]:
        by_id = {node.node_id: node for node in self.nodes}
        incoming = {node.node_id: set(node.depends_on) for node in self.nodes}
        ordered: list[ConversationGoalNode] = []
        while incoming:
            ready = [node_id for node_id, deps in incoming.items() if not deps]
            if not ready:
                raise ValueError("ConversationTaskGraph contains a dependency cycle")
            for node_id in ready:
                ordered.append(by_id[node_id])
                incoming.pop(node_id)
            for deps in incoming.values():
                deps.difference_update(ready)
        return ordered


@dataclass(frozen=True, slots=True)
class GraphProposal:
    """Provider-neutral semantic graph proposal."""

    text: str
    intent: IntentSpec
    depends_on: tuple[int | str, ...] = ()
    artifact_inputs: tuple[str, ...] = ()
    artifact_outputs: tuple[str, ...] = ()


class TaskGraphBuilder:
    """Build a validated graph from an Intent/Goal analyzer.

    ``resolve_graph`` is the preferred provider API.  A provider that only
    implements ``resolve`` remains compatible and produces one semantic node;
    the optional explicit legacy splitter is isolated as a compatibility
    fallback and is never used by the semantic graph analyzer itself.
    """

    def __init__(self, intent_provider: Any, *, legacy_splitter: Any = None) -> None:
        self._provider = intent_provider
        self._legacy_splitter = legacy_splitter

    async def build(
        self,
        message: str,
        *,
        existing_tasks: Sequence[Mapping[str, str]] | None = None,
    ) -> ConversationTaskGraph:
        proposals: list[GraphProposal] = []
        resolver = getattr(self._provider, "resolve_graph", None)
        if callable(resolver):
            raw = await resolver(message, existing_tasks=existing_tasks)
            proposals = self._normalize_proposals(raw, message)

        if not proposals and self._legacy_splitter is not None:
            for segment in self._legacy_splitter(message):
                intent = await self._provider.resolve(
                    segment.text,
                    existing_tasks=existing_tasks,
                )
                proposals.append(GraphProposal(segment.text, intent))

        if not proposals:
            intent = await self._provider.resolve(
                message,
                existing_tasks=existing_tasks,
            )
            proposals = [GraphProposal(message, intent)]

        nodes: list[ConversationGoalNode] = []
        for index, proposal in enumerate(proposals):
            node_id = f"goal-{index + 1}"
            dependencies = [
                self._dependency_id(value, index)
                for value in proposal.depends_on
            ]
            read_only = _is_read_only(proposal.intent)
            nodes.append(ConversationGoalNode(
                node_id=node_id,
                text=proposal.text,
                goal=proposal.intent.goal,
                intent=proposal.intent,
                depends_on=dependencies,
                read_only=read_only,
                create_task=not read_only,
                artifact_inputs=list(proposal.artifact_inputs),
                artifact_outputs=list(proposal.artifact_outputs),
            ))

        graph = ConversationTaskGraph(
            nodes=nodes,
            edges=[(dependency, node.node_id) for node in nodes for dependency in node.depends_on],
        )
        graph.validate_graph()
        return graph

    @staticmethod
    def _dependency_id(value: int | str, current_index: int) -> str:
        if isinstance(value, int) or str(value).isdigit():
            index = int(value)
            if index < 0 or index >= current_index:
                raise ValueError("A graph dependency must reference a previous goal")
            return f"goal-{index + 1}"
        value = str(value)
        return value if value.startswith("goal-") else f"goal-{value}"

    @staticmethod
    def _normalize_proposals(raw: Any, message: str) -> list[GraphProposal]:
        if isinstance(raw, ConversationTaskGraph):
            return [
                GraphProposal(
                    text=node.text or node.goal,
                    intent=node.intent,
                    depends_on=tuple(node.depends_on),
                    artifact_inputs=tuple(node.artifact_inputs),
                    artifact_outputs=tuple(node.artifact_outputs),
                )
                for node in raw.nodes
            ]
        if isinstance(raw, Mapping):
            raw = raw.get("goals") or raw.get("nodes") or []
        if isinstance(raw, IntentSpec):
            return [GraphProposal(message, raw)]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []

        proposals: list[GraphProposal] = []
        for item in raw:
            if isinstance(item, GraphProposal):
                proposals.append(item)
                continue
            if isinstance(item, IntentSpec):
                proposals.append(GraphProposal(item.goal or message, item))
                continue
            if not isinstance(item, Mapping):
                continue
            intent_raw = item.get("intent") or item.get("intent_spec") or item
            intent = IntentSpec.model_validate(intent_raw)
            dependencies = item.get("depends_on") or item.get("dependencies") or ()
            proposals.append(GraphProposal(
                text=str(item.get("text") or item.get("source_text") or intent.goal or message),
                intent=intent,
                depends_on=tuple(dependencies),
                artifact_inputs=tuple(str(value) for value in item.get("artifact_inputs", ()) or ()),
                artifact_outputs=tuple(str(value) for value in item.get("artifact_outputs", ()) or ()),
            ))
        return proposals


def _is_read_only(intent: IntentSpec) -> bool:
    if not intent.actions:
        return True
    read_actions = {ActionType.QUERY, ActionType.SEARCH, ActionType.ANALYZE}
    return all(action.action in read_actions for action in intent.actions)


__all__ = [
    "ConversationGoalNode",
    "ConversationTaskGraph",
    "GraphProposal",
    "TaskGraphBuilder",
]
