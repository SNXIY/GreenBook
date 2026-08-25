"""Truthful public Activity projection tests.

These use explicit test doubles only.  They verify projection semantics, not
live Java/LLM integration.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_core.activity import (
    PostgresUserActivityStore,
    UserActivityProjector,
    UserActivityPublisher,
    UserActivityStore,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.queue_execution_handler import (
    RuntimeExecutionQueueHandler,
)
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import OperationReceipt, ResourceRef
from greenbook_contracts.user_activity import (
    UserActivityStatus,
    UserActivityType,
    activity_mapping_for_semantic_action,
)
from greenbook_mcp_server.tool_registry import list_tools
from sqlalchemy.pool import StaticPool


def _verified_receipt(
    action: str,
    *,
    resource_ref: ResourceRef | None = None,
) -> OperationReceipt:
    return OperationReceipt(
        operation_id=f"operation-{action.lower()}",
        semantic_action=action,
        result_known=True,
        status="COMPLETED",
        verification_evidence={"source": "test-authoritative-read"},
        resource_ref=resource_ref,
    )


def _result(
    action: str,
    *,
    data: dict[str, Any] | list[Any] | None = None,
    receipt: OperationReceipt | None = None,
    ok: bool = True,
    code: str = "OK",
    request_sent: bool | None = True,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        "data": data,
        "request_sent": request_sent,
        "operation_receipt": receipt.model_dump(mode="json") if receipt else None,
    }


def test_verified_write_is_the_only_draft_completion() -> None:
    receipt = _verified_receipt(
        "CREATE_DRAFT",
        resource_ref=ResourceRef(ref="draft:d-java", kind="DRAFT", resource_id="d-java"),
    )
    projected = UserActivityProjector().project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-create",
        semantic_action="CREATE_DRAFT",
        source_key="step-java-create",
        result=_result(
            "CREATE_DRAFT",
            data={"draft_id": "d-java", "title": "Java interview"},
            receipt=receipt,
        ),
    )

    assert projected is not None
    assert projected.event.activity_type == UserActivityType.DRAFT_CREATED
    assert projected.event.status == UserActivityStatus.COMPLETED
    assert projected.event.verified_at
    assert projected.event.resource_ref is not None
    assert projected.event.resource_ref.resource_id == "d-java"
    assert projected.event.safe_payload["title"] == "Java interview"


def test_every_registered_tool_declares_a_backend_owned_activity_mapping() -> None:
    missing = []
    for contract in list_tools():
        action = getattr(contract, "semantic_action", None)
        action_value = getattr(action, "value", action)
        if activity_mapping_for_semantic_action(str(action_value or "")) is None:
            missing.append(contract.name)
    assert missing == []


def test_unverified_write_and_unknown_delivery_never_become_completed() -> None:
    projector = UserActivityProjector()
    missing_proof = projector.project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-update",
        semantic_action="UPDATE_DRAFT",
        source_key="step-update-unverified",
        result=_result("UPDATE_DRAFT", data={"draft_id": "d-java"}),
    )
    unknown_receipt = OperationReceipt(
        operation_id="operation-unknown",
        semantic_action="UPDATE_SCHEDULE",
        request_sent=True,
        downstream_accepted=True,
        side_effect_started=True,
        result_known=False,
        status="RESULT_UNKNOWN",
    )
    unknown = projector.project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-schedule",
        semantic_action="UPDATE_SCHEDULE",
        source_key="step-schedule-unknown",
        result=_result(
            "UPDATE_SCHEDULE",
            data={"schedule_id": "s-java"},
            receipt=unknown_receipt,
        ),
    )

    assert missing_proof is not None
    assert missing_proof.event.activity_type == UserActivityType.RESULT_UNKNOWN
    assert missing_proof.event.status == UserActivityStatus.RESULT_UNKNOWN
    assert unknown is not None
    assert unknown.event.activity_type == UserActivityType.RESULT_UNKNOWN
    assert unknown.event.status != UserActivityStatus.FAILED


def test_waiting_external_keeps_activity_open_even_when_envelope_succeeds() -> None:
    projected = UserActivityProjector().project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-schedule",
        semantic_action="UPDATE_SCHEDULE",
        source_key="step-schedule-waiting",
        result={"ok": True, "status": "WAITING_EXTERNAL", "request_sent": True},
    )

    assert projected is None


def test_superseded_activity_is_not_projected_as_failure() -> None:
    projected = UserActivityProjector().project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-schedule",
        semantic_action="UPDATE_SCHEDULE",
        source_key="step-schedule-superseded",
        result={
            "ok": False,
            "status": "SUPERSEDED",
            "mutation_status": "SUPERSEDED",
            "request_sent": False,
        },
    )

    assert projected is None


def test_search_count_is_taken_from_real_list_data() -> None:
    projected = UserActivityProjector().project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-search",
        objective_id="goal-search",
        semantic_action="SEARCH_POSTS",
        source_key="step-search",
        result=_result(
            "SEARCH_POSTS",
            data=[{"post_id": "p1"}, {"post_id": "p2"}, {"post_id": "p3"}],
        ),
    )

    assert projected is not None
    assert projected.event.activity_type == UserActivityType.SEARCH_COMPLETED
    assert projected.event.safe_payload["result_count"] == 3


def test_known_failure_is_sanitized_to_a_business_message() -> None:
    projected = UserActivityProjector().project_result(
        conversation_id="conversation-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-schedule",
        semantic_action="UPDATE_SCHEDULE",
        source_key="step-missing",
        result={
            "ok": False,
            "code": "SCHEDULE_NOT_FOUND",
            "request_sent": True,
            "data": {"raw_exception": "httpx.ReadTimeout", "schedule_id": "s-old"},
        },
    )

    assert projected is not None
    assert projected.event.status == UserActivityStatus.FAILED
    assert "raw_exception" not in projected.event.safe_payload
    assert "httpx" not in str(projected.event.safe_payload)


def test_partial_success_preserves_each_business_fact_and_task_scope() -> None:
    store = UserActivityStore()
    publisher = UserActivityPublisher(store)
    draft_receipt = _verified_receipt(
        "UPDATE_DRAFT",
        resource_ref=ResourceRef(ref="draft:d-java", kind="DRAFT", resource_id="d-java"),
    )
    schedule_unknown = OperationReceipt(
        operation_id="schedule-uncertain",
        semantic_action="UPDATE_SCHEDULE",
        request_sent=True,
        result_known=False,
        status="RESULT_UNKNOWN",
    )

    draft_event = publisher.publish_result(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-draft",
        semantic_action="UPDATE_DRAFT",
        source_key="java-draft",
        result=_result(
            "UPDATE_DRAFT",
            data={"draft_id": "d-java", "title": "Java revised"},
            receipt=draft_receipt,
        ),
    )
    schedule_event = publisher.publish_result(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        task_id="task-java",
        objective_id="goal-schedule",
        semantic_action="UPDATE_SCHEDULE",
        source_key="java-schedule",
        result=_result(
            "UPDATE_SCHEDULE",
            data={"schedule_id": "s-java"},
            receipt=schedule_unknown,
        ),
    )

    assert draft_event is not None and schedule_event is not None
    assert draft_event.status == UserActivityStatus.COMPLETED
    assert schedule_event.status == UserActivityStatus.RESULT_UNKNOWN
    events = store.list_since(
        "conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )
    assert [event.task_id for event in events] == ["task-java", "task-java"]
    assert all(event.task_id != "task-agent" for event in events)


def test_cancel_schedule_keeps_draft_lifecycle_and_reopened_task_scope() -> None:
    store = UserActivityStore()
    publisher = UserActivityPublisher(store)
    receipt = _verified_receipt(
        "CANCEL_SCHEDULE",
        resource_ref=ResourceRef(ref="schedule:s-langgraph", kind="SCHEDULE", resource_id="s-langgraph"),
    )
    cancelled = publisher.publish_result(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-cancel",
        task_id="task-langgraph",
        objective_id="goal-schedule",
        semantic_action="CANCEL_SCHEDULE",
        source_key="cancel-schedule",
        result=_result(
            "CANCEL_SCHEDULE",
            data={"schedule_id": "s-langgraph", "draft_id": "d-langgraph", "status": "CANCELLED"},
            receipt=receipt,
        ),
    )
    reopened = publisher.publish_result(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-reopen",
        task_id="task-langgraph",
        objective_id="goal-draft-revision",
        semantic_action="UPDATE_DRAFT",
        source_key="reopen-draft",
        result=_result(
            "UPDATE_DRAFT",
            data={"draft_id": "d-langgraph", "title": "LangGraph revised"},
            receipt=_verified_receipt("UPDATE_DRAFT"),
        ),
    )

    assert cancelled is not None
    assert cancelled.activity_type == UserActivityType.SCHEDULE_CANCELLED
    assert cancelled.resource_ref is not None and cancelled.resource_ref.kind == "SCHEDULE"
    assert reopened is not None and reopened.task_id == "task-langgraph"
    assert all(
        event.activity_type != UserActivityType.DRAFT_DELETED
        for event in store.list_since(
            "conversation-1", user_id="user-1", tenant_id="tenant-1"
        )
    )


def test_waiting_clarification_and_approval_are_not_result_unknown() -> None:
    store = UserActivityStore()
    publisher = UserActivityPublisher(store)
    clarification_result = RuntimeResult(
        success=False,
        status="WAITING_HUMAN",
        run_id="run-clarify",
        task_id="task-java",
        error_code="AMBIGUOUS_TARGET",
        partial_results={
            "clarification": {
                "question": "Which article?",
                "candidates": [{"label": "JVM"}, {"label": "Spring Boot"}],
            }
        },
    )
    approval_result = RuntimeResult(
        success=False,
        status="WAITING_APPROVAL",
        run_id="run-approval",
        task_id="task-delete",
        approval_id="approval-1",
        approval_data={"description": "Delete draft"},
    )

    publisher.publish_runtime_result(
        clarification_result,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )
    publisher.publish_runtime_result(
        approval_result,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )
    events = store.list_since(
        "conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )

    assert [event.activity_type for event in events] == [
        UserActivityType.NEEDS_CLARIFICATION,
        UserActivityType.NEEDS_APPROVAL,
    ]
    assert [event.status for event in events] == [
        UserActivityStatus.WAITING_CLARIFICATION,
        UserActivityStatus.WAITING_APPROVAL,
    ]


def test_planning_event_does_not_fabricate_started_activity() -> None:
    store = UserActivityStore()
    publisher = UserActivityPublisher(store)

    result = publisher.publish_runtime_event(
        "SEMANTIC_ACTION_SELECTED",
        {"business_action": "CREATE_DRAFT", "task_id": "task-java"},
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )

    assert result is None
    assert store.list_since(
        "conversation-1", user_id="user-1", tenant_id="tenant-1"
    ) == []


def test_live_worker_event_and_completion_replay_share_one_idempotency_key() -> None:
    """A queue Worker emits live facts, then persists a replayable result.

    The completion hook must not turn that normal two-event lifecycle into
    four cards merely because it crosses a process/completion boundary.
    """

    store = UserActivityStore()
    publisher = UserActivityPublisher(store)
    receipt = _verified_receipt("CREATE_DRAFT")
    result = _result(
        "CREATE_DRAFT",
        data={"draft_id": "d-java", "title": "Java interview"},
        receipt=receipt,
    )
    live_payload = {
        "task_id": "task-java",
        "goal_id": "goal-create",
        "business_action": "CREATE_DRAFT",
        "execution_id": "execution-java",
        "step_id": "step-create",
        "activity_key": "execution:execution-java:step:step-create",
    }
    publisher.publish_runtime_event(
        "RUNTIME_TOOL_STARTED",
        live_payload,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )
    publisher.publish_runtime_event(
        "RUNTIME_TOOL_COMPLETED",
        {**live_payload, "result": result},
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )

    publisher.publish_runtime_result(
        RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id="run-1",
            task_id="task-java",
            activity_records=[{
                **live_payload,
                "capability": "CREATE_DRAFT",
                "result": result,
            }],
        ),
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )

    events = store.list_since(
        "conversation-1", user_id="user-1", tenant_id="tenant-1"
    )
    assert [(event.activity_type, event.status) for event in events] == [
        (UserActivityType.DRAFT_CREATING, UserActivityStatus.IN_PROGRESS),
        (UserActivityType.DRAFT_CREATED, UserActivityStatus.COMPLETED),
    ]


def test_store_orders_replays_and_dedupes_without_exposing_private_key() -> None:
    engine = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = PostgresUserActivityStore(engine)
    publisher = UserActivityPublisher(store)
    for action, source_key in (("SEARCH_POSTS", "search"), ("GET_DRAFT", "lookup")):
        publisher.publish_started(
            conversation_id="conversation-1",
            user_id="user-1",
            tenant_id="tenant-1",
            run_id="run-1",
            task_id="task-java",
            objective_id=None,
            semantic_action=action,
            source_key=source_key,
        )
    duplicate = publisher.publish_started(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        task_id="task-java",
        objective_id=None,
        semantic_action="SEARCH_POSTS",
        source_key="search",
    )
    events = store.list_since(
        "conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        after_sequence=0,
    )

    assert [event.sequence for event in events] == [1, 2]
    assert duplicate is not None and duplicate.sequence == 1
    assert store.list_since(
        "conversation-1", user_id="other", tenant_id="tenant-1"
    ) == []
    assert "dedupe_key" not in events[0].model_dump(mode="json")


class _WorkerService:
    async def execute_queued(self, message: ExecutionQueueMessage, **kwargs: Any) -> RuntimeResult:
        callback = kwargs["activity_callback"]
        payload = {
            "task_id": "task-agent",
            "goal_id": "goal-agent",
            "business_action": "CREATE_DRAFT",
            "tool_name": "content.create_draft",
            "activity_key": "execution:e-agent:step:create",
        }
        started = callback("RUNTIME_TOOL_STARTED", payload)
        if inspect.isawaitable(started):
            await started
        receipt = _verified_receipt("CREATE_DRAFT")
        completed = callback("RUNTIME_TOOL_COMPLETED", {
            **payload,
            "result": _result(
                "CREATE_DRAFT",
                data={"draft_id": "d-agent", "title": "Agent article"},
                receipt=receipt,
            ),
        })
        if inspect.isawaitable(completed):
            await completed
        return RuntimeResult(success=True, status="COMPLETED", run_id="run-worker")


@pytest.mark.asyncio
async def test_worker_path_projects_actual_tool_start_and_verified_completion() -> None:
    store = UserActivityStore()
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="")
    handler = RuntimeExecutionQueueHandler(
        service=_WorkerService(),
        mcp=SimpleNamespace(),
        credential_resolver=lambda _message: auth,
        user_activity_publisher=UserActivityPublisher(store),
    )
    await handler(ExecutionQueueMessage(
        execution_id="e-agent",
        payload={"conversation_id": "conversation-1", "run_id": "run-worker"},
    ))

    events = store.list_since(
        "conversation-1", user_id="user-1", tenant_id="tenant-1"
    )
    assert [(event.activity_type, event.status, event.task_id) for event in events] == [
        (UserActivityType.DRAFT_CREATING, UserActivityStatus.IN_PROGRESS, "task-agent"),
        (UserActivityType.DRAFT_CREATED, UserActivityStatus.COMPLETED, "task-agent"),
    ]
