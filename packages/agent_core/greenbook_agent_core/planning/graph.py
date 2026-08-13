"""Execution graph contracts emitted by GoalCompiler.

This module contains graph data and validation only.  It does not understand
user messages and deliberately has no builder or resolver.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlanNode(BaseModel):
    node_id: str
    text: str = ""
    goal: str = ""
    capabilities: list[str] = Field(default_factory=list)
    constraints: list[dict[str, object]] = Field(default_factory=list)
    target_hint: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    read_only: bool = False
    create_task: bool = True
    artifact_inputs: list[str] = Field(default_factory=list)
    artifact_outputs: list[str] = Field(default_factory=list)


class PlanGraph(BaseModel):
    """Validated DAG projection consumed by compatibility execution callers."""

    nodes: list[PlanNode] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)

    def validate_graph(self) -> None:
        node_ids = {node.node_id for node in self.nodes}
        for node in self.nodes:
            missing = [dep for dep in node.depends_on if dep not in node_ids]
            if missing:
                raise ValueError(f"Unknown graph dependency: {missing[0]}")
        self.topological_order()

    def topological_order(self) -> list[PlanNode]:
        by_id = {node.node_id: node for node in self.nodes}
        incoming = {node.node_id: set(node.depends_on) for node in self.nodes}
        ordered: list[PlanNode] = []
        while incoming:
            ready = [node_id for node_id, deps in incoming.items() if not deps]
            if not ready:
                raise ValueError("PlanGraph contains a dependency cycle")
            for node_id in ready:
                ordered.append(by_id[node_id])
                incoming.pop(node_id)
            for deps in incoming.values():
                deps.difference_update(ready)
        return ordered


__all__ = ["PlanNode", "PlanGraph"]
