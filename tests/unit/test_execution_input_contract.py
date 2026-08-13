"""Phase 5.5 canonical Intelligence -> Execution contract tests."""

from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput


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
