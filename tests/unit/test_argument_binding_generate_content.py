from __future__ import annotations

from greenbook_assistant_core.execution.argument_binder import ArgumentBinder
from greenbook_assistant_core.orchestration.context import PlanningContext
from greenbook_assistant_core.orchestration.models import PlanStep
from greenbook_assistant_core.task.models import TaskIntent


def test_generate_content_arguments_are_bound_from_schema_and_intent() -> None:
    intent = TaskIntent(
        goal="写一篇Java学习文章",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    context = PlanningContext(task_intent=intent)
    schemas = [
        {
            "name": "content.create_draft",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "references": {"type": "array"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "content"],
            },
        },
    ]
    step = PlanStep(capability="GENERATE_CONTENT", constraints={"topic": "Java"})

    arguments = ArgumentBinder(schemas).bind(step, context, user_message=intent.goal)

    assert arguments["title"] == "如何学好Java"
    assert "Java" in arguments["content"]
    assert "topic" not in arguments
    assert "references" not in arguments
    assert set(arguments) <= {"title", "content", "summary"}
