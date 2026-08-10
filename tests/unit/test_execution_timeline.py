"""Phase13-C execution timeline read-model tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_assistant_core.execution.timeline import (
    ExecutionTimelineService,
    TimelineItemKind,
)
from greenbook_assistant_core.observability.context import TraceContext


def test_timeline_preserves_trace_ids_and_categorizes_runtime_facts() -> None:
    events = ExecutionEventStore()
    operations = ExternalOperationStore()
    context = TraceContext(
        conversation_id="conversation-1",
        run_id="run-1",
        trace_id="trace-1",
        task_id="task-1",
        execution_id="execution-1",
        step_id="step-1",
        invocation_id="invocation-1",
        tool_call_id="tool-call-1",
        operation_id="operation-1",
    )
    events.append(ExecutionEvent(
        execution_id="execution-1",
        event_type=EventType.EXECUTION_STARTED,
        timestamp="2026-08-10T00:00:00+00:00",
        trace_context=context,
    ))
    events.append(ExecutionEvent(
        execution_id="execution-1",
        event_type=EventType.STEP_COMPLETED,
        step_id="step-1",
        timestamp="2026-08-10T00:00:01+00:00",
        trace_context=context,
        payload={
            "tool_name": "content.create_draft",
            "operation_id": "operation-1",
            "evidence": {
                "execution_id": "execution-1",
                "step_id": "step-1",
                "invocation_id": "invocation-1",
                "tool_call_id": "tool-call-1",
                "operation_id": "operation-1",
                "request_sent": True,
                "side_effect_state": "POSSIBLE",
                "receipt_id": "receipt-1",
                "trace_id": "trace-1",
            },
        },
    ))
    events.append(ExecutionEvent(
        execution_id="execution-1",
        event_type=EventType.STEP_RETRY_REQUESTED,
        step_id="step-1",
        timestamp="2026-08-10T00:00:02+00:00",
        trace_context=context,
        payload={"retry_decision": {"allowed": True}},
    ))
    operations.create(ExternalOperationRecord(
        operation_id="operation-1",
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.create_draft",
        status=OperationStatus.SUCCEEDED,
        external_operation_id="external-1",
        receipt_id="receipt-1",
    ))

    timeline = ExecutionTimelineService(events, operations).build("execution-1")

    assert [item.kind for item in timeline.items] == [
        TimelineItemKind.EVENT,
        TimelineItemKind.TOOL,
        TimelineItemKind.RETRY,
        TimelineItemKind.EXTERNAL_OPERATION,
    ]
    tool = timeline.items[1]
    assert tool.trace_context is not None
    assert tool.trace_context.trace_id == "trace-1"
    assert tool.invocation_id == "invocation-1"
    assert tool.tool_call_id == "tool-call-1"
    assert tool.receipt_id == "receipt-1"
    assert timeline.items[-1].operation_status == "SUCCEEDED"


def test_timeline_is_empty_for_an_eventless_execution() -> None:
    timeline = ExecutionTimelineService(ExecutionEventStore()).build("empty")

    assert timeline.execution_id == "empty"
    assert timeline.items == []


@pytest.mark.asyncio
async def test_timeline_api_uses_existing_authorization_boundary() -> None:
    from apps.assistant_api.greenbook_assistant_api.api.runtime_routes import (
        get_execution_timeline,
    )

    class Manager:
        event_store = ExecutionEventStore()

        def get_execution(self, execution_id: str):
            if execution_id == "missing":
                raise ValueError("not found")
            return SimpleNamespace(execution_id=execution_id)

        def list_events(self, execution_id: str):
            return self.event_store.list_events(execution_id)

    manager = Manager()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                execution_runtime_manager=manager,
                external_operation_store=ExternalOperationStore(),
                execution_authorizer=lambda _auth, _execution: True,
            )
        ),
        state=SimpleNamespace(auth_context=SimpleNamespace(user_id="u")),
    )

    response = await get_execution_timeline("execution-api", request)

    assert response.execution_id == "execution-api"
    assert response.items == []
