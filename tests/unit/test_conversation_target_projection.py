"""Regression tests for Assistant conversation/task target recovery."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.api.routes import (
    _conversation_target_task,
    _sync_task_artifact_binding,
    _task_artifacts_from_result,
)
from greenbook_assistant_api.models.runtime_context import RuntimeContext, TaskContext
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.task.models import ArtifactRef, Task, TaskIntent


def _task() -> Task:
    return Task(
        task_id="task-agent",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="创建 AI Agent 学习路线帖子",
        goal_category="CREATE_CONTENT",
        artifacts=[
            ArtifactRef(
                artifact_id="artifact-draft",
                task_id="task-agent",
                artifact_type="DRAFT",
                resource_id="draft-agent",
            ),
            ArtifactRef(
                artifact_id="artifact-schedule",
                task_id="task-agent",
                artifact_type="SCHEDULE",
                resource_id="schedule-agent",
            ),
        ],
    )


def _session(**kwargs: str | None) -> SessionContext:
    return SessionContext(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        **kwargs,
    )


def test_follow_up_schedule_binds_existing_task_and_creates_schedule() -> None:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal_category="PUBLISH_CONTENT",
        requirements=[{"type": "PUBLISH"}],
    )

    resolved = _conversation_target_task(
        intent,
        session=_session(active_draft_id="draft-agent"),
        recent_tasks=[_task()],
        user_message="五分钟之后发布",
    )

    assert resolved is intent
    assert intent.target_task_id == "task-agent"
    assert intent.relation == "CONTINUE_TASK"
    assert intent.goal_category == "PUBLISH_CONTENT"
    assert intent.requirements == [{"type": "PUBLISH"}]
    assert intent.resource_requests[0]["operation"] == "CREATE"


def test_follow_up_schedule_updates_existing_schedule() -> None:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal_category="PUBLISH_CONTENT",
        requirements=[{"type": "PUBLISH"}],
    )

    _conversation_target_task(
        intent,
        session=_session(
            active_draft_id="draft-agent",
            active_schedule_id="schedule-agent",
        ),
        recent_tasks=[_task()],
        user_message="把发布时间改到晚上九点",
    )

    assert intent.target_task_id == "task-agent"
    assert intent.goal_category == "MANAGE_SCHEDULE"
    assert intent.requirements == [{"type": "UPDATE"}]
    assert intent.resource_requests[0]["operation"] == "UPDATE"


def test_follow_up_revision_selects_update_draft_without_research() -> None:
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        intent_spec={"stale": True},
    )

    _conversation_target_task(
        intent,
        session=_session(active_draft_id="draft-agent"),
        recent_tasks=[_task()],
        user_message="修改一下内容，增加代码逻辑",
    )

    assert intent.target_task_id == "task-agent"
    assert intent.requirements == [{"type": "IMPROVE"}]
    assert intent.resource_requests[0]["resource_type"] == "CONTENT_DRAFT"
    assert intent.intent_spec is None


def test_runtime_artifacts_become_durable_task_references() -> None:
    result = RuntimeResult(
        task_id="task-agent",
        artifacts=[
            {
                "artifact_id": "artifact-draft",
                "artifact_type": "DRAFT",
                "resource_id": "draft-agent",
                "summary": "AI Agent 学习路线",
                "step_id": "generate-content",
            },
            {
                "artifact_id": "artifact-schedule",
                "artifact_type": "SCHEDULE",
                "data": {"schedule_id": "schedule-agent"},
            },
        ],
    )

    refs = _task_artifacts_from_result(result)

    assert [ref["artifact_id"] for ref in refs] == [
        "artifact-draft", "artifact-schedule",
    ]
    assert refs[1]["resource_id"] == "schedule-agent"
    assert refs[1]["task_id"] == "task-agent"


def test_create_post_sets_active_task_and_artifact() -> None:
    session = _session()
    result = RuntimeResult(
        task_id="task-create",
        artifacts=[{
            "artifact_id": "artifact-create",
            "artifact_type": "DRAFT",
            "resource_id": "draft-create",
        }],
    )

    _sync_task_artifact_binding(
        session,
        task_id=result.task_id,
        artifact_refs=_task_artifacts_from_result(result),
    )

    assert session.active_task_id == "task-create"
    assert session.active_artifact_id == "artifact-create"


def test_modify_post_keeps_same_task_id() -> None:
    session = _session(
        active_task_id="task-create",
        active_artifact_id="artifact-create",
        active_draft_id="draft-create",
    )
    result = RuntimeResult(
        task_id="task-create",
        artifacts=[{
            "artifact_id": "artifact-revised",
            "artifact_type": "DRAFT",
            "resource_id": "draft-create",
        }],
    )

    _sync_task_artifact_binding(
        session,
        task_id=result.task_id,
        artifact_refs=_task_artifacts_from_result(result),
    )

    assert session.active_task_id == "task-create"
    assert session.active_artifact_id == "artifact-revised"
    assert session.active_draft_id == "draft-create"


def test_schedule_post_attaches_result_to_same_task() -> None:
    session = _session(
        active_task_id="task-create",
        active_artifact_id="artifact-create",
        active_draft_id="draft-create",
    )
    result = RuntimeResult(
        task_id="task-create",
        artifacts=[{
            "artifact_id": "artifact-schedule",
            "artifact_type": "SCHEDULE",
            "resource_id": "schedule-create",
            "data": {"draft_id": "draft-create"},
        }],
    )

    refs = _task_artifacts_from_result(result)
    _sync_task_artifact_binding(
        session,
        task_id=result.task_id,
        artifact_refs=refs,
    )

    assert session.active_task_id == "task-create"
    assert refs[0]["task_id"] == "task-create"
    assert refs[0]["resource_id"] == "schedule-create"
    assert session.active_draft_id == "draft-create"


@pytest.mark.asyncio
async def test_resolved_revision_uses_revision_tool_not_community_search() -> None:
    calls: list[str] = []
    mcp = AsyncMock()

    async def execute_tool(tool_name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(tool_name)
        return {
            "ok": True,
            "data": {"draft_id": "draft-agent", "title": "Updated"},
        }

    mcp.execute_tool = execute_tool
    service = RuntimeAgentService()
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        requirements=[{"type": "IMPROVE"}],
    )
    result = await service.execute(RuntimeContext(
        run_id="run-revise",
        trace_id="trace-revise",
        task_id="task-agent",
        user_id="user-1",
        user_message="修改一下内容，增加代码逻辑",
        task_intent=intent,
        task_context=TaskContext(
            task_id="task-agent",
            goal="AI Agent 学习路线",
            task_intent=intent,
            active_artifact_id="artifact-draft",
        ),
        active_draft_id="draft-agent",
        mcp=mcp,
    ))

    assert result.success is True
    assert calls == ["content.revise_draft"]
