from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_agent_core.command import CommandContext, CommandInterpreter
from greenbook_agent_core.context import ContextBuilder, SessionContext
from greenbook_agent_core.context.projection import project_interpreter_context


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "command": "QUERY",
                "goal": "查看状态",
            }, ensure_ascii=False)))],
        )


class _LLM:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions())


def _context() -> CommandContext:
    return CommandContext(
        active_tasks=[
            {
                "task_id": "task-java",
                "goal": "Java 学习",
                "status": "PENDING",
                "objectives": [{
                    "objective_id": "objective-java",
                    "description": "Java 学习",
                    "status": "PENDING",
                }],
                "resource_index": [{
                    "resource_id": "schedule-java",
                    "resource_kind": "SCHEDULE",
                    "label": "Java 学习",
                    "run_at": "2026-08-23T15:00:00+08:00",
                }],
            },
            {
                "task_id": "task-failed",
                "goal": "Agent Memory",
                "status": "FAILED",
                "objectives": [{
                    "objective_id": "objective-failed",
                    "description": "Agent Memory",
                    "status": "FAILED",
                    "desired_outcome": "PUBLISH_NOW",
                    "constraints": {"publication_intent": "IMMEDIATE_PUBLISH"},
                }],
                "resource_index": [{
                    "resource_id": "draft-failed",
                    "resource_kind": "DRAFT",
                    "label": "Agent Memory",
                }],
            },
        ],
        targets=[{
            "kind": "SCHEDULE",
            "resource_id": "schedule-java",
            "task_id": "task-java",
            "label": "Java 学习",
            "status": "SCHEDULED",
        }],
        metadata={"objective_id": "objective-java", "resource_id": "schedule-java"},
    )


def test_interpreter_context_hides_canonical_ids_and_labels_history() -> None:
    view = project_interpreter_context(_context())
    serialized = json.dumps(view, ensure_ascii=False)

    assert "schedule-java" not in serialized
    assert "objective-java" not in serialized
    assert "task-java" not in serialized
    assert "draft-failed" not in serialized
    assert "Java 学习" in serialized
    assert "historical_outcome" in serialized
    assert "historical_constraints" in serialized
    assert "current_outcome" not in serialized


@pytest.mark.asyncio
async def test_provider_view_keeps_explicit_user_id_in_user_input_only() -> None:
    llm = _LLM()
    await CommandInterpreter(llm=llm, model="test").interpret(
        "删除 draft 123",
        _context(),
    )

    request = json.loads(llm.chat.completions.calls[0]["messages"][1]["content"])
    context_text = json.dumps(request["context"], ensure_ascii=False)
    assert "schedule-java" not in context_text
    assert "objective-java" not in context_text
    assert request["user_input"] == "删除 draft 123"


@pytest.mark.asyncio
async def test_snapshot_derived_context_preserves_failed_business_evidence() -> None:
    objective = SimpleNamespace(
        objective_id="objective-java",
        task_id="task-java",
        description="Java 学习路线",
        intent="CREATE_AND_SCHEDULE",
        status="FAILED",
        expected_resource_kind="SCHEDULE",
        constraints={
            "run_at": "2026-08-24T04:00:00Z",
            "publication_intent": "SCHEDULED_PUBLISH",
            "resource_id": "schedule-java",
        },
        related_resource_ids=["schedule-java"],
    )
    task = SimpleNamespace(
        task_id="task-java",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Java 学习路线",
        status="COMPLETED",
        objectives=[objective],
        goals=[],
        artifacts=[],
        resource_index=[{
            "resource_id": "schedule-java",
            "resource_kind": "SCHEDULE",
            "title": "Java 学习路线",
            "status": "SCHEDULED",
            "scheduled_at": "2026-08-24T04:00:00Z",
        }],
    )

    class Tasks:
        async def list_tasks(self, _scope):
            return [task]

    snapshot = await ContextBuilder(task_provider=Tasks()).build(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        session=SessionContext(conversation_id="c1", user_id="u1", tenant_id="t1"),
    )
    view = project_interpreter_context(CommandContext.from_any(snapshot))
    serialized = json.dumps(view, ensure_ascii=False)

    failed = view["unfinished_goals"][0]
    assert failed["semantic_label"] == "Java 学习路线"
    assert failed["operation"] == "CREATE_AND_SCHEDULE"
    assert failed["resource_kind"] == "SCHEDULE"
    assert failed["relation"] == "failed_previous_turn"
    assert failed["historical"] is True
    assert failed["historical_constraints"]["run_at"] == "2026-08-24T04:00:00Z"
    assert "schedule-java" not in serialized

    schedule = next(item for item in view["targets"] if item["resource_kind"] == "SCHEDULE")
    assert schedule["semantic_label"] == "Java 学习路线"
    assert schedule["current_state"] == "SCHEDULED"
    assert schedule["current"] is True


def test_provider_view_projects_verified_facts_without_resource_identity() -> None:
    view = project_interpreter_context(CommandContext(targets=[{
        "kind": "POST",
        "resource_id": "post-java",
        "label": "Java 学习路线",
        "status": "COMPLETED",
        "verified_facts": {
            "state": "PUBLISHED",
            "post_id": "post-java",
            "source": "java-business",
        },
    }]))

    post = view["targets"][0]
    assert post["semantic_label"] == "Java 学习路线"
    assert post["resource_kind"] == "POST"
    assert post["historical"] is True
    assert post["verified_outcome"] == {
        "state": "PUBLISHED",
        "source": "java-business",
    }
    assert "post-java" not in json.dumps(view, ensure_ascii=False)


def test_provider_view_strips_nested_history_runtime_identity() -> None:
    view = project_interpreter_context(CommandContext(
        history=[{
            "role": "assistant",
            "parts": [{
                "artifacts": [{
                    "draftId": "draft-java",
                    "resourceId": "draft-java",
                    "taskId": "task-java",
                    "resourceType": "DRAFT",
                    "title": "Java 学习路线",
                }],
                "execution": {"executionId": "execution-java"},
            }],
        }],
    ))
    serialized = json.dumps(view, ensure_ascii=False)

    assert "draft-java" not in serialized
    assert "task-java" not in serialized
    assert "execution-java" not in serialized
    assert "Java 学习路线" in serialized
