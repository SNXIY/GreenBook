from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from greenbook_agent_core.command.models import (
    Command,
    CommandContext,
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
async def test_resource_index_candidates_keep_their_business_kind() -> None:
    task = SimpleNamespace(
        task_id="task-java",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Java 学习",
        status="COMPLETED",
        goals=[],
        objectives=[],
        artifacts=[],
        resource_index=[
            {
                "resource_id": "draft-java",
                "resource_kind": "DRAFT",
                "title": "Java 学习路线",
                "status": "DRAFT",
            },
            {
                "resource_id": "schedule-java",
                "resource_kind": "SCHEDULE",
                "title": "Java 学习路线",
                "status": "SCHEDULED",
            },
        ],
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

    by_id = {item["id"]: item for item in snapshot.target_candidates}
    assert by_id["draft-java"]["kind"] == "DRAFT"
    assert by_id["schedule-java"]["kind"] == "SCHEDULE"


@pytest.mark.asyncio
async def test_target_candidates_dedupe_artifact_and_business_resource_views() -> None:
    task = SimpleNamespace(
        task_id="task-java",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="Java 学习",
        status="COMPLETED",
        goals=[],
        objectives=[],
        artifacts=[{
            "artifact_id": "artifact-draft-java",
            "artifact_type": "DRAFT",
            "resource_id": "draft-java",
            "resource_kind": "DRAFT",
            "title": "Java 学习路线",
        }],
        resource_index=[{
            "resource_id": "draft-java",
            "resource_kind": "DRAFT",
            "title": "Java 学习路线",
            "status": "DRAFT",
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

    drafts = [
        item for item in snapshot.target_candidates
        if item.get("kind") == "DRAFT" and item.get("id") == "draft-java"
    ]
    assert len(drafts) == 1


def test_schedule_publish_scope_resolves_the_draft_not_its_task_owner() -> None:
    context = CommandContext(targets=[
        {
            "kind": "TASK",
            "id": "task-java",
            "label": "Java task",
            "resource_index": [{
                "resource_id": "draft-java",
                "resource_kind": "DRAFT",
                "title": "Java draft",
            }],
        },
        {
            "kind": "DRAFT",
            "id": "draft-java",
            "resource_kind": "DRAFT",
            "label": "Java draft",
        },
    ])
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="SCHEDULE_PUBLISH",
        target=CommandTarget(
            kind=TargetKind.TASK,
            reference_type=TargetReferenceType.PROPERTY,
            property="label",
            value="Java",
        ),
    )

    resolution = TargetResolver().resolve(command, context)

    assert resolution.status.value == "RESOLVED"
    assert resolution.target is not None
    assert resolution.target.kind == TargetKind.DRAFT
    assert resolution.target.resource_id == "draft-java"


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


def test_target_candidate_contract_dedupes_physical_resource_but_not_peers() -> None:
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="UPDATE_DRAFT",
        objective="modify the Java draft",
        target=CommandTarget(
            kind=TargetKind.DRAFT,
            reference_type=TargetReferenceType.PROPERTY,
            property="label",
            value="Java",
        ),
    )
    result = TargetResolver().resolve(command, {
        "targets": [
            {"kind": "DRAFT", "id": "draft-java", "label": "Java 学习路线"},
            # Same physical resource, repeated by two durable projections.
            {"kind": "DRAFT", "resource_id": "draft-java", "label": "Java 学习路线"},
            {"kind": "DRAFT", "id": "draft-java-2", "label": "Java 学习路线"},
        ]
    })

    assert result.is_ambiguous
    assert {candidate.identity for candidate in result.candidates} == {
        "draft-java",
        "draft-java-2",
    }
