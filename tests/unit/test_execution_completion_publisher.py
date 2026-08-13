from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_api.models.runtime_result import RuntimeResult
from greenbook_agent_api.services.execution_completion_publisher import (
    ExecutionCompletionPublisher,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.result_projection import (
    ExecutionResultProjection,
    MemoryExecutionResultProjectionStore,
)
from greenbook_contracts.identity import AuthContext


class _ConversationService:
    def __init__(self) -> None:
        self.messages = [{
            "message_id": "user-message",
            "role": "user",
            "content": "create a post",
            "trace_id": "trace-1",
        }]

    async def list_messages(self, _conversation_id: str, **_scope):
        return [dict(item) for item in self.messages]

    async def append_message(self, _conversation_id: str, **message):
        self.messages.append(dict(message))


@pytest.mark.asyncio
async def test_queued_completion_publishes_assistant_message_once() -> None:
    conversation_service = _ConversationService()
    run_store = {
        "run-1": {
            "run_id": "run-1",
            "status": "QUEUED",
            "content": "",
        }
    }
    publisher = ExecutionCompletionPublisher(
        conversation_service=conversation_service,
        run_store=run_store,
    )
    message = ExecutionQueueMessage(
        execution_id="execution-1",
        trace_id="trace-1",
        payload={
            "run_id": "run-1",
            "conversation_id": "conversation-1",
        },
    )
    result = RuntimeResult(
        success=True,
        status="COMPLETED",
        run_id="run-1",
        execution_id="execution-1",
        trace_id="trace-1",
        content="The post has been created.",
    )
    auth = AuthContext(
        user_id="user-1",
        tenant_id="tenant-1",
        raw_access_token="validated-token",
    )

    await publisher(message, result, auth)
    await publisher(message, result, auth)

    assistant_messages = [
        item for item in conversation_service.messages if item["role"] == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "The post has been created."
    assert run_store["run-1"]["status"] == "COMPLETED"
    assert run_store["run-1"]["execution_id"] == "execution-1"


@pytest.mark.asyncio
async def test_reconcile_restores_projection_for_previous_completed_execution() -> None:
    conversation_service = _ConversationService()
    publisher = ExecutionCompletionPublisher(
        conversation_service=conversation_service,
        run_store={},
    )
    message = ExecutionQueueMessage(
        execution_id="execution-old",
        trace_id="trace-old",
        payload={
            "run_id": "run-old",
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "user_message": "create a Java learning post",
        },
    )

    restored = await publisher.reconcile(
        message,
        SimpleNamespace(status="COMPLETED"),
    )

    assert restored is True
    assert conversation_service.messages[-1]["role"] == "assistant"
    assert conversation_service.messages[-1]["content"] == "执行已完成。"
    assert conversation_service.messages[-1]["parts"][0]["type"] == "execution_result"


class _MissingConversationContext:
    def __init__(self) -> None:
        self.projection_methods_called = False

    async def get_conversation(self, _conversation_id: str, **_scope):
        return None

    async def load(self, _conversation_id: str, **_scope):
        self.projection_methods_called = True
        raise AssertionError("orphaned reconciliation must not load context")

    async def list_messages(self, _conversation_id: str, **_scope):
        self.projection_methods_called = True
        raise AssertionError("orphaned reconciliation must not list messages")


@pytest.mark.asyncio
async def test_reconcile_skips_projection_when_historical_conversation_is_missing() -> None:
    conversation_service = _MissingConversationContext()
    projection_store = MemoryExecutionResultProjectionStore()
    projection_store.save(ExecutionResultProjection(
        execution_id="execution-orphan",
        task_id="task-orphan",
        conversation_id="conversation-deleted",
        run_id="run-orphan",
        trace_id="trace-orphan",
        status="COMPLETED",
        summary="Historical completed execution",
        assistant_response={
            "message": "Historical completed execution",
            "status": "COMPLETED",
        },
    ))
    publisher = ExecutionCompletionPublisher(
        conversation_service=conversation_service,
        result_projection_store=projection_store,
        run_store={},
    )
    message = ExecutionQueueMessage(
        execution_id="execution-orphan",
        trace_id="trace-orphan",
        payload={
            "run_id": "run-orphan",
            "task_id": "task-orphan",
            "conversation_id": "conversation-deleted",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        },
    )

    restored = await publisher.reconcile(
        message,
        SimpleNamespace(status="COMPLETED"),
    )

    assert restored is False
    assert conversation_service.projection_methods_called is False
    assert projection_store.get("execution-orphan") is not None
