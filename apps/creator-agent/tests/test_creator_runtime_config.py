from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.creator.runtime.models import BudgetLimits
from app.creator.runtime.runtime import LangGraphCreatorRuntime


def _runtime() -> LangGraphCreatorRuntime:
    runtime = object.__new__(LangGraphCreatorRuntime)
    runtime._limits = BudgetLimits()
    return runtime


def _root_graph():
    class ProbeState(TypedDict):
        value: str

    def probe(state: ProbeState) -> dict:
        return state

    builder = StateGraph(ProbeState)
    builder.add_node("probe", probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_root_graph_config_uses_business_thread_id_only() -> None:
    runtime = _runtime()

    config = runtime._config("r2-config-test")

    assert config["configurable"]["thread_id"] == "creator:r2-config-test"
    assert "checkpoint_ns" not in config["configurable"]


@pytest.mark.anyio
async def test_new_run_preflight_reads_root_graph_state() -> None:
    runtime = _runtime()
    config = runtime._config("r2-preflight-test")

    snapshot = await _root_graph().aget_state(config)

    assert snapshot.values == {}
    assert snapshot.next == ()


@pytest.mark.anyio
async def test_same_root_config_reads_state_after_ainvoke() -> None:
    class ProbeState(TypedDict):
        value: str
        visited: list[str]

    async def probe(state):
        return {"value": state["value"], "visited": ["probe"]}

    builder = StateGraph(ProbeState)
    builder.add_node("probe", probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "creator:r2-state-test"}}

    result = await graph.ainvoke({"value": "r2", "visited": []}, config=config)
    snapshot = await graph.aget_state(config)
    history = [item async for item in graph.aget_state_history(config)]

    assert result == {"value": "r2", "visited": ["probe"]}
    assert snapshot.values == result
    assert history


@pytest.mark.anyio
async def test_nonempty_business_namespace_is_rejected_by_langgraph() -> None:
    runtime = _runtime()
    invalid_config = {
        "configurable": {
            "thread_id": "creator:r2-invalid-namespace",
            "checkpoint_ns": "creator:r2-invalid-namespace",
        }
    }

    with pytest.raises(ValueError, match="Subgraph creator not found"):
        await _root_graph().aget_state(invalid_config)
