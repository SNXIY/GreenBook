"""Queue dispatch integration at the RuntimeAgentService boundary."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_api.models.runtime_context import (
    RuntimeContext,
    TargetContext,
    TaskContext,
)
from greenbook_assistant_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.execution_queue import ExecutionQueue
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.task.models import TaskIntent


@pytest.mark.asyncio
async def test_queued_runtime_is_consumed_against_existing_execution() -> None:
    repository = ExecutionRepository()
    event_store = ExecutionEventStore()
    queue = ExecutionQueue()
    calls: list[str] = []

    async def execute_tool(tool_name: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(tool_name)
        return {
            "ok": True,
            "code": "",
            "data": {"draft_id": "queued-draft", "title": "Queued"},
        }

    class MCP:
        pass

    MCP.execute_tool = staticmethod(execute_tool)

    intent = TaskIntent(
        relation="NEW_TASK",
        goal="Write a queued post",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    session = SessionContext(
        conversation_id="conversation-queue",
        user_id="user-queue",
        tenant_id="tenant-queue",
    )
    auth = type(
        "Auth",
        (),
        {
            "user_id": "user-queue",
            "tenant_id": "tenant-queue",
            "roles": [],
            "session_id": None,
            "token_id": None,
            "timezone": "Asia/Shanghai",
            "raw_access_token": "worker-token",
            "model_dump": lambda self, **_kwargs: {
                "user_id": self.user_id,
                "tenant_id": self.tenant_id,
                "roles": self.roles,
            },
        },
    )()
    ctx = RuntimeContext(
        conversation_id="conversation-queue",
        run_id="run-queue",
        trace_id="trace-queue",
        task_id="task-queue",
        user_id="user-queue",
        tenant_id="tenant-queue",
        user_message="Write a queued post",
        task_intent=intent,
        task_context=TaskContext(
            task_id="task-queue",
            goal="Write a queued post",
            task_intent=intent,
            target=TargetContext(
                task_id="task-queue",
                artifact_id="artifact-queue",
                resource_id="draft-queue",
                resource_kind="DRAFT",
            ),
        ),
        session=session,
        auth=auth,
        mcp=MCP(),
    )

    api_service = RuntimeAgentService(
        repository=repository,
        event_store=event_store,
        execution_queue=queue,
        dispatch_mode="queue",
    )
    queued = await api_service.execute(ctx)
    assert queued.status == "QUEUED"
    assert queued.execution_id is not None
    assert calls == []

    message = queue.get_by_execution_id(queued.execution_id)
    assert message is not None
    assert message.payload["task_context"]["target"] == {
        "task_id": "task-queue",
        "artifact_id": "artifact-queue",
        "resource_id": "draft-queue",
        "resource_kind": "DRAFT",
    }
    worker_service = RuntimeAgentService(
        repository=repository,
        event_store=event_store,
    )
    completed = await worker_service.execute_queued(
        message,
        mcp=MCP(),
        auth=auth,
    )

    assert completed.execution_id == queued.execution_id
    assert completed.status == "COMPLETED"
    assert calls == ["content.create_draft"]
    assert len(repository.list_all()) == 1
