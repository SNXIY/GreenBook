from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.argument_binder import ArgumentBinder
from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput
from greenbook_agent_core.planning.contracts import PlanStep
import pytest


def test_schedule_arguments_normalize_relative_goal_at_queue_boundary() -> None:
    execution_input = ExecutionInput(
        task_id="task-1",
        goal="下周一晚上8点发布第一篇文章",
        created_at="2026-08-12T04:00:00+00:00",
        steps=[
            ExecutionStepInput(
                step_id="schedule",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={"run_at": "下周一晚上8点"},
            )
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

    bound = binder.bind(
        PlanStep(
            step_id="schedule",
            capability="SCHEDULE_PUBLISH",
            tool_name="publication.schedule",
            constraints={"run_at": "下周一晚上8点"},
        ),
        execution_input=execution_input,
    )

    assert bound["run_at"] == "2026-08-17T12:00:00Z"
    assert bound["timezone"] == "Asia/Shanghai"


def test_schedule_update_relative_to_existing_schedule_uses_existing_run_at() -> None:
    execution_input = ExecutionInput(
        task_id="task-1",
        goal="move the schedule ten minutes later",
        created_at="2026-08-15T00:00:00Z",
        steps=[
            ExecutionStepInput(
                step_id="update",
                capability="MANAGE_SCHEDULE",
                tool_name="publication.update_schedule",
                arguments={},
            )
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.update_schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {"type": "string"},
                        "run_at": {"type": "string"},
                    },
                }
            }
        },
        registry=CapabilityRegistry(),
        timezone="Asia/Shanghai",
    )

    bound = binder.bind(
        PlanStep(
            step_id="update",
            capability="MANAGE_SCHEDULE",
            tool_name="publication.update_schedule",
            constraints={
                "schedule_id": "schedule-1",
                "run_at": "10 minutes later",
                "temporal_base": "EXISTING_SCHEDULE_TIME",
                "existing_schedule_run_at": "2026-08-15T08:00:00Z",
            },
        ),
        execution_input=execution_input,
    )

    assert bound == {
        "schedule_id": "schedule-1",
        "run_at": "2026-08-15T08:10:00Z",
    }


def test_schedule_update_relative_to_existing_schedule_requires_authoritative_base() -> None:
    execution_input = ExecutionInput(
        task_id="task-1",
        goal="move later",
        steps=[
            ExecutionStepInput(
                step_id="update",
                capability="MANAGE_SCHEDULE",
                tool_name="publication.update_schedule",
                arguments={},
            )
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.update_schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {"schedule_id": {}, "run_at": {}},
                }
            }
        },
        registry=CapabilityRegistry(),
    )

    with pytest.raises(ValueError, match="EXISTING_SCHEDULE_TIME"):
        binder.bind(
            PlanStep(
                step_id="update",
                capability="MANAGE_SCHEDULE",
                tool_name="publication.update_schedule",
                constraints={
                    "schedule_id": "schedule-1",
                    "run_at": "10 minutes later",
                    "temporal_base": "EXISTING_SCHEDULE_TIME",
                },
            ),
            execution_input=execution_input,
        )


def test_update_schedule_defers_existing_base_to_authoritative_tool_read() -> None:
    execution_input = ExecutionInput(
        task_id="task-1",
        goal="move later than original plan",
        steps=[
            ExecutionStepInput(
                step_id="update",
                capability="MANAGE_SCHEDULE",
                tool_name="publication.update_schedule",
                arguments={},
            )
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.update_schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {},
                        "run_at": {},
                        "temporal_base": {},
                        "timezone": {},
                    },
                }
            }
        },
        registry=CapabilityRegistry(),
    )

    bound = binder.bind(
        PlanStep(
            step_id="update",
            capability="MANAGE_SCHEDULE",
            tool_name="publication.update_schedule",
            constraints={
                "schedule_id": "schedule-1",
                "run_at": "10 minutes later",
                "temporal_base": "EXISTING_SCHEDULE_TIME",
            },
        ),
        execution_input=execution_input,
    )

    assert bound == {
        "schedule_id": "schedule-1",
        "run_at": "10 minutes later",
        "temporal_base": "EXISTING_SCHEDULE_TIME",
        "timezone": "Asia/Shanghai",
    }


def test_schedule_binder_fails_closed_for_unresolved_future_time() -> None:
    execution_input = ExecutionInput(
        task_id="task-1",
        goal="schedule it sometime later",
        steps=[
            ExecutionStepInput(
                step_id="schedule",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                arguments={},
            )
        ],
    )
    binder = ArgumentBinder(
        {
            "publication.schedule": {
                "parameters": {
                    "type": "object",
                    "properties": {"run_at": {}, "draft_id": {}},
                }
            }
        },
        registry=CapabilityRegistry(),
    )

    with pytest.raises(ValueError, match="Unresolved future temporal"):
        binder.bind(
            PlanStep(
                step_id="schedule",
                capability="SCHEDULE_PUBLISH",
                tool_name="publication.schedule",
                constraints={"run_at": "sometime later"},
            ),
            execution_input=execution_input,
        )
