"""Golden path for a detached Runtime execution with a long Creator task."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime.tool_runtime import AsyncTaskHandle
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager

from apps.assistant_api.greenbook_assistant_api.models.runtime_context import (
    RuntimeContext,
)
from apps.assistant_api.greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)


class _SlowCreateMcp:
    async def execute_tool(self, name: str, **_kwargs: Any) -> Any:
        if name == "content.create_draft":
            async def finish() -> dict[str, Any]:
                await asyncio.sleep(0.04)
                return {
                    "ok": True,
                    "code": "",
                    "data": {
                        "draft_id": "draft-long-1",
                        "title": "Java 学习路线",
                        "content": "从基础到实战的学习路径",
                    },
                }

            return AsyncTaskHandle("creator-long-1", finish())
        return {"ok": True, "code": "", "data": {}}


class _TimeoutCreateMcp:
    calls = 0

    async def execute_tool(self, name: str, **_kwargs: Any) -> Any:
        if name == "content.create_draft":
            self.calls += 1

            async def finish() -> dict[str, Any]:
                await asyncio.sleep(0.1)
                return {"ok": True, "code": "", "data": {"draft_id": "late"}}

            return AsyncTaskHandle(
                "creator-timeout-1",
                finish(),
                deadline=datetime.now(UTC) + timedelta(seconds=0.01),
            )
        return {"ok": True, "code": "", "data": {}}


@pytest.fixture(autouse=True)
def clear_runtime() -> None:
    ExecutionRepository.clear()


@pytest.mark.asyncio
async def test_long_creator_task_returns_running_then_completes() -> None:
    intent = type(
        "Intent",
        (),
        {
            "goal_category": "CREATE_CONTENT",
            "relation": "NEW_TASK",
            "requirements": [{"type": "CREATE"}],
        },
    )()
    context = RuntimeContext(
        run_id="long-run-1",
        trace_id="long-trace-1",
        task_id="long-task-1",
        user_id="user-1",
        tenant_id="tenant-1",
        task_intent=intent,
        user_message="写一篇 Java 学习文章",
        mcp=_SlowCreateMcp(),
        session=None,
    )

    service = RuntimeAgentService()
    accepted = await service.execute(context, detach=True)

    assert accepted.status == "RUNNING"
    assert accepted.execution_id
    execution = ExecutionRepository().find_by_id(accepted.execution_id)
    assert execution is not None
    assert execution.status.value == "RUNNING"
    assert execution.steps[0].status.value in {"PENDING", "RUNNING"}

    # The 120-second short-tool timeout must not turn the acknowledged task
    # into a failure while Creator is still working.
    await asyncio.sleep(0.02)
    mid = ExecutionRepository().find_by_id(accepted.execution_id)
    assert mid is not None
    assert mid.status.value != "FAILED"
    mid_events = ExecutionStateManager().event_store.list_events(accepted.execution_id)
    assert any(event.event_type.value == "STEP_STARTED" for event in mid_events)

    await asyncio.sleep(0.12)
    final = service.background_result(context.run_id)
    assert final is not None
    assert final.status == "COMPLETED"
    assert final.success is True
    final_events = ExecutionStateManager().event_store.list_events(accepted.execution_id)
    assert any(event.event_type.value == "STEP_COMPLETED" for event in final_events)
    assert any(event.event_type.value == "EXECUTION_COMPLETED" for event in final_events)


@pytest.mark.asyncio
async def test_content_create_draft_timeout_fails_execution() -> None:
    intent = type(
        "Intent",
        (),
        {
            "goal_category": "CREATE_CONTENT",
            "relation": "NEW_TASK",
            "requirements": [{"type": "CREATE"}],
        },
    )()
    mcp = _TimeoutCreateMcp()
    context = RuntimeContext(
        run_id="timeout-run-1",
        trace_id="timeout-trace-1",
        task_id="timeout-task-1",
        user_id="user-1",
        tenant_id="tenant-1",
        task_intent=intent,
        user_message="写一篇 Java 学习文章",
        mcp=mcp,
        session=None,
    )

    service = RuntimeAgentService()
    accepted = await service.execute(context, detach=True)
    await asyncio.sleep(0.08)

    final = service.background_result(context.run_id)
    assert final is not None
    assert final.status == "FAILED"
    assert final.error_code == "TIMEOUT"
    execution = ExecutionRepository().find_by_id(accepted.execution_id)
    assert execution is not None
    assert execution.status.value == "FAILED"
    assert execution.steps[0].status.value in {"FAILED", "FAILED_RETRYABLE"}
    assert mcp.calls == 1
    events = ExecutionStateManager().event_store.list_events(accepted.execution_id)
    assert any(event.event_type.value == "STEP_FAILED" for event in events)
    assert any(event.event_type.value == "EXECUTION_FAILED" for event in events)
