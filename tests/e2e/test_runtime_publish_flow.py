"""Golden-path Runtime execution tests for create + scheduled publish."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.task.models import TaskIntent

USER_REQUEST = "五分钟之后发布一篇关于如何学好 Java 的帖子"


@pytest.fixture(autouse=True)
def clear_runtime_state() -> None:
    # PlanExecution is the canonical state source for this test.  In
    # particular, no assistant_runs projection is consulted.
    ExecutionRepository.clear()


def _schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "content.create_draft",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication.schedule",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "run_at": {"type": "string"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["draft_id", "run_at"],
                },
            },
        },
    ]


def _context(mcp: Any) -> RuntimeContext:
    intent = TaskIntent(
        goal=USER_REQUEST,
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
        constraints=[{"type": "TIME", "value": "五分钟之后"}],
    )
    return RuntimeContext(
        run_id="golden-run",
        trace_id="golden-trace",
        task_id="golden-publish-task",
        user_id="user-1",
        tenant_id="tenant-1",
        timezone="Asia/Shanghai",
        user_message=USER_REQUEST,
        task_intent=intent,
        mcp=mcp,
        session=None,
    )


def _mcp(
    *,
    create_result: dict[str, Any],
    schedule_result: dict[str, Any] | None,
    calls: list[tuple[str, dict[str, Any]]],
) -> Any:
    class FakeMCP:
        def get_tool_definitions(self) -> list[dict[str, Any]]:
            return _schemas()

        async def execute_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((name, kwargs))
            if name == "content.create_draft":
                return create_result
            if name == "publication.schedule" and schedule_result is not None:
                return schedule_result
            return {
                "ok": False,
                "code": "UNEXPECTED_TOOL",
                "message": f"Unexpected tool: {name}",
            }

    return FakeMCP()


@pytest.mark.asyncio
async def test_runtime_publish_flow_completes_and_binds_schedule_time() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    mcp = _mcp(
        create_result={
            "ok": True,
            "code": "",
            "data": {
                "draft_id": "draft-golden",
                "title": "如何学好Java",
                "content": "Java 学习路线",
            },
        },
        schedule_result={
            "ok": True,
            "code": "",
            "data": {
                "schedule_id": "schedule-golden",
                "draft_id": "draft-golden",
                "run_at": "2026-08-09T00:05:00Z",
                "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
            },
        },
        calls=calls,
    )

    context = _context(mcp)
    result = await RuntimeAgentService().execute(context)

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.execution_path == "runtime"
    assert result.execution_id
    draft_artifact = next(
        artifact for artifact in result.artifacts
        if artifact["artifact_type"] == "DRAFT"
    )
    assert draft_artifact["data"]["title"] == "如何学好Java"
    assert draft_artifact["data"]["content"] == "Java 学习路线"
    schedule_artifact = next(
        artifact for artifact in result.artifacts
        if artifact["artifact_type"] == "SCHEDULE"
    )
    assert schedule_artifact["data"]["status"] == "SCHEDULED"

    execution = ExecutionRepository().find_by_id(result.execution_id)
    assert execution is not None
    assert [step.capability for step in execution.steps] == [
        "GENERATE_CONTENT",
        "VALIDATE_QUALITY",
        "SCHEDULE_PUBLISH",
    ]
    assert execution.status.value == "COMPLETED"
    assert all(step.status.value == "COMPLETED" for step in execution.steps)
    schedule_constraints = execution.steps[-1].checkpoint_data["constraints"]
    assert schedule_constraints["run_at"]
    assert "time" not in schedule_constraints

    schedule_calls = [name_args for name_args in calls if name_args[0] == "publication.schedule"]
    assert len(schedule_calls) == 1
    schedule_args = schedule_calls[0][1]
    assert schedule_args["draft_id"] == "draft-golden"
    assert schedule_args["run_at"]
    assert "time" not in schedule_args


@pytest.mark.asyncio
async def test_runtime_publish_flow_propagates_tool_failure() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    mcp = _mcp(
        create_result={
            "ok": False,
            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
            "message": "title and content are required",
            "user_message": "Invalid draft arguments",
        },
        schedule_result=None,
        calls=calls,
    )

    context = _context(mcp)
    result = await RuntimeAgentService().execute(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result.execution_id
    assert "已完成" not in result.content

    execution = ExecutionRepository().find_by_id(result.execution_id)
    assert execution is not None
    assert execution.status.value == "FAILED"
    assert execution.steps[0].capability == "GENERATE_CONTENT"
    assert execution.steps[0].status.value == "FAILED"
    assert execution.steps[0].error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert all(name != "publication.schedule" for name, _ in calls)
