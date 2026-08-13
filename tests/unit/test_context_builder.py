from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_agent_core.command.models import (
    Command,
    CommandTarget,
    CommandType,
    TargetKind,
    TargetReferenceType,
)
from greenbook_agent_core.command.target import TargetResolver
from greenbook_agent_core.context import ContextBudget, ContextBuilder, SessionContext


class _Tasks:
    def __init__(self, values):
        self.values = values

    async def list_tasks(self, _scope):
        return self.values


@pytest.mark.asyncio
async def test_context_builder_preserves_structured_state_and_budget() -> None:
    java = SimpleNamespace(
        task_id="task-java",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Create Java article",
        status="RUNNING",
        plan_version=2,
        goals=[],
        artifacts=[{
            "artifact_id": "draft-java",
            "task_id": "task-java",
            "artifact_type": "DRAFT",
            "summary": "Java tutorial draft",
        }],
        resource_index=[],
    )
    python = SimpleNamespace(
        task_id="task-python",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Create Python article",
        status="READY",
        plan_version=1,
        goals=[],
        artifacts=[],
        resource_index=[],
    )
    session = SessionContext(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        active_task_id="task-java",
    )
    builder = ContextBuilder(
        task_provider=_Tasks([java, python]),
        budget=ContextBudget(recent_message_limit=2, recent_message_chars=500),
    )

    snapshot = await builder.build(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        session=session,
        history=[
            {"role": "user", "content": "old message"},
            {"role": "assistant", "content": "middle"},
            {"role": "user", "content": "latest message"},
        ],
    )

    assert snapshot.active_task_id == "task-java"
    assert {item["task_id"] for item in snapshot.active_tasks} == {"task-java", "task-python"}
    assert snapshot.plan_version == 2
    assert len(snapshot.recent_messages) <= 2
    assert sum(len(item["content"]) for item in snapshot.recent_messages) <= 500
    assert any(item["id"] == "draft-java" for item in snapshot.target_candidates)


@pytest.mark.asyncio
async def test_context_projection_drops_unbounded_task_snapshots() -> None:
    task = SimpleNamespace(
        task_id="task-large",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Continue the article task",
        status="RUNNING",
        plan_version=7,
        goal_tree_version=7,
        goal_tree_snapshot={"goals": [{"body": "x" * 100_000}]},
        plan_history=[],
        revisions=[],
        action_history=["y" * 100_000 for _ in range(4)],
        artifacts=[],
        goals=[],
        execution_refs=[],
        resource_index=[],
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
    payload = snapshot.decision_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "goal_tree_snapshot" not in serialized
    assert len(serialized) < 20_000
    assert payload["active_tasks"][0]["task_id"] == "task-large"
    assert payload["active_tasks"][0]["plan_version"] == 7


def test_target_resolution_uses_candidates_and_reports_ambiguity() -> None:
    command = Command(
        type=CommandType.MODIFY,
        objective="modify Java draft",
        target=CommandTarget(
            kind=TargetKind.DRAFT,
            reference="Java tutorial draft",
            reference_type=TargetReferenceType.NONE,
        ),
    )
    resolved = TargetResolver().resolve(command, {
        "targets": [{
            "kind": "DRAFT",
            "id": "draft-java",
            "label": "Java tutorial draft",
        }]
    })
    assert resolved.is_resolved
    assert resolved.target is not None
    assert resolved.target.id == "draft-java"

    ambiguous = TargetResolver().resolve(command, {
        "targets": [
            {"kind": "DRAFT", "id": "draft-1", "label": "Java tutorial draft"},
            {"kind": "DRAFT", "id": "draft-2", "label": "Java tutorial draft"},
        ]
    })
    assert ambiguous.is_ambiguous
