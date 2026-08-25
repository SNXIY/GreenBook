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


def _independent_content_goals(count: int) -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="Create independent content targets",
            children=[
                Goal(
                    goal_id=f"goal-{index}",
                    description=f"Create content target {index}",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT"],
                    target={"topic": f"subject-{index}"},
                    publication_intent="DRAFT_ONLY",
                )
                for index in range(1, count + 1)
            ],
        )
    )


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
async def test_single_goal_side_effect_tool_call_keeps_direct_submission_path() -> None:
    metadata = _tool(capabilities=("GENERATE_CONTENT",))
    direct_submissions: list[str] = []
    executions = RecordingExecutionSubmissionService()

    async def reasoner(_observation, _state):
        return AgentAction(action=AgentActionType.TOOL_CALL, tool_name=metadata.name)

    async def submit_tool(*, tool_name, arguments, state):
        del arguments, state
        direct_submissions.append(tool_name)
        return {"queued": True, "status": "QUEUED"}

    result = await AgentLoop(reasoner=reasoner).run(
        Command(type=CommandType.CREATE, objective="Create one content target"),
        _independent_content_goals(1),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True},
        tool_submission=submit_tool,
        execution_submission=executions,
    )

    assert result.status.value == "RUNNING"
    assert direct_submissions == ["content.create_draft"]
    assert executions.submissions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_count", [2, 3])
async def test_multi_goal_side_effect_tool_call_submits_full_goal_tree_plan(
    goal_count: int,
) -> None:
    metadata = _tool(capabilities=("GENERATE_CONTENT",))
    executions = RecordingExecutionSubmissionService()
    direct_submissions: list[str] = []

    async def reasoner(_observation, _state):
        # This mirrors the live regression: the LLM chose a single
        # side-effecting TOOL_CALL instead of CREATE_TASK.
        return AgentAction(action=AgentActionType.TOOL_CALL, tool_name=metadata.name)

    async def submit_tool(*, tool_name, arguments, state):
        del arguments, state
        direct_submissions.append(tool_name)
        return {"queued": True, "status": "QUEUED"}

    result = await AgentLoop(reasoner=reasoner).run(
        Command(type=CommandType.CREATE, objective="Create independent content targets"),
        _independent_content_goals(goal_count),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True},
        tool_submission=submit_tool,
        execution_submission=executions,
    )

    assert result.status.value == "RUNNING"
    assert direct_submissions == []
    assert len(executions.submissions) == 1
    plan_steps = executions.submissions[0]["plan"]["steps"]
    assert {step["goal_id"] for step in plan_steps} == {
        f"goal-{index}" for index in range(1, goal_count + 1)
    }
    assert len(plan_steps) == goal_count
    assert result.execution_results[-1]["submitted_full_goal_tree_plan"] is True


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


@pytest.mark.asyncio
async def test_failed_queue_submission_is_not_reported_as_in_flight() -> None:
    """A rejected QUEUE submission must not be marked ``queued`` (design goal
    0813: only real business actions may be shown as in progress).  The loop
    re-observes the failure and re-routes instead of ending with a fake
    RUNNING/acceptance."""
    metadata = _tool(capabilities=("GENERATE_CONTENT",))
    submit_calls = 0

    async def reasoner(_observation, _state):
        nonlocal submit_calls
        if submit_calls == 0:
            submit_calls += 1
            return AgentAction(action=AgentActionType.TOOL_CALL, tool_name=metadata.name)
        # Second reason: the failed submission must be re-observed; the model
        # then decides to stop with an explicit failure instead of pretending
        # the work was accepted.
        return AgentAction(action=AgentActionType.FINISH, reason="submission failed")

    async def submit_tool(*, tool_name, arguments, state):
        del tool_name, arguments, state
        return {"ok": False, "code": "JAVA_BACKEND_UNAVAILABLE", "message": "down"}

    result = await AgentLoop(reasoner=reasoner, max_iterations=3).run(
        Command(type=CommandType.CREATE, objective="Create one content target"),
        _independent_content_goals(1),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True},
        tool_submission=submit_tool,
    )

    assert result.status.value == "FAILED"
    queued_results = [
        item for item in result.tool_results
        if item.get("queued") is True
    ]
    assert queued_results == [], "a rejected submission must never be queued=True"


@pytest.mark.asyncio
async def test_accepted_queue_submission_is_reported_as_in_flight() -> None:
    metadata = _tool(capabilities=("GENERATE_CONTENT",))

    async def reasoner(_observation, _state):
        return AgentAction(action=AgentActionType.TOOL_CALL, tool_name=metadata.name)

    async def submit_tool(*, tool_name, arguments, state):
        del tool_name, arguments, state
        return {"status": "QUEUED", "execution_id": "exec-1"}

    result = await AgentLoop(reasoner=reasoner, max_iterations=2).run(
        Command(type=CommandType.CREATE, objective="Create one content target"),
        _independent_content_goals(1),
        available_tools=[metadata],
        tool_runtime=lambda *_args, **_kwargs: {"ok": True},
        tool_submission=submit_tool,
    )

    assert result.status.value == "RUNNING"
    assert result.tool_results
    assert result.tool_results[-1].get("queued") is True


# ── in-loop tool argument schema validation (design goal 0813) ─────────────


def _validated_tool_metadata() -> ToolMetadata:
    return ToolMetadata(
        name="community.search_public_posts",
        description="Search public posts",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        policy=ToolPolicyMetadata(risk_level="READ"),
    )


@pytest.mark.asyncio
async def test_missing_required_tool_argument_is_controlled_failure() -> None:
    """A TOOL_CALL missing a required argument must not reach the runtime:
    the loop re-reasons with a controlled failure instead of sending
    malformed arguments downstream."""
    metadata = _validated_tool_metadata()
    tool_calls: list[str] = []

    async def reasoner(_observation, _state):
        return AgentAction(
            action=AgentActionType.TOOL_CALL,
            tool_name=metadata.name,
            tool_args={},  # missing required "query"
        )

    result = await AgentLoop(reasoner=reasoner, max_iterations=2).run(
        Command(type=CommandType.QUERY, objective="search"),
        GoalTree(root=Goal(goal_id="g1", description="search")),
        available_tools=[metadata],
        tool_runtime=lambda name, args: tool_calls.append(name) or {"ok": True},
    )

    assert result.success is False
    assert tool_calls == [], "the malformed call must never reach the tool runtime"
    assert result.error_code == "TOOL_ARGUMENT_MISSING"


@pytest.mark.asyncio
async def test_wrong_type_tool_argument_is_controlled_failure() -> None:
    metadata = _validated_tool_metadata()
    tool_calls: list[str] = []

    async def reasoner(_observation, _state):
        return AgentAction(
            action=AgentActionType.TOOL_CALL,
            tool_name=metadata.name,
            tool_args={"query": 42},  # numeric where string required
        )

    result = await AgentLoop(reasoner=reasoner, max_iterations=2).run(
        Command(type=CommandType.QUERY, objective="search"),
        GoalTree(root=Goal(goal_id="g1", description="search")),
        available_tools=[metadata],
        tool_runtime=lambda name, args: tool_calls.append(name) or {"ok": True},
    )

    assert result.success is False
    assert tool_calls == []
    assert result.error_code == "TOOL_ARGUMENT_TYPE_INVALID"


@pytest.mark.asyncio
async def test_valid_tool_arguments_pass_validation() -> None:
    metadata = _validated_tool_metadata()
    tool_calls: list[str] = []

    async def reasoner(_observation, _state):
        return AgentAction(
            action=AgentActionType.TOOL_CALL,
            tool_name=metadata.name,
            tool_args={"query": "Agent"},
        )

    result = await AgentLoop(reasoner=reasoner, max_iterations=3).run(
        Command(type=CommandType.QUERY, objective="search"),
        GoalTree(root=Goal(goal_id="g1", description="search")),
        available_tools=[metadata],
        tool_runtime=lambda name, args: tool_calls.append(name) or {
            "ok": True, "data": {"items": []},
        },
    )

    assert tool_calls, "the valid call must reach the tool runtime"
    assert all(name == "community.search_public_posts" for name in tool_calls)
    assert result.error_code not in ("TOOL_ARGUMENT_MISSING", "TOOL_ARGUMENT_TYPE_INVALID")
