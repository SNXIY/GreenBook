from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain import AgentPlan


class DagState(TypedDict):
    completed: Annotated[list[str], operator.add]


def compile_task_graph(plan: AgentPlan):
    """Compile the LLM-produced DAG into a LangGraph topology.

    PostgreSQL remains the authoritative durable checkpointer because it also
    binds leases, approvals and side effects. LangGraph supplies graph
    validation, parallel frontier semantics and an inspectable graph artifact.
    """

    builder = StateGraph(DagState)
    if not plan.steps:
        def finish_without_tools(state: DagState) -> dict[str, list[str]]:
            del state
            return {"completed": []}

        builder.add_node("__answer__", finish_without_tools)
        builder.add_edge(START, "__answer__")
        builder.add_edge("__answer__", END)
        return builder.compile()

    dependents: dict[str, set[str]] = {str(step.task_id): set() for step in plan.steps}
    for step in plan.steps:
        task_id = str(step.task_id)

        def mark_complete(
            state: DagState, *, current_task_id: str = task_id
        ) -> dict[str, list[str]]:
            del state
            return {"completed": [current_task_id]}

        builder.add_node(task_id, mark_complete)
        for dependency in step.depends_on:
            dependents[dependency].add(task_id)
            builder.add_edge(dependency, task_id)

    for step in plan.steps:
        task_id = str(step.task_id)
        if not step.depends_on:
            builder.add_edge(START, task_id)
        if not dependents[task_id]:
            builder.add_edge(task_id, END)
    return builder.compile()


def graph_descriptor(plan: AgentPlan) -> dict[str, Any]:
    graph = compile_task_graph(plan)
    return {
        "layers": (
            [
                [str(step.task_id) for step in layer]
                for layer in plan.execution_layers()
            ]
            if plan.steps
            else []
        ),
        "mermaid": graph.get_graph().draw_mermaid(),
    }
