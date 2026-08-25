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
from greenbook_agent_core.human import ApprovalRequest
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


class _ApprovalService:
    async def capture_result(self, *_args, **_kwargs):
        return ApprovalRequest(
            approval_id="approval-1",
            execution_id="execution-1",
            conversation_id="conversation-1",
            user_id="user-1",
            tenant_id="tenant-1",
            message="Approve publish",
        )


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
async def test_approval_identity_survives_completion_boundary() -> None:
    publisher = ExecutionCompletionPublisher(
        conversation_service=_ConversationService(),
        run_store={},
        approval_service=_ApprovalService(),
    )
    result = RuntimeResult(
        success=False,
        status="WAITING_APPROVAL",
        run_id="run-1",
        execution_id="execution-1",
    )
    await publisher(
        ExecutionQueueMessage(
            execution_id="execution-1",
            payload={"run_id": "run-1", "conversation_id": "conversation-1"},
        ),
        result,
        AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token"),
    )
    assert result.approval_id == "approval-1"


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


@pytest.mark.asyncio
async def test_reconcile_reprojects_prior_result_unknown_with_execution_truth() -> None:
    class _TaskProvider:
        def __init__(self) -> None:
            self.statuses: list[str] = []

        async def persist_completion_projection(self, _scope, **kwargs):
            self.statuses.append(kwargs["status"])
            return SimpleNamespace(status="COMPLETED")

    projections = MemoryExecutionResultProjectionStore()
    projections.save(ExecutionResultProjection(
        execution_id="execution-unknown",
        task_id="task-unknown",
        conversation_id="conversation-1",
        run_id="run-unknown",
        trace_id="trace-unknown",
        status="RESULT_UNKNOWN",
        summary="write acknowledgement lost",
        assistant_response={"message": "write acknowledgement lost", "status": "RESULT_UNKNOWN"},
    ))
    task_provider = _TaskProvider()
    publisher = ExecutionCompletionPublisher(
        conversation_service=_ConversationService(),
        result_projection_store=projections,
        task_provider=task_provider,
        run_store={},
    )
    message = ExecutionQueueMessage(
        execution_id="execution-unknown",
        trace_id="trace-unknown",
        payload={
            "run_id": "run-unknown",
            "task_id": "task-unknown",
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        },
    )

    await publisher.reconcile(message, SimpleNamespace(status="COMPLETED"))

    assert task_provider.statuses == ["COMPLETED"]
    assert projections.get("execution-unknown").status == "COMPLETED"


@pytest.mark.asyncio
async def test_reconcile_replaces_stale_running_step_with_terminal_execution_step() -> None:
    projections = MemoryExecutionResultProjectionStore()
    projections.save(ExecutionResultProjection(
        execution_id="execution-unknown-step",
        task_id="task-unknown-step",
        conversation_id="conversation-1",
        run_id="run-unknown-step",
        trace_id="trace-unknown-step",
        status="RESULT_UNKNOWN",
        summary="write acknowledgement lost",
        assistant_response={
            "message": "write acknowledgement lost",
            "status": "RESULT_UNKNOWN",
            "steps": [{"capability": "GENERATE_CONTENT", "status": "RUNNING"}],
        },
    ))
    publisher = ExecutionCompletionPublisher(
        conversation_service=_ConversationService(),
        result_projection_store=projections,
        run_store={},
    )
    message = ExecutionQueueMessage(
        execution_id="execution-unknown-step",
        trace_id="trace-unknown-step",
        payload={
            "run_id": "run-unknown-step",
            "task_id": "task-unknown-step",
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        },
    )
    execution = SimpleNamespace(
        status="COMPLETED",
        steps=[SimpleNamespace(
            step_id="step-1",
            goal_id="goal-1",
            capability="GENERATE_CONTENT",
            status="COMPLETED",
            retry_count=0,
            error_code="",
            error_message="",
            started_at="",
            completed_at="2026-08-23T18:48:30Z",
        )],
    )

    await publisher.reconcile(message, execution)

    restored = projections.get("execution-unknown-step")
    assert restored is not None
    assert restored.status == "COMPLETED"
    assert restored.assistant_response["steps"][0]["status"] == "COMPLETED"


def test_reconciliation_resource_evidence_is_projected_without_internal_ids() -> None:
    from greenbook_agent_api.main import _reconciled_artifacts_from_operation

    operation = SimpleNamespace(
        resource_type="DRAFT",
        resource_id="draft-authoritative-1",
        semantic_action="CREATE_DRAFT",
        expected_postcondition={
            "arguments": {"title": "Authoritative draft"},
        },
        evidence=SimpleNamespace(
            resource_refs=[{
                "ref": "draft:draft-authoritative-1",
                "kind": "DRAFT",
                "resource_id": "draft-authoritative-1",
            }],
        ),
    )

    artifacts = _reconciled_artifacts_from_operation(operation)

    assert artifacts == [{
        "type": "DRAFT",
        "artifact_type": "DRAFT",
        "resource_type": "DRAFT",
        "resource_id": "draft-authoritative-1",
        "step_id": "",
        "status": "DRAFT",
        "title": "Authoritative draft",
    }]
    assert not any(
        key in artifacts[0]
        for key in ("objective_id", "task_id", "execution_id", "operation_id")
    )


def test_reconciliation_uses_evidence_ref_when_operation_resource_id_is_missing() -> None:
    from greenbook_agent_api.main import _reconciled_artifacts_from_operation

    operation = SimpleNamespace(
        resource_type="DRAFT",
        resource_id="",
        semantic_action="CREATE_DRAFT",
        expected_postcondition={
            "arguments": {"title": "Evidence-only draft"},
        },
        evidence=SimpleNamespace(
            resource_refs=[{
                "ref": "draft:draft-from-evidence",
                "kind": "DRAFT",
                "resource_id": "draft-from-evidence",
            }],
        ),
    )

    artifacts = _reconciled_artifacts_from_operation(operation)

    assert artifacts == [{
        "type": "DRAFT",
        "artifact_type": "DRAFT",
        "resource_type": "DRAFT",
        "resource_id": "draft-from-evidence",
        "step_id": "",
        "status": "DRAFT",
        "title": "Evidence-only draft",
    }]


@pytest.mark.asyncio
async def test_reconcile_prefers_authoritative_artifacts_when_old_projection_is_empty() -> None:
    class _TaskProvider:
        async def persist_completion_projection(self, _scope, **kwargs):
            assert kwargs["artifacts"][0]["resource_id"] == "draft-authoritative-1"
            return SimpleNamespace(status="COMPLETED")

    projections = MemoryExecutionResultProjectionStore()
    projections.save(ExecutionResultProjection(
        execution_id="execution-unknown-artifacts",
        task_id="task-unknown-artifacts",
        conversation_id="conversation-1",
        run_id="run-unknown-artifacts",
        trace_id="trace-unknown-artifacts",
        status="RESULT_UNKNOWN",
        assistant_response={"message": "write acknowledgement lost", "status": "RESULT_UNKNOWN"},
    ))
    publisher = ExecutionCompletionPublisher(
        conversation_service=_ConversationService(),
        result_projection_store=projections,
        task_provider=_TaskProvider(),
        run_store={},
    )
    message = ExecutionQueueMessage(
        execution_id="execution-unknown-artifacts",
        trace_id="trace-unknown-artifacts",
        payload={
            "run_id": "run-unknown-artifacts",
            "task_id": "task-unknown-artifacts",
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        },
    )

    await publisher.reconcile(
        message,
        SimpleNamespace(status="COMPLETED"),
        result=RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id="run-unknown-artifacts",
            task_id="task-unknown-artifacts",
            execution_id="execution-unknown-artifacts",
            artifacts=[{
                "type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": "draft-authoritative-1",
                "status": "DRAFT",
            }],
        ),
    )

    assert projections.get("execution-unknown-artifacts").status == "COMPLETED"
    assert projections.get("execution-unknown-artifacts").artifacts[0]["resource_id"] == "draft-authoritative-1"


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
