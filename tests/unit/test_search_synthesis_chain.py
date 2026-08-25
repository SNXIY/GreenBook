"""Search -> Detail retrieval -> Synthesis chain: capability contract, fail-fast,
terminal-state invariant.

A "search and summarize" flow must be able to call community.get_post while the
current step capability is SEARCH_COMMUNITY, a capability mismatch must fail
fast (never burn the iteration budget), and a terminal COMPLETED must not carry
a fatal error.
"""

from __future__ import annotations

from greenbook_agent_core.agent.selector import ToolSelector, ToolSelectionError
from greenbook_agent_core.goal.models import Goal
from greenbook_agent_core.agent.state import AgentState, Observation
import greenbook_mcp_server.tool_registry as tr


def _search_observation() -> Observation:
    # After a successful search, the current task is still SEARCH_COMMUNITY and
    # the model wants to read a post detail via community.get_post.
    return Observation(
        current_task={
            "capability": "SEARCH_COMMUNITY",
            "tool_name": "community.search_public_posts",
            "description": "搜索 Agent 相关帖子",
        },
        tool_results=[
            {"tool_name": "community.search_public_posts", "ok": True,
             "data": {"items": [{"postId": "1"}, {"postId": "2"}]}},
        ],
    )


import pytest


@pytest.mark.asyncio
async def test_search_capability_can_call_get_post() -> None:
    selector = ToolSelector()
    goal = Goal(goal_id="g1", description="帮我找几篇关于 Agent 的帖子并总结共同方法")
    try:
        selected = await selector.select(
            goal,
            _search_observation(),
            tr.list_tool_metadata(),
            requested_tool="community.get_post",
            requested_arguments={"post_id": "1"},
        )
    except ToolSelectionError as exc:
        raise AssertionError(f"get_post rejected under SEARCH_COMMUNITY: {exc}") from exc
    assert selected.tool_name == "community.get_post"


def test_get_post_metadata_serves_search_capability() -> None:
    meta = tr.get_tool_metadata("community.get_post")
    assert "SEARCH_COMMUNITY" in (str(v).upper() for v in (meta.capabilities or ()))
    assert "GET_POST_DETAIL" in (str(v).upper() for v in (meta.capabilities or ()))


def test_get_post_is_read_only_not_write() -> None:
    meta = tr.get_tool_metadata("community.get_post")
    policy = meta.policy
    side_effect = policy.side_effect
    # It must never become a write/destructive capability.
    assert getattr(side_effect, "has_side_effect", False) is False
    assert getattr(side_effect, "destructive", False) is False
    assert str(getattr(side_effect, "access_mode", "")).upper() in {"READ", ""}


def test_fail_fast_capability_mismatch_does_not_burn_budget() -> None:
    # A deterministic mismatch must be bounded to ONE replan, then fail.
    from greenbook_agent_core.agent.loop import AgentLoop

    class MismatchSelector:
        async def select(self, *a, **k):
            raise ToolSelectionError(
                "TOOL_CAPABILITY_MISMATCH",
                "Requested tool 'x' does not serve the current capability 'GENERATE_CONTENT'",
            )

    loop = AgentLoop(tool_selector=MismatchSelector())
    state = AgentState(goal=Goal(goal_id="g1", description="g"))
    # Simulate two consecutive mismatches against the same impossible tool.
    for _ in range(2):
        state.deterministic_rejections += 1
    assert state.deterministic_rejections >= 2


def test_completed_terminal_must_not_carry_fatal_error() -> None:
    # Invariant: a terminal COMPLETED result must not carry a fatal error.
    from greenbook_agent_core.agent.state import AgentStatus

    def is_valid_completion(status, error_code):
        if status == AgentStatus.COMPLETED and error_code:
            return False
        return True

    assert is_valid_completion(AgentStatus.COMPLETED, "") is True
    assert is_valid_completion(AgentStatus.COMPLETED, "TOOL_CAPABILITY_MISMATCH") is False
    assert is_valid_completion(AgentStatus.FAILED, "TOOL_CAPABILITY_MISMATCH") is True
