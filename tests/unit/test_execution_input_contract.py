"""Phase 5.5 canonical Intelligence -> Execution contract tests."""

from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree


def test_execution_input_contains_resolved_step_contract_only() -> None:
    value = ExecutionInput(
        task_id="task-1",
        goal_id="goal-1",
        plan_id="plan-1",
        plan_version=2,
        steps=[
            ExecutionStepInput(
                step_id="publish",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={"run_at": "2026-08-12T10:00:00+08:00"},
                idempotency_key="task-1:plan-1:publish",
            )
        ],
    )

    payload = value.model_dump(mode="json")
    assert "task_intent" not in payload
    assert "intent_spec" not in payload
    assert payload["steps"][0]["tool_name"] == "publication.schedule"
    assert value.to_executable_plan().steps[0].tool_name == "publication.schedule"


def test_execution_input_rebuilds_multiple_steps_without_user_understanding() -> None:
    value = ExecutionInput(
        task_id="task-1",
        plan_id="plan-1",
        steps=[
            ExecutionStepInput(step_id="search", capability="SEARCH_COMMUNITY"),
            ExecutionStepInput(
                step_id="create",
                capability="GENERATE_CONTENT",
                tool_name="content.create_draft",
                dependency_refs=["search"],
            ),
        ],
    )
    plan = value.to_executable_plan()
    assert [step.step_id for step in plan.steps] == ["search", "create"]
    assert plan.steps[1].depends_on == ["search"]


def test_execution_input_round_trip_preserves_goal_identity() -> None:
    value = ExecutionInput(
        task_id="task-goal-owned",
        plan_id="plan-goal-owned",
        steps=[
            ExecutionStepInput(
                step_id="goal-17:1",
                goal_id="goal-17",
                capability="GENERATE_CONTENT",
            ),
            ExecutionStepInput(
                step_id="goal-04:1",
                goal_id="goal-04",
                capability="SEARCH_COMMUNITY",
            ),
        ],
    )

    rebuilt = value.to_executable_plan()
    assert [step.goal_id for step in rebuilt.steps] == ["goal-17", "goal-04"]


def test_dynamic_goal_cardinality_reaches_plan_and_execution_input() -> None:
    child_goals = [
        Goal(
            goal_id=f"goal-{index}",
            description=f"目标 {index}",
            required_capabilities=["GENERATE_CONTENT"],
        )
        for index in (17, 4, 92, 31, 58)
    ]
    tree = GoalTree(
        root=Goal(
            goal_id="request-root",
            description="用户请求",
            goal_type="COMPOSITE",
            children=child_goals,
        )
    )
    plan = GoalCompiler().compile_plan(tree, task_id="task-five-goals")
    assert len(plan.steps) == 5
    assert [step.goal_id for step in plan.steps] == [
        "goal-17", "goal-4", "goal-92", "goal-31", "goal-58"
    ]

    execution_input = ExecutionInput.from_executable_plan(
        task_id="task-five-goals",
        plan=plan,
        executable=plan,
    )
    assert len(execution_input.steps) == 5
    assert [step.goal_id for step in execution_input.steps] == [
        "goal-17", "goal-4", "goal-92", "goal-31", "goal-58"
    ]
