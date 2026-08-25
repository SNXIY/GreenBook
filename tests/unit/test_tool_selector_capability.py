"""ToolSelector capability-consistency: a requested tool that does not serve
the current semantic step must be refused (GENERATE_CONTENT calling
community.get_post previously executed and silently produced no draft)."""

from __future__ import annotations

import pytest
from greenbook_agent_core.agent.selector import ToolSelector
from greenbook_agent_core.agent.state import Observation
from greenbook_agent_core.goal.models import Goal
from greenbook_contracts.tool_contract import ToolMetadata


def _tool(name: str, capability: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description=name,
        capabilities=(capability,),
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
    )


_CATALOG = [
    _tool("community.search_public_posts", "SEARCH_COMMUNITY"),
    _tool("community.get_post", "GET_POST_DETAIL"),
    _tool("content.create_draft", "GENERATE_CONTENT"),
]


def _observation(capability: str) -> Observation:
    return Observation(current_task={"capability": capability, "task_id": "t1"})


@pytest.mark.asyncio
async def test_requested_tool_serving_current_capability_is_allowed() -> None:
    selector = ToolSelector()
    selected = await selector.select(
        Goal(goal_id="g1", description="x", required_capabilities=["GENERATE_CONTENT"]),
        _observation("GENERATE_CONTENT"),
        _CATALOG,
        requested_tool="content.create_draft",
        requested_arguments={"title": "t", "instruction": "i"},
    )
    assert selected.tool_name == "content.create_draft"


@pytest.mark.asyncio
async def test_requested_tool_mismatching_capability_is_refused() -> None:
    """Regression: GENERATE step requesting community.get_post."""
    from greenbook_agent_core.agent.selector import ToolSelectionError

    selector = ToolSelector()
    with pytest.raises(ToolSelectionError) as exc:
        await selector.select(
            Goal(goal_id="g1", description="x", required_capabilities=["GENERATE_CONTENT"]),
            _observation("GENERATE_CONTENT"),
            _CATALOG,
            requested_tool="community.get_post",
            requested_arguments={"post_id": "1"},
        )
    assert "does not serve the" in str(exc.value)
    assert "GENERATE_CONTENT" in str(exc.value)


@pytest.mark.asyncio
async def test_read_tool_matching_search_capability_still_works() -> None:
    selector = ToolSelector()
    selected = await selector.select(
        Goal(goal_id="g1", description="x", required_capabilities=["SEARCH_COMMUNITY"]),
        _observation("SEARCH_COMMUNITY"),
        _CATALOG,
        requested_tool="community.search_public_posts",
        requested_arguments={"query": "AI"},
    )
    assert selected.tool_name == "community.search_public_posts"


@pytest.mark.asyncio
async def test_mismatch_without_capability_context_is_not_blocked() -> None:
    """No current-task capability -> cannot check; keep legacy behaviour."""
    selector = ToolSelector()
    selected = await selector.select(
        None,
        Observation(current_task={}),
        _CATALOG,
        requested_tool="community.get_post",
        requested_arguments={"post_id": "1"},
    )
    assert selected.tool_name == "community.get_post"
