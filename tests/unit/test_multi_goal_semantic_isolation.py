from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.execution.argument_binder import ArgumentBinder
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.execution.runtime_context import RuntimeContext, TaskContext
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.goal.compiler import GoalCompilationError, GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_agent_core.planning.validation import PlanValidator


def _three_goal_tree(order: tuple[str, ...] = ("goal-a", "goal-b", "goal-c")) -> GoalTree:
    goals = {
        "goal-a": Goal(
            goal_id="goal-a",
            description="Create content A",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            target={"topic": "subject-a"},
            temporal_constraint={"run_at": "2026-08-13T13:20:00+08:00"},
            publication_intent="SCHEDULED_PUBLISH",
        ),
        "goal-b": Goal(
            goal_id="goal-b",
            description="Create content B",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            target={"topic": "subject-b"},
            temporal_constraint={"run_at": "2026-08-14T15:00:00+08:00"},
            publication_intent="SCHEDULED_PUBLISH",
        ),
        "goal-c": Goal(
            goal_id="goal-c",
            description="Create content C and keep it as a draft",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT"],
            target={"topic": "subject-c"},
            publication_intent="DRAFT_ONLY",
        ),
    }
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="Three independent content goals",
            goal_type="TASK",
            children=[goals[item] for item in order],
        )
    )


def _aggregate_command() -> Command:
    # This deliberately contains a request-wide scalar.  A multi-goal
    # compiler must not copy it into every Goal.
    return Command(
        type=CommandType.CREATE,
        goal="Create three independent pieces of content",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        parameters={
            "run_at": "2026-08-13T13:20:00+08:00",
            "publication_intent": "SCHEDULED_PUBLISH",
        },
    )


def test_three_goals_keep_independent_publication_semantics() -> None:
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        _three_goal_tree(),
        task_id="semantic-isolation",
        command=_aggregate_command(),
    )

    by_goal = {
        goal_id: [step for step in plan.steps if step.goal_id == goal_id]
        for goal_id in ("goal-a", "goal-b", "goal-c")
    }
    assert len(plan.steps) == 5
    assert [step.capability for step in by_goal["goal-a"]].count("GENERATE_CONTENT") == 1
    assert [step.capability for step in by_goal["goal-a"]].count("SCHEDULE_PUBLISH") == 1
    assert [step.capability for step in by_goal["goal-b"]].count("GENERATE_CONTENT") == 1
    assert [step.capability for step in by_goal["goal-b"]].count("SCHEDULE_PUBLISH") == 1
    assert [step.capability for step in by_goal["goal-c"]] == ["GENERATE_CONTENT"]
    assert by_goal["goal-a"][1].constraints["run_at"] == "2026-08-13T13:20:00+08:00"
    assert by_goal["goal-b"][1].constraints["run_at"] == "2026-08-14T15:00:00+08:00"
    assert by_goal["goal-a"][1].depends_on == [by_goal["goal-a"][0].step_id]
    assert by_goal["goal-b"][1].depends_on == [by_goal["goal-b"][0].step_id]
    assert "run_at" not in by_goal["goal-c"][0].constraints
    assert all(step.capability != "PUBLISH_NOW" for step in plan.steps)
    assert [step.tool_name for step in plan.steps].count("content.create_draft") == 3
    assert [step.tool_name for step in plan.steps].count("publication.schedule") == 2
    assert "publication.publish_now" not in [step.tool_name for step in plan.steps]


def test_goal_order_does_not_change_semantic_ownership() -> None:
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        _three_goal_tree(("goal-c", "goal-a", "goal-b")),
        task_id="reordered-semantic-isolation",
        command=_aggregate_command(),
    )
    schedule_times = {
        step.goal_id: step.constraints.get("run_at")
        for step in plan.steps
        if step.capability == "SCHEDULE_PUBLISH"
    }
    assert schedule_times == {
        "goal-a": "2026-08-13T13:20:00+08:00",
        "goal-b": "2026-08-14T15:00:00+08:00",
    }
    assert {step.goal_id for step in plan.steps} == {"goal-a", "goal-b", "goal-c"}


def test_partial_task_hint_keeps_schedule_bound_to_its_goal_draft() -> None:
    tree = _three_goal_tree()
    tree.task_nodes = [
        TaskNode(
            task_id="goal-a:2",
            goal_id="goal-a",
            capability="SCHEDULE_PUBLISH",
            inputs={"run_at": "2026-08-13T13:20:00+08:00"},
        ),
    ]
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        tree,
        task_id="task-input-isolation",
        command=_aggregate_command(),
    )
    schedule_steps = {
        step.goal_id: step
        for step in plan.steps
        if step.capability == "SCHEDULE_PUBLISH"
    }
    assert schedule_steps["goal-a"].constraints["run_at"] == (
        "2026-08-13T13:20:00+08:00"
    )
    assert schedule_steps["goal-b"].constraints["run_at"] == (
        "2026-08-14T15:00:00+08:00"
    )
    generated_step = next(
        step
        for step in plan.steps
        if step.goal_id == "goal-a" and step.capability == "GENERATE_CONTENT"
    )
    assert schedule_steps["goal-a"].depends_on == [generated_step.step_id]
    assert plan.steps.index(generated_step) < plan.steps.index(schedule_steps["goal-a"])
    # The optional planner hint names only Goal A. GoalTree remains the
    # cardinality source, so deterministic completion must still cover all
    # three executable leaves before a durable plan can be submitted.
    assert {step.goal_id for step in plan.steps} == {"goal-a", "goal-b", "goal-c"}
    assert len(plan.steps) == 5
    assert PlanValidator(CapabilityRegistry()).validate(plan).is_valid


def test_structured_temporal_expression_is_normalized_for_its_goal() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="expression-time",
            description="Create and schedule content",
            goal_type="PUBLISH",
            required_capabilities=["SCHEDULE_PUBLISH"],
            temporal_constraint={
                "expression": "2026-08-14T15:00:00+08:00",
            },
            publication_intent="SCHEDULED_PUBLISH",
        )
    )

    plan = GoalCompiler(CapabilityRegistry()).to_task_plan(tree)

    assert plan.steps[0].constraints["run_at"] == (
        "2026-08-14T15:00:00+08:00"
    )


def test_multi_goal_immediate_publish_requires_its_own_semantic_intent() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Mixed publication modes",
            children=[
                Goal(
                    goal_id="scheduled",
                    required_capabilities=["SCHEDULE_PUBLISH"],
                    temporal_constraint={"run_at": "2026-08-14T15:00:00+08:00"},
                    publication_intent="SCHEDULED_PUBLISH",
                ),
                Goal(
                    goal_id="ambiguous-immediate",
                    required_capabilities=["PUBLISH_NOW"],
                ),
            ],
        )
    )
    command = Command(
        type=CommandType.CREATE,
        required_capabilities=["SCHEDULE_PUBLISH", "PUBLISH_NOW"],
    )

    with pytest.raises(GoalCompilationError, match="explicitly declare IMMEDIATE_PUBLISH"):
        GoalCompiler(CapabilityRegistry()).compile_plan(tree, command=command)


def test_mixed_publication_modes_remain_independent() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Four independent content goals",
            children=[
                Goal(
                    goal_id="relative-schedule",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    temporal_constraint={"run_at": "2026-08-13T13:20:00+08:00"},
                    publication_intent="SCHEDULED_PUBLISH",
                ),
                Goal(
                    goal_id="absolute-schedule",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    temporal_constraint={"run_at": "2026-08-14T15:00:00+08:00"},
                    publication_intent="SCHEDULED_PUBLISH",
                ),
                Goal(
                    goal_id="draft-only",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                ),
                Goal(
                    goal_id="immediate",
                    required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
                    publication_intent="IMMEDIATE_PUBLISH",
                ),
            ],
        )
    )
    command = Command(
        type=CommandType.CREATE,
        required_capabilities=[
            "GENERATE_CONTENT",
            "SCHEDULE_PUBLISH",
            "PUBLISH_NOW",
        ],
    )

    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree, command=command)
    by_goal = {
        goal_id: [step.capability for step in plan.steps if step.goal_id == goal_id]
        for goal_id in (
            "relative-schedule",
            "absolute-schedule",
            "draft-only",
            "immediate",
        )
    }

    assert by_goal["relative-schedule"] == ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    assert by_goal["absolute-schedule"] == ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    assert by_goal["draft-only"] == ["GENERATE_CONTENT"]
    assert by_goal["immediate"] == ["GENERATE_CONTENT", "PUBLISH_NOW"]


def test_execution_input_and_durable_step_preserve_goal_identity() -> None:
    compiler = GoalCompiler(CapabilityRegistry())
    plan = compiler.compile_plan(_three_goal_tree(), task_id="durable-goals")
    executable = PlanValidator(CapabilityRegistry()).validate(plan)
    assert executable.is_valid
    execution_input = ExecutionInput.from_executable_plan(
        task_id="durable-goals",
        plan=plan,
        executable=executable,
    )
    assert [step.goal_id for step in execution_input.steps] == [
        step.goal_id for step in plan.steps
    ]

    execution = ExecutionStateManager(repository=ExecutionRepository()).init_execution(
        plan,
        executable,
    )
    assert [step.goal_id for step in execution.steps] == [
        step.goal_id for step in plan.steps
    ]


def test_draft_only_cannot_become_immediate_publish() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="draft-only",
            description="Create and keep a draft",
            required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
            publication_intent="DRAFT_ONLY",
        )
    )
    with pytest.raises(GoalCompilationError, match="DRAFT_ONLY"):
        GoalCompiler(CapabilityRegistry()).compile_plan(tree)


def test_unrequested_immediate_publish_is_rejected() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="unexpected-publish",
            description="Create content",
            required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
        )
    )
    with pytest.raises(GoalCompilationError, match="unrequested PUBLISH_NOW"):
        GoalCompiler(CapabilityRegistry()).compile_plan(
            tree,
            command=Command(
                type=CommandType.CREATE,
                required_capabilities=["GENERATE_CONTENT"],
            ),
        )


def test_schedule_without_time_fails_closed_at_plan_boundary() -> None:
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        GoalTree(
            root=Goal(
                goal_id="missing-time",
                goal_type="PUBLISH",
                required_capabilities=["SCHEDULE_PUBLISH"],
            )
        )
    )
    result = PlanValidator(CapabilityRegistry()).validate(plan)
    assert result.is_valid is False
    assert any(error.error_code == "SCHEDULE_TIME_REQUIRED" for error in result.errors)


def test_multi_goal_schedule_without_owned_draft_fails_closed() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Two independent goals",
            children=[
                Goal(
                    goal_id="schedule-existing",
                    goal_type="PUBLISH",
                    required_capabilities=["SCHEDULE_PUBLISH"],
                    temporal_constraint={"run_at": "2026-08-14T15:00:00+08:00"},
                    publication_intent="SCHEDULED_PUBLISH",
                ),
                Goal(
                    goal_id="independent-draft",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                ),
            ],
        )
    )
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(tree)

    result = PlanValidator(CapabilityRegistry()).validate(plan)

    assert result.is_valid is False
    assert any(
        error.error_code == "MULTI_GOAL_PUBLICATION_OWNERSHIP_REQUIRED"
        for error in result.errors
    )


def test_multi_goal_immediate_publish_without_owned_draft_fails_closed() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Two independent goals",
            children=[
                Goal(
                    goal_id="publish-existing",
                    goal_type="PUBLISH",
                    required_capabilities=["PUBLISH_NOW"],
                    publication_intent="IMMEDIATE_PUBLISH",
                ),
                Goal(
                    goal_id="independent-draft",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                ),
            ],
        )
    )
    plan = GoalCompiler(CapabilityRegistry()).compile_plan(
        tree,
        command=Command(
            type=CommandType.CREATE,
            required_capabilities=["PUBLISH_NOW", "GENERATE_CONTENT"],
        ),
    )

    result = PlanValidator(CapabilityRegistry()).validate(plan)

    assert result.is_valid is False
    assert any(
        error.error_code == "MULTI_GOAL_PUBLICATION_OWNERSHIP_REQUIRED"
        for error in result.errors
    )


@pytest.mark.asyncio
async def test_queue_execution_revalidates_missing_schedule_time() -> None:
    execution_input = ExecutionInput(
        task_id="queue-missing-schedule-time",
        goal="Schedule a draft",
        goal_category="COMPOSITE",
        steps=[
            ExecutionStepInput(
                step_id="scheduled:1",
                goal_id="scheduled",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={},
            )
        ],
    )
    message = ExecutionQueueMessage(
        execution_id="execution-missing-schedule-time",
        trace_id="trace-missing-schedule-time",
        payload={
            "execution_input": execution_input.model_dump(mode="json"),
            "conversation_id": "conversation-missing-schedule-time",
            "user_id": "user-missing-schedule-time",
            "tenant_id": "tenant-missing-schedule-time",
            "session": {
                "conversation_id": "conversation-missing-schedule-time",
                "user_id": "user-missing-schedule-time",
                "tenant_id": "tenant-missing-schedule-time",
            },
        },
    )

    result = await RuntimeAgentService(dispatch_mode="direct").execute_queued(
        message,
        mcp=None,
    )

    assert result.success is False
    assert result.error_code == "PLAN_INVALID"
    assert "Scheduled publication requires an explicit run_at" in result.error_message


def test_schedule_time_binding_uses_each_step_not_request_text() -> None:
    execution_input = ExecutionInput(
        task_id="temporal-isolation",
        goal="two independently scheduled goals",
        created_at="2026-08-13T05:00:00+00:00",
        steps=[
            ExecutionStepInput(
                step_id="goal-a:2",
                goal_id="goal-a",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={"run_at": "\u4e8c\u5341\u5206\u949f\u540e"},
            ),
            ExecutionStepInput(
                step_id="goal-b:2",
                goal_id="goal-b",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={"run_at": "\u660e\u5929\u4e0b\u53483\u70b9"},
            ),
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_at": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                }
            }
        },
        registry=CapabilityRegistry(),
        timezone="Asia/Shanghai",
    )

    bound = [
        binder.bind(
            PlanStep(
                step_id=step.step_id,
                goal_id=step.goal_id,
                capability=step.capability,
                tool_name=step.tool_name,
                constraints=step.arguments,
            ),
            execution_input=execution_input,
        )
        for step in execution_input.steps
    ]
    assert bound[0]["run_at"] == "2026-08-13T05:20:00Z"
    assert bound[1]["run_at"] == "2026-08-14T07:00:00Z"
    assert bound[0]["run_at"] != bound[1]["run_at"]


def test_multi_step_binding_does_not_use_a_global_schedule_argument() -> None:
    execution_input = ExecutionInput(
        task_id="global-argument-isolation",
        goal="two independently scheduled goals",
        arguments={"run_at": "2099-01-01T00:00:00Z"},
        steps=[
            ExecutionStepInput(
                step_id="goal-a:2",
                goal_id="goal-a",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={"run_at": "2026-08-13T13:20:00+08:00"},
            ),
            ExecutionStepInput(
                step_id="goal-b:2",
                goal_id="goal-b",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={},
            ),
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_at": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                }
            }
        },
        registry=CapabilityRegistry(),
        timezone="Asia/Shanghai",
    )

    bound_first = binder.bind(
        PlanStep(
            step_id="goal-a:2",
            goal_id="goal-a",
            capability="SCHEDULE_PUBLISH",
            tool_name="publication.schedule",
            constraints={"run_at": "2026-08-13T13:20:00+08:00"},
        ),
        execution_input=execution_input,
    )
    bound_second = binder.bind(
        PlanStep(
            step_id="goal-b:2",
            goal_id="goal-b",
            capability="SCHEDULE_PUBLISH",
            tool_name="publication.schedule",
            constraints={},
        ),
        execution_input=execution_input,
    )

    assert bound_first["run_at"] == "2026-08-13T05:20:00Z"
    assert "run_at" not in bound_second


class _ExecutionRepo:
    def __init__(self, execution: PlanExecution) -> None:
        self.execution = execution

    def find_by_id(self, _execution_id: str) -> PlanExecution:
        return self.execution


def test_approval_projection_keeps_goal_step_and_target() -> None:
    execution = PlanExecution(
        execution_id="execution-approval",
        task_id="task-approval",
        status=ExecutionStatus.WAITING_HUMAN,
        steps=[
            StepExecution(
                execution_id="execution-approval",
                step_id="goal-b:2",
                goal_id="goal-b",
                capability="PUBLISH_NOW",
                tool_name="publication.publish_now",
                status=StepStatus.WAITING_APPROVAL,
                depends_on=["goal-b:1"],
                arguments={
                    "draft_id": "draft-b",
                    "run_at": "2026-08-14T15:00:00+08:00",
                },
                checkpoint_data={"constraints": {"draft_id": "draft-b"}},
            )
        ],
    )
    worker = SimpleNamespace(_repo=_ExecutionRepo(execution))
    execution_input = ExecutionInput(
        task_id="task-approval",
        steps=[
            ExecutionStepInput(
                step_id="goal-b:2",
                goal_id="goal-b",
                capability="PUBLISH_NOW",
                tool_name="publication.publish_now",
                arguments={"draft_id": "draft-b"},
            )
        ],
    )
    ctx = RuntimeContext(
        task_id="task-approval",
        execution_id="execution-approval",
        task_context=TaskContext(
            task_id="task-approval",
            goal="approval target",
            execution_input=execution_input,
        ),
        execution_input=execution_input,
    )
    result = RuntimeAgentService(dispatch_mode="direct")._pause_for_approval(
        ctx,
        worker,
        "execution-approval",
    )
    assert result.approval_data is not None
    assert result.approval_data["goal_id"] == "goal-b"
    assert result.approval_data["step_id"] == "goal-b:2"
    assert result.approval_data["operation"] == "publication.publish_now"
    assert result.approval_data["resource_id"] == "draft-b"
    assert result.approval_data["payload"]["run_at"] == (
        "2026-08-14T15:00:00+08:00"
    )
