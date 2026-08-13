from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.argument_binder import ArgumentBinder
from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput
from greenbook_agent_core.planning.contracts import PlanStep


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
