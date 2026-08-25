"""Phase 4.3 focused tests: generic duplicate-read guard, loop detection, and
premature-FINISH rejection (design 0813)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.agent.state import AgentState
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_contracts.tool_contract import ToolMetadata, ToolPolicyMetadata


class _LLM:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"Fake LLM received more calls than expected: {len(self.calls)}")
        payload = self.responses.pop(0)
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def _search_tool() -> ToolMetadata:
    return ToolMetadata(
        name="community.search_public_posts",
        description="Search public GreenBook community posts",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        capabilities=["SEARCH_COMMUNITY"],
        policy=ToolPolicyMetadata(risk_level="READ"),
    )


def _single_tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="research_ai",
        description="搜索最近 AI 文章",
        goal_type="RESEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
    ))


def _multi_tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="root",
        description="搜索并写文章",
        goal_type="CREATE",
        children=[
            Goal(
                goal_id="g2",
                description="搜索社区 Agent 帖子",
                goal_type="RESEARCH",
                required_capabilities=["SEARCH_COMMUNITY"],
            ),
            Goal(
                goal_id="g4",
                description="根据观点写一篇文章",
                goal_type="CREATE",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["g2"],
                publication_intent="DRAFT_ONLY",
            ),
        ],
    ))


def _select_payload() -> dict[str, Any]:
    return {
        "tool_name": "community.search_public_posts",
        "arguments": {"query": "AI"},
        "reason": "Search community posts.",
        "confidence": 0.95,
    }


def _search_reason() -> dict[str, Any]:
    return {"action": "TOOL_CALL", "tool_args": {"query": "AI"}, "reason": "Search."}


def _continue_reflect() -> dict[str, Any]:
    return {"finished": False, "needs_next_step": True, "retry": False, "reason": "Continue."}


# ── duplicate guard (isolated _invoke_tool) ────────────────────────────────


def _loop_with_prior_read() -> AgentLoop:
    return AgentLoop(llm=_LLM())


def _state_with_success(*, query: str = "AI", empty: bool = False) -> AgentState:
    return AgentState(
        goal=_single_tree().root_goal,
        goal_tree=_single_tree(),
        command=Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        available_tools=[_search_tool()],
        tool_results=[
            {
                "ok": True,
                "tool_name": "community.search_public_posts",
                "tool_arguments": {"query": query},
                "data": {"items": [] if empty else [{"post_id": "p1"}]},
            }
        ],
    )


async def _invoke(
    loop: AgentLoop,
    state: AgentState,
    *,
    query: str,
) -> tuple[dict[str, Any], list[str]]:
    calls: list[str] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"ok": True, "data": {"items": [{"post_id": "fresh"}]}}

    result = await loop._invoke_tool(
        "community.search_public_posts",
        {"query": query},
        state,
        ToolRuntime(tool_runtime, ledger=ToolExecutionLedger()),
        metadata=_search_tool(),
        permission_context=None,
        permission_scopes=(),
        approval_granted=False,
        tool_submission=None,
        execution_runtime=None,
    )
    return result, calls


@pytest.mark.asyncio
async def test_same_scope_read_is_rejected() -> None:
    result, calls = await _invoke(
        _loop_with_prior_read(),
        _state_with_success(query="AI"),
        query="AI",
    )
    assert result["error_code"] == "EQUIVALENT_ACTION_ALREADY_SUCCEEDED"
    assert result["ok"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_changed_query_is_allowed() -> None:
    result, calls = await _invoke(
        _loop_with_prior_read(),
        _state_with_success(query="AI"),
        query="AI agents",
    )
    assert "EQUIVALENT_ACTION_ALREADY_SUCCEEDED" not in result.get("error_code", "")
    assert calls == ["community.search_public_posts"]


@pytest.mark.asyncio
async def test_empty_prior_result_allows_reread() -> None:
    result, calls = await _invoke(
        _loop_with_prior_read(),
        _state_with_success(query="AI", empty=True),
        query="AI",
    )
    assert "EQUIVALENT_ACTION_ALREADY_SUCCEEDED" not in result.get("error_code", "")
    assert calls == ["community.search_public_posts"]


@pytest.mark.asyncio
async def test_rejection_feeds_back_and_alternative_finishes() -> None:
    llm = _LLM(
        _search_reason(),          # 1 reason: search
        _select_payload(),         # 2 selector
        _continue_reflect(),       # 3 reflect
        _search_reason(),          # 4 reason: same search (duplicate)
        _select_payload(),         # 5 selector
        _continue_reflect(),       # 6 reflect
        {"action": "FINISH", "reason": "Evidence sufficient."},  # 7 reason: finish
    )
    tool_calls: list[str] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(name)
        return {"ok": True, "data": {"items": [{"post_id": "p1"}]}}

    result = await AgentLoop(llm=llm, max_iterations=4).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _single_tree(),
        available_tools=[_search_tool()],
        tool_runtime=ToolRuntime(tool_runtime, ledger=ToolExecutionLedger()),
    )

    assert result.success is True
    assert tool_calls == ["community.search_public_posts"]
    rejected = [item for item in result.tool_results
                if item.get("error_code") == "EQUIVALENT_ACTION_ALREADY_SUCCEEDED"]
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_loop_detection_after_repeated_equivalent_read() -> None:
    llm = _LLM(
        _search_reason(),
        _select_payload(),
        _continue_reflect(),
        _search_reason(),
        _select_payload(),
        _continue_reflect(),
        _search_reason(),
        _select_payload(),
        _continue_reflect(),
        {"action": "FINISH", "reason": "Stop."},
    )
    tool_calls: list[str] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(name)
        return {"ok": True, "data": {"items": [{"post_id": "p1"}]}}

    result = await AgentLoop(llm=llm, max_iterations=5).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _single_tree(),
        available_tools=[_search_tool()],
        tool_runtime=ToolRuntime(tool_runtime, ledger=ToolExecutionLedger()),
    )

    assert tool_calls == ["community.search_public_posts"]
    rejections = [
        item.get("error_code")
        for item in result.tool_results
        if item.get("error_code")
    ]
    assert rejections == [
        "EQUIVALENT_ACTION_ALREADY_SUCCEEDED",
        "LOOP_DETECTED",
    ]


# ── premature FINISH rejection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finish_rejected_when_publication_goal_unsatisfied() -> None:
    llm = _LLM(
        _search_reason(),
        _select_payload(),
        _continue_reflect(),
        {"action": "FINISH", "reason": "Search is done, call it finished."},  # rejected
        _continue_reflect(),
        {"action": "FINISH", "reason": "Still unfinished."},  # rejected -> controlled error
    )
    tool_calls: list[str] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(name)
        return {"ok": True, "data": {"items": [{"post_id": "p1"}]}}

    result = await AgentLoop(llm=llm, max_iterations=5).run(
        Command(type=CommandType.CREATE, objective="搜索并写文章"),
        _multi_tree(),
        available_tools=[_search_tool()],
        tool_runtime=ToolRuntime(tool_runtime, ledger=ToolExecutionLedger()),
    )

    assert result.success is False
    assert result.error_code == "GOAL_NOT_SATISFIED"
    rejections = [item for item in result.tool_results
                  if item.get("error_code") == "GOAL_NOT_SATISFIED"]
    assert len(rejections) == 1
    # No raw technical error surfaced.
    assert "invalid Agent JSON" not in result.error_message
    assert "unsatisfied" not in result.error_message
    assert result.error_message == "这一步没有顺利完成，我暂时无法继续执行。你可以让我重试。"


# ── durable read evidence in runtime constraints ───────────────────────────


def test_consumed_read_evidence_includes_durable_execution() -> None:
    from greenbook_agent_core.agent.loop import _read_evidence_constraints

    state = AgentState(
        goal=_single_tree().root_goal,
        goal_tree=_single_tree(),
        command=Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        available_tools=[_search_tool()],
        context_snapshot={
            "execution_states": [
                {
                    "goal_id": "research_ai",
                    "capability": "SEARCH_COMMUNITY",
                    "status": "COMPLETED",
                }
            ]
        },
    )
    constraints = _read_evidence_constraints(state)
    evidence = constraints["consumed_read_evidence"]
    assert any(
        item["tool_name"] == "community.search_public_posts"
        and item.get("source") == "EXECUTION_EVIDENCE"
        for item in evidence
    )


@pytest.mark.asyncio
async def test_goal_states_finish_validation_uses_publication_intent() -> None:
    loop = AgentLoop(llm=_LLM())
    state = AgentState(
        goal=_multi_tree().root_goal,
        goal_tree=_multi_tree(),
        context_snapshot={"execution_states": []},
    )
    # g4 (DRAFT_ONLY) has no draft -> FINISH must be rejected.
    assert loop._goal_tree_finished_ok(state) is False

    state.context_snapshot = {
        "execution_states": [
            {
                "goal_id": "g4",
                "capability": "GENERATE_CONTENT",
                "status": "COMPLETED",
                "draft_id": "draft-1",
            }
        ]
    }
    assert loop._goal_tree_finished_ok(state) is True
