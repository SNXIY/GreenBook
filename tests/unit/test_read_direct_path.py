"""Read-only direct path tests (Phase 2.7).

A read-only tool (no side effect, no approval, short timeout) executes
directly through ToolRuntime via the policy SYNC mode — no Execution, no
Queue. Side-effecting capabilities (SCHEDULE_PUBLISH, GENERATE_CONTENT) stay
on the Durable Runtime path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.execution.submission import RecordingExecutionSubmissionService
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_contracts.tool_contract import (
    RetryPolicy,
    SideEffectMetadata,
    ToolMetadata,
    ToolPolicyMetadata,
)


def _metadata(name: str, *, capabilities: tuple[str, ...], side_effect: bool) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description=name,
        capabilities=capabilities,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        policy=ToolPolicyMetadata(
            risk_level="WRITE" if side_effect else "READ",
            side_effect=SideEffectMetadata(has_side_effect=side_effect, idempotent=True),
            retry_policy=RetryPolicy(max_attempts=1 if not side_effect else 3),
            timeout_seconds=10.0 if not side_effect else 130.0,
        ),
    )


READ_TOOL = _metadata(
    "community_search_public_posts",
    capabilities=("SEARCH_COMMUNITY",),
    side_effect=False,
)
SCHEDULE_TOOL = _metadata(
    "publication_schedule",
    capabilities=("SCHEDULE_PUBLISH",),
    side_effect=True,
)


class _LLM:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.responses = list(payloads)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        payload = self.responses.pop(0) if self.responses else {"action": "FINISH", "reason": ""}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
        )])


def _tree(capabilities: list[str], *, intent: str = "") -> GoalTree:
    from greenbook_agent_core.goal.models import TaskNode

    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="one goal",
            goal_type="CREATE",
            publication_intent=intent,
            required_capabilities=capabilities,
            temporal_constraint={"run_at": "T"} if capabilities == ["SCHEDULE_PUBLISH"] else {},
        ),
        task_nodes=[
            TaskNode(task_id=f"g1:{i + 1}", goal_id="g1", capability=cap)
            for i, cap in enumerate(capabilities)
        ],
    )


class _ToolRuntime:
    def __init__(self) -> None:
        self.invoked: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, context):
        self.invoked.append((context.tool_name, dict(context.tool_args)))
        return {
            "ok": True,
            "data": {"posts": [{"post_id": "p1", "title": "T"}], "total": 1},
            "resource_refs": [],
        }


async def _run(loop: AgentLoop, tree: GoalTree, tools: list[ToolMetadata], runtime: Any, submission: Any):
    from greenbook_agent_core.command.models import Command, CommandType

    return await loop.run(
        command=Command(
            type=CommandType.CREATE,
            goal="one goal",
            objective="one goal",
            required_capabilities=list(tree.root_goal.required_capabilities),
            raw_input="one goal",
        ),
        goal_tree=tree,
        available_tools=tools,
        tool_runtime=runtime,
        execution_submission=submission,
    )


@pytest.mark.asyncio
async def test_read_only_tool_call_executes_directly_without_execution() -> None:
    llm = _LLM([
        {
            "action": "TOOL_CALL",
            "tool_name": "community_search_public_posts",
            "tool_args": {"query": "Agent Memory"},
            "reason": "search",
            "confidence": 0.9,
        },
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    runtime = _ToolRuntime()
    submission = RecordingExecutionSubmissionService()
    loop = AgentLoop(llm=llm, model="test")
    result = await _run(loop, _tree(["SEARCH_COMMUNITY"]), [READ_TOOL], runtime, submission)
    assert runtime.invoked, "read-only tool must be invoked directly"
    assert runtime.invoked[0][0] == "community_search_public_posts"
    assert submission.submissions == [], "read-only tool must not create a durable Execution"
    assert result.success is True
    # timing markers recorded for the first turn
    timings = getattr(getattr(result, "state", None), "timings", None) or {}
    assert "first_reason_started_at" in timings
    assert "first_action_decided_at" in timings


@pytest.mark.asyncio
async def test_side_effect_schedule_stays_durable() -> None:
    llm = _LLM([
        {
            "action": "CREATE_TASK",
            "tool_name": "",
            "tool_args": {},
            "reason": "schedule",
            "confidence": 0.9,
        },
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    runtime = _ToolRuntime()
    submission = RecordingExecutionSubmissionService()
    loop = AgentLoop(llm=llm, model="test")
    await _run(loop, _tree(["SCHEDULE_PUBLISH"]), [SCHEDULE_TOOL], runtime, submission)
    assert submission.submissions, "SCHEDULE_PUBLISH must create a durable Execution"
    assert runtime.invoked == [], "side-effecting action must not invoke the tool directly"


@pytest.mark.asyncio
async def test_creator_generate_stays_durable() -> None:
    llm = _LLM([
        {
            "action": "CREATE_TASK",
            "tool_name": "",
            "tool_args": {},
            "reason": "generate",
            "confidence": 0.9,
        },
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    runtime = _ToolRuntime()
    submission = RecordingExecutionSubmissionService()
    loop = AgentLoop(llm=llm, model="test")
    await _run(loop, _tree(["GENERATE_CONTENT"]), [READ_TOOL], runtime, submission)
    assert submission.submissions, "GENERATE_CONTENT must stay durable"
    assert runtime.invoked == []
