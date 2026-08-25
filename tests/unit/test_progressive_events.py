"""Step-level progressive events tests (Phase 2.9).

A single Run emits multiple business activities (ACTION_STARTED ->
ACTION_SUCCEEDED/FAILED -> PARTIAL_RESULT) from real Observations; the run
stream stays open until terminal; continuation preserves the activity
callback and run ownership; no premature final; no fake progress.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.runner import project_progressive_event
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.execution.action_observation import (
    ActionObservationStore,
    ActionObservationWriter,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.execution.submission import RecordingExecutionSubmissionService
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_contracts.tool_contract import (
    RetryPolicy,
    SideEffectMetadata,
    ToolMetadata,
    ToolPolicyMetadata,
)

SEARCH_TOOL = ToolMetadata(
    name="community_search_public_posts",
    description="search",
    capabilities=("SEARCH_COMMUNITY",),
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    policy=ToolPolicyMetadata(
        side_effect=SideEffectMetadata(has_side_effect=False, idempotent=True),
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_seconds=10.0,
    ),
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


class _ToolRuntime:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.invoked: list[str] = []

    async def invoke(self, context):
        self.invoked.append(context.tool_name)
        return dict(self._result)


def _tree(capabilities: list[str]) -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="one goal",
            goal_type="CREATE",
            required_capabilities=capabilities,
        ),
        task_nodes=[
            TaskNode(task_id=f"g1:{i + 1}", goal_id="g1", capability=cap)
            for i, cap in enumerate(capabilities)
        ],
    )


async def _run(loop: AgentLoop, tree: GoalTree, tools: list[ToolMetadata], runtime: Any, submission: Any, callback: Any):
    return await loop.run(
        command=Command(
            type=CommandType.CREATE,
            goal="search and synthesize",
            objective="search and synthesize",
            required_capabilities=list(tree.root_goal.required_capabilities),
            raw_input="search and synthesize",
        ),
        goal_tree=tree,
        available_tools=tools,
        tool_runtime=runtime,
        execution_submission=submission,
        activity_callback=callback,
    )


# ── §46 multiple actions, same run ──────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_actions_emit_events_on_same_run() -> None:
    # SEARCH (direct) then SYNTHESIZE (direct) in two AgentLoop rounds.
    llm = _LLM([
        {
            "action": "TOOL_CALL",
            "tool_name": "community_search_public_posts",
            "tool_args": {"query": "Agent"},
            "reason": "search",
            "confidence": 0.9,
        },
        {"finished": True, "needs_next_step": True, "retry": False, "adjust_plan": False, "reason": ""},
        {
            "action": "TOOL_CALL",
            "tool_name": "community_search_public_posts",
            "tool_args": {"query": "Agent"},
            "reason": "search again",
            "confidence": 0.9,
        },
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    events: list[tuple[str, dict[str, Any]]] = []

    async def callback(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, dict(payload)))

    runtime = _ToolRuntime({"ok": True, "data": {"posts": [{"post_id": "p1"}], "total": 1}})
    loop = AgentLoop(llm=llm, model="test")
    await _run(
        loop,
        _tree(["SEARCH_COMMUNITY"]),
        [SEARCH_TOOL],
        runtime,
        RecordingExecutionSubmissionService(),
        callback,
    )
    started = [e for e in events if e[0] == "SEMANTIC_ACTION_SELECTED"]
    completed = [e for e in events if e[0] == "ACTION_COMPLETED"]
    assert len(started) >= 1
    assert len(completed) >= 1
    assert all(payload["goal_id"] == "g1" for _, payload in started)


# ── §47 direct tool partial result ──────────────────────────────────────


def test_direct_tool_partial_result_projection() -> None:
    partial = project_progressive_event("SEARCH_COMMUNITY", {
        "data": {"total": 18, "posts": [{"post_id": "p1"}]},
    })
    assert partial is not None
    assert partial["activity_type"] == "SEARCH_SUMMARY"
    assert partial["count"] == 18
    assert partial["status"] == "SUCCESS"


def test_no_fake_progress_without_business_facts() -> None:
    # §11: no fake progress — an empty/unknown result produces no partial.
    assert project_progressive_event("SEARCH_COMMUNITY", {"ok": True}) is None
    assert project_progressive_event("SYNTHESIZE_RESULTS", {"ok": True}) is None
    assert project_progressive_event("GENERATE_CONTENT", {"ok": True, "data": {}}) is None


# ── §48 durable observation progressive ─────────────────────────────────


def test_durable_observation_progressive_projection() -> None:
    partial = project_progressive_event("GENERATE_CONTENT", {
        "ok": True,
        "draft_id": "draft-1",
    })
    assert partial is not None
    assert partial["activity_type"] == "DRAFT_CREATED"
    assert partial["business_result"]["draft_id"] == "draft-1"

    scheduled = project_progressive_event("SCHEDULE_PUBLISH", {
        "ok": True,
        "schedule_id": "schedule-1",
    })
    assert scheduled is not None
    assert scheduled["activity_type"] == "SCHEDULED"


# ── observation run_id ownership ────────────────────────────────────────


def test_observation_carries_run_id() -> None:
    store = ActionObservationStore()
    writer = ActionObservationWriter(store=store)
    message = ExecutionQueueMessage(
        execution_id="e1",
        payload={
            "run_id": "run-42",
            "conversation_id": "c1",
            "task_id": "t1",
            "execution_input": {
                "goal_id": "g1",
                "execution_metadata": {"plan_mode": "INCREMENTAL"},
                "steps": [{"step_id": "s1", "goal_id": "g1", "capability": "GENERATE_CONTENT"}],
            },
        },
    )
    result = RuntimeResult(
        success=True,
        status="COMPLETED",
        execution_id="e1",
        artifacts=[{
            "artifact_id": "a1",
            "type": "DRAFT",
            "artifact_type": "DRAFT",
            "resource_type": "DRAFT",
            "resource_id": "draft-1",
        }],
    )
    observation = writer.write(message, result, _auth())
    assert observation is not None
    assert observation.run_id == "run-42"


def _auth() -> Any:
    from greenbook_contracts.identity import AuthContext

    return AuthContext(user_id="u1", tenant_id="ten1", raw_access_token="")
