from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph


class ProbeState(TypedDict):
    value: str
    visited: list[str]


async def probe(state: ProbeState) -> dict:
    print("PROBE_NODE_STARTED", flush=True)
    return {
        "value": state["value"],
        "visited": [*state.get("visited", []), "probe"],
    }


@pytest.mark.asyncio
async def test_minimal_graph_direct_ainvoke() -> None:
    builder = StateGraph(ProbeState)
    builder.add_node("probe", probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)

    graph = builder.compile()

    print("GRAPH_MERMAID", flush=True)
    print(graph.get_graph().draw_mermaid(), flush=True)

    result = await graph.ainvoke(
        {
            "value": "hello",
            "visited": [],
        }
    )

    print("GRAPH_RESULT", result, flush=True)

    assert result["value"] == "hello"
    assert result["visited"] == ["probe"]
