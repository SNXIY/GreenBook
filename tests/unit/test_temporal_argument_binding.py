from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenbook_assistant_core.execution.argument_binder import ArgumentBinder
from greenbook_assistant_core.orchestration.context import PlanningContext
from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
from greenbook_assistant_core.task.models import TaskIntent


def test_schedule_run_at_is_bound_into_task_plan() -> None:
    intent = TaskIntent(
        goal="明天8点发布",
        goal_category="PUBLISH_CONTENT",
        requirements=[{"type": "PUBLISH"}],
    )
    context = PlanningContext(task_intent=intent)
    step = PlanStep(
        capability="SCHEDULE_PUBLISH",
        constraints={"time": "明天8点"},
    )
    plan = TaskPlan(steps=[step])
    binder = ArgumentBinder(
        [
            {
                "name": "publication.schedule",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_at": {"type": "string"},
                        "draft_id": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["run_at"],
                },
            },
        ],
        timezone="Asia/Shanghai",
        now=datetime(2026, 8, 9, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    binder.bind_plan(plan, context, user_message=intent.goal)

    assert step.constraints["run_at"] == "2026-08-10T00:00:00Z"
    assert step.constraints["timezone"] == "Asia/Shanghai"
    assert "time" not in step.constraints

