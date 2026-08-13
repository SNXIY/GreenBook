"""Phase 3 AgentLoop, metadata selection, and reflection tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentAction, AgentActionType, AgentLoop
from greenbook_agent_core.agent.state import AgentState, Observation
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_contracts.tool_contract import ToolMetadata, ToolPolicyMetadata


class _LLM:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.payloads:
            raise AssertionError("Fake LLM received more calls than expected")
        payload = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
            )],
        )


def _tool(name: str = "community.search_public_posts") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description="Search public GreenBook community posts",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        policy=ToolPolicyMetadata(risk_level="READ"),
    )


def _tree(*, composite: bool = False) -> GoalTree:
    if not composite:
        return GoalTree(root=Goal(
            goal_id="research_ai",
            description="搜索最近 AI 文章",
            goal_type="RESEARCH",
            required_capabilities=["SEARCH_COMMUNITY"],
        ))
    return GoalTree(root=Goal(
        goal_id="write_ai_article",
        description="分析热门文章然后写文章",
        goal_type="CREATE",
        children=[
            Goal(
                goal_id="research_ai",
                description="搜索最近 AI 文章",
                goal_type="RESEARCH",
                required_capabilities=["SEARCH_COMMUNITY"],
            ),
            Goal(
                goal_id="generate_article",
                description="根据热门文章写文章",
                goal_type="CREATE",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["research_ai"],
            ),
        ],
    ))


@pytest.mark.asyncio
async def test_reason_normalizes_nullable_json_mode_defaults() -> None:
    llm = _LLM(
        {
            "action": "TOOL_CALL",
            "tool_name": "content.get_draft",
            "tool_args": {},
            "goal_tree": None,
            "plan_patch": None,
            "question": None,
            "reason": "确认草稿",
            "confidence": 0.8,
        },
    )
    state = AgentState(
        goal=_tree().root_goal,
        command=Command(type=CommandType.QUERY, objective="查看草稿"),
        goal_tree=_tree(),
    )

    action = await AgentLoop(llm=llm).reason(Observation(), state)

    assert action.action == AgentActionType.TOOL_CALL
    assert action.plan_patch == {}
    assert action.question == ""


@pytest.mark.asyncio
async def test_agent_selects_search_tool_from_metadata() -> None:
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {
            "tool_name": "community.search_public_posts",
            "arguments": {"query": "AI"},
            "reason": "The metadata describes community search.",
            "confidence": 0.95,
        },
        {"finished": True, "needs_next_step": False, "reason": "Search completed."},
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def raw_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, args))
        return {"ok": True, "data": {"items": [{"title": "AI trend"}]}}

    result = await AgentLoop(llm=llm, max_iterations=2).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _tree(),
        available_tools=[_tool()],
        tool_runtime=ToolRuntime(raw_tool, ledger=ToolExecutionLedger()),
    )

    assert result.success is True
    assert calls == [("community.search_public_posts", {"query": "AI"})]
    assert result.actions[0]["action"] == AgentActionType.TOOL_CALL.value
    assert len(llm.calls) == 3  # Reason -> Selector -> Reflect


@pytest.mark.asyncio
async def test_reflection_ignores_provider_schema_metadata_only() -> None:
    llm = _LLM({
        "finished": True,
        "needs_next_step": False,
        "reason": "The observation is sufficient.",
        "additionalProperties": False,
    })
    state = AgentState(
        goal=_tree().root_goal,
        command=Command(type=CommandType.QUERY, objective="Search AI posts"),
        goal_tree=_tree(),
    )

    reflection = await AgentLoop(llm=llm).reflect(
        Observation(),
        AgentAction(action=AgentActionType.TOOL_CALL),
        {"ok": True},
        state,
    )

    assert reflection.finished is True
    assert reflection.needs_next_step is False


@pytest.mark.asyncio
async def test_agent_searches_then_creates_next_execution_task() -> None:
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {"tool_name": "community.search_public_posts", "arguments": {"query": "AI"}},
        {"finished": False, "needs_next_step": True, "reason": "Use the research for writing."},
        {"action": "CREATE_TASK", "reason": "Compile the remaining writing Goal."},
        {"finished": True, "needs_next_step": False, "reason": "The task was submitted."},
    )
    tool_calls: list[str] = []
    executions: list[tuple[str, int]] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(name)
        return {"ok": True, "data": {"items": [{"title": "AI trend"}]}}

    async def execution_runtime(*, graph: Any, plan: Any, state: Any) -> dict[str, Any]:
        executions.append((plan.plan_source, len(graph.nodes)))
        assert state.goal_tree is not None
        return {
            "ok": True,
            "success": True,
            "status": "COMPLETED",
            "task_id": "task-ai-article",
            "execution_id": "execution-ai-article",
        }

    result = await AgentLoop(llm=llm, max_iterations=4).run(
        Command(type=CommandType.CREATE, objective="分析热门文章然后写文章"),
        _tree(composite=True),
        available_tools=[_tool()],
        tool_runtime=tool_runtime,
        execution_runtime=execution_runtime,
    )

    assert result.success is True
    assert tool_calls == ["community.search_public_posts"]
    assert executions == [("GOAL_RUNTIME", 2)]
    assert [item["action"] for item in result.actions] == [
        AgentActionType.TOOL_CALL.value,
        AgentActionType.CREATE_TASK.value,
    ]
    assert result.execution_results[-1]["execution_id"] == "execution-ai-article"


@pytest.mark.asyncio
async def test_queued_task_handoff_stops_agent_loop_without_duplicate_submission() -> None:
    llm = _LLM(
        {"action": "CREATE_TASK", "reason": "Submit the writing plan."},
        # This response must not be consumed: queue acceptance is the runtime
        # hand-off boundary, so the AgentLoop must not ask for another action.
        {"finished": False, "needs_next_step": True, "reason": "Submit again."},
    )

    async def execution_runtime(*, graph: Any, plan: Any, state: Any) -> dict[str, Any]:
        del graph, plan, state
        return {
            "status": "QUEUED",
            "execution_id": "execution-queued",
            "task_id": "task-queued",
        }

    result = await AgentLoop(llm=llm, max_iterations=3).run(
        Command(type=CommandType.CREATE, objective="写一篇 Agent 文章"),
        _tree(),
        execution_runtime=execution_runtime,
    )

    assert result.success is False
    assert result.status.value == "RUNNING"
    assert result.execution_results[-1]["execution_id"] == "execution-queued"
    assert [item["action"] for item in result.actions] == [
        AgentActionType.CREATE_TASK.value,
    ]
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_failed_tool_causes_agent_to_update_goal_plan() -> None:
    updated_tree = _tree()
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {"tool_name": "community.search_public_posts", "arguments": {"query": "AI"}},
        {"finished": False, "needs_next_step": True, "retry": True, "reason": "Retry with a revised plan."},
        {
            "action": "UPDATE_PLAN",
            "goal_tree": updated_tree.model_dump(mode="json"),
            "reason": "The search tool failed; keep the Goal explicit.",
        },
        {"finished": False, "needs_next_step": True, "reason": "Plan updated."},
        {"action": "FINISH", "reason": "Waiting for the next user turn."},
    )

    async def failing_tool(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "TIMEOUT",
            "retryable": True,
            "message": "Search timed out",
        }

    result = await AgentLoop(llm=llm, max_iterations=5).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _tree(),
        available_tools=[_tool()],
        tool_runtime=failing_tool,
    )

    assert result.success is True
    assert [item["action"] for item in result.actions] == [
        AgentActionType.TOOL_CALL.value,
        AgentActionType.UPDATE_PLAN.value,
        AgentActionType.FINISH.value,
    ]
    assert result.tool_results[0]["error_code"] == "TIMEOUT"
    assert result.state is not None
    assert result.state.goal_tree is not None
