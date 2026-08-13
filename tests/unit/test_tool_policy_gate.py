"""Phase 4 ToolMetadata policy gate tests."""

from __future__ import annotations

import pytest
from greenbook_agent_core.agent import (
    AgentAction,
    AgentActionType,
    AgentLoop,
    Reflection,
    SelectedTool,
)
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.execution import RecordingExecutionSubmissionService
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import InMemoryTaskRepository, TaskManager
from greenbook_agent_core.toolruntime import (
    ToolExecutionMode,
    ToolPolicyGate,
)
from greenbook_contracts.tool_contract import (
    PermissionPolicy,
    RetryPolicy,
    SideEffectMetadata,
    ToolMetadata,
    ToolPolicyMetadata,
)


def _tool(**changes) -> ToolMetadata:
    values = {
        "name": "content.create_draft",
        "description": "Create a draft",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "policy": ToolPolicyMetadata(
            side_effect=SideEffectMetadata(has_side_effect=True, idempotent=True),
        ),
    }
    values.update(changes)
    return ToolMetadata(**values)


def test_permission_denial_is_code_owned() -> None:
    decision = ToolPolicyGate().evaluate(
        _tool(policy=ToolPolicyMetadata(
            permission=PermissionPolicy(required_scopes=("content:write",)),
        )),
    )
    assert decision.allowed is False
    assert decision.mode == ToolExecutionMode.DENY


def test_requires_approval_enters_waiting_human() -> None:
    decision = ToolPolicyGate().evaluate(
        _tool(policy=ToolPolicyMetadata(
            requires_approval=True,
            side_effect=SideEffectMetadata(has_side_effect=True),
        )),
    )
    assert decision.allowed is False
    assert decision.mode == ToolExecutionMode.WAITING_HUMAN


def test_side_effect_and_retry_policy_force_queue() -> None:
    decision = ToolPolicyGate().evaluate(
        _tool(policy=ToolPolicyMetadata(retry_policy=RetryPolicy(max_attempts=2))),
        approval_granted=True,
    )
    assert decision.allowed is True
    assert decision.mode == ToolExecutionMode.QUEUE
    assert decision.queue_required is True


def test_read_only_tool_can_run_synchronously() -> None:
    metadata = ToolMetadata(
        name="community.search_public_posts",
        description="Search posts",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    decision = ToolPolicyGate().evaluate(metadata)
    assert decision.allowed is True
    assert decision.mode == ToolExecutionMode.SYNC


def test_cost_budget_denies_before_execution() -> None:
    decision = ToolPolicyGate().evaluate(
        _tool(policy=ToolPolicyMetadata(cost=2.0)),
        max_cost=1.0,
    )
    assert decision.allowed is False
    assert decision.mode == ToolExecutionMode.DENY


def test_long_timeout_is_queue_only() -> None:
    decision = ToolPolicyGate().evaluate(
        _tool(policy=ToolPolicyMetadata(
            side_effect=SideEffectMetadata(has_side_effect=False),
            timeout_seconds=300.0,
        )),
    )
    assert decision.allowed is True
    assert decision.mode == ToolExecutionMode.QUEUE


@pytest.mark.asyncio
async def test_agent_action_policy_deny_stops_before_tool_runtime() -> None:
    metadata = _tool(
        policy=ToolPolicyMetadata(
            side_effect=SideEffectMetadata(has_side_effect=False),
            permission=PermissionPolicy(required_scopes=("content:write",)),
        ),
    )

    async def reasoner(_observation, _state):
        return AgentAction(action=AgentActionType.TOOL_CALL, tool_name=metadata.name)

    result = await AgentLoop(reasoner=reasoner).run(
        Command(type=CommandType.QUERY, objective="read"),
        GoalTree(root=Goal(goal_id="g1", description="read")),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True},
    )
    assert result.success is False
    assert result.error_code == "TOOL_POLICY_DENIED"


@pytest.mark.asyncio
async def test_create_task_uses_submission_boundary_and_task_manager() -> None:
    submissions = RecordingExecutionSubmissionService()
    manager = TaskManager(InMemoryTaskRepository())
    action_count = 0

    async def reasoner(_observation, _state):
        nonlocal action_count
        action_count += 1
        return AgentAction(action=AgentActionType.CREATE_TASK)

    async def reflector(_observation, _action, _result, _state):
        return Reflection(finished=True, needs_next_step=False, reason="queued")

    result = await AgentLoop(
        reasoner=reasoner,
        reflector=reflector,
        task_manager=manager,
    ).run(
        Command(type=CommandType.CREATE, objective="create"),
        GoalTree(root=Goal(goal_id="g1", description="create")),
        execution_submission=submissions,
    )
    # Queue submission is an asynchronous hand-off.  AgentLoop reports the
    # accepted execution as RUNNING; the Worker owns eventual completion.
    assert result.success is False
    assert result.status.value == "RUNNING"
    assert len(submissions.submissions) == 1
    assert submissions.submissions[0]["plan"]
    assert action_count == 1


@pytest.mark.asyncio
async def test_multistep_read_observation_returns_to_agent_loop() -> None:
    metadata = ToolMetadata(
        name="community.search_public_posts",
        description="Search posts",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    actions = iter(
        [
            AgentAction(
                action=AgentActionType.TOOL_CALL,
                tool_name=metadata.name,
            ),
            AgentAction(action=AgentActionType.FINISH, reason="observed"),
        ]
    )

    async def reasoner(_observation, _state):
        return next(actions)

    result = await AgentLoop(reasoner=reasoner).run(
        Command(type=CommandType.QUERY, objective="research and summarize"),
        GoalTree(
            root=Goal(goal_id="g1", description="research and summarize"),
            task_nodes=[
                {"task_id": "t1", "goal_id": "g1", "capability": "SEARCH_COMMUNITY"},
                {"task_id": "t2", "goal_id": "g1", "capability": "ANALYZE_CONTENT_PATTERNS"},
            ],
        ),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True, "items": ["post"]},
    )

    assert result.status.value == "COMPLETED"
    assert result.success is True
    assert len(result.tool_results) == 1


@pytest.mark.asyncio
async def test_create_task_resolves_composite_capability_before_queue() -> None:
    metadata = [
        ToolMetadata(
            name="analytics.get_post_performance",
            description="Get performance for one post",
            capabilities=("ANALYZE_PERFORMANCE",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        ToolMetadata(
            name="analytics.get_account_summary",
            description="Get account performance summary",
            capabilities=("ANALYZE_PERFORMANCE",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
    ]

    class Selector:
        calls = 0

        async def select(self, _goal, _observation, _catalog, **_kwargs):
            self.calls += 1
            return SelectedTool(
                tool_name="analytics.get_account_summary",
                reason="selected from metadata",
            )

    selector = Selector()
    submissions = RecordingExecutionSubmissionService()

    async def reasoner(_observation, _state):
        return AgentAction(action=AgentActionType.CREATE_TASK)

    result = await AgentLoop(
        reasoner=reasoner,
        tool_selector=selector,
    ).run(
        Command(type=CommandType.CREATE, objective="analyze performance"),
        GoalTree(
            root=Goal(
                goal_id="g1",
                description="Analyze account performance",
                required_capabilities=["ANALYZE_PERFORMANCE"],
            ),
            task_nodes=[
                {
                    "task_id": "t1",
                    "goal_id": "g1",
                    "capability": "ANALYZE_PERFORMANCE",
                }
            ],
        ),
        available_tools=metadata,
        execution_submission=submissions,
    )

    assert result.status.value == "RUNNING"
    assert selector.calls == 1
    assert submissions.submissions[0]["plan"]["steps"][0]["tool_name"] == (
        "analytics.get_account_summary"
    )
