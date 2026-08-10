"""Phase13-D Runtime verification through the API/queue/worker boundary."""

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
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueStatus,
)
from greenbook_assistant_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_assistant_core.execution.evidence import ExecutionEvidence
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    ExternalOperationTracker,
    OperationStatus,
)
from greenbook_assistant_core.execution.reconciliation import (
    ReconciliationRecoveryService,
    ReconciliationService,
)
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.retry_manager import RetryManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.execution.timeline import (
    ExecutionTimelineService,
    TimelineItemKind,
)
from greenbook_assistant_core.observability.context import TraceContext
from greenbook_assistant_core.observability.metrics import MemoryMetricsCollector
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator
from greenbook_assistant_core.task.models import TaskIntent
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_contracts import SideEffectState


@pytest.fixture(autouse=True)
def clear_execution_repository() -> None:
    ExecutionRepository.clear()


class _VerificationMCP:
    """Deterministic MCP boundary used by the real Runtime worker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            _tool_schema(
                "community.search_public_posts",
                {"query": {"type": "string"}},
                required=["query"],
            ),
            _tool_schema(
                "content.create_draft",
                {
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                },
                required=["title", "instruction"],
            ),
            _tool_schema(
                "publication.schedule",
                {
                    "draft_id": {"type": "string"},
                    "run_at": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                required=["run_at"],
            ),
        ]

    async def execute_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, dict(kwargs)))
        if name == "community.search_public_posts":
            return {
                "ok": True,
                "code": "",
                "data": {
                    "items": [
                        {
                            "post_id": "post-java-1",
                            "title": "Learning Java",
                            "summary": "A Java learning path",
                        }
                    ],
                },
            }
        if name == "content.create_draft":
            return {
                "ok": True,
                "code": "",
                "data": {
                    "draft_id": "draft-java-1",
                    "title": "How to learn Java",
                    "content": "A practical Java learning path.",
                },
            }
        if name == "publication.schedule":
            return {
                "ok": True,
                "code": "",
                "data": {
                    "schedule_id": "schedule-java-1",
                    "draft_id": kwargs.get("draft_id", "draft-java-1"),
                    "run_at": kwargs.get("run_at", "2026-08-11T08:00:00+08:00"),
                    "status": "SCHEDULED",
                },
            }
        return {"ok": False, "code": "UNEXPECTED_TOOL", "message": name}


def _tool_schema(
    name: str,
    properties: dict[str, dict[str, str]],
    *,
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _context(
    *,
    run_id: str,
    goal: str,
    goal_category: str,
    requirements: list[dict[str, str]],
    constraints: list[dict[str, str]] | None = None,
    mcp: _VerificationMCP,
) -> RuntimeContext:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal=goal,
        goal_category=goal_category,
        requirements=requirements,
        constraints=constraints or [],
    )
    task_id = f"task-{run_id}"
    return RuntimeContext(
        conversation_id=f"conversation-{run_id}",
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        task_id=task_id,
        user_id="verification-user",
        tenant_id="verification-tenant",
        timezone="Asia/Shanghai",
        user_message=goal,
        task_intent=intent,
        task_context=TaskContext(
            task_id=task_id,
            goal=goal,
            task_intent=intent,
            target=TargetContext(
                task_id=task_id,
                artifact_id=f"artifact-{run_id}",
                resource_kind="TASK",
            ),
        ),
        session=SessionContext(
            conversation_id=f"conversation-{run_id}",
            user_id="verification-user",
            tenant_id="verification-tenant",
        ),
        mcp=mcp,
    )


async def _run_through_queue(
    context: RuntimeContext,
) -> tuple[Any, ExecutionEventStore, ExternalOperationStore, MemoryMetricsCollector, _VerificationMCP]:
    repository = ExecutionRepository()
    event_store = ExecutionEventStore()
    operation_store = ExternalOperationStore()
    tracker = ExternalOperationTracker(store=operation_store)
    queue = ExecutionQueue()
    metrics = MemoryMetricsCollector()
    api_service = RuntimeAgentService(
        repository=repository,
        event_store=event_store,
        execution_queue=queue,
        dispatch_mode="queue",
        operation_tracker=tracker,
        metrics_collector=metrics,
    )

    queued = await api_service.execute(context)
    assert queued.status == "QUEUED"
    assert queued.execution_id
    assert context.mcp.calls == []

    message = queue.get_by_execution_id(queued.execution_id)
    assert message is not None
    results: list[Any] = []
    worker_service = RuntimeAgentService(
        repository=repository,
        event_store=event_store,
        operation_tracker=tracker,
        metrics_collector=metrics,
    )

    async def handle(owned_message) -> None:
        results.append(
            await worker_service.execute_queued(
                owned_message,
                mcp=context.mcp,
            )
        )

    queue_worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handle,
        worker_id=f"verification-worker-{context.run_id}",
        poll_interval_seconds=0,
    )
    handled = await queue_worker.run_once()

    assert len(handled) == 1
    assert queue.get(message.message_id).status == ExecutionQueueStatus.ACKED
    assert len(results) == 1
    result = results[0]
    assert result.execution_id == queued.execution_id
    assert result.status == "COMPLETED"
    return result, event_store, operation_store, metrics, context.mcp


@pytest.mark.asyncio
async def test_query_post_analysis_crosses_api_queue_worker_tool_and_event() -> None:
    mcp = _VerificationMCP()
    result, events, operations, metrics, _ = await _run_through_queue(
        _context(
            run_id="query-post-analysis",
            goal="Find and analyze Java community posts",
            goal_category="ANALYZE_COMMUNITY",
            requirements=[{"type": "SEARCH"}],
            mcp=mcp,
        )
    )

    assert result.success is True
    assert [name for name, _ in mcp.calls] == ["community.search_public_posts"]
    _assert_trace_chain(result.execution_id, events, operations, "trace-query-post-analysis")
    snapshot = metrics.snapshot()
    assert snapshot.execution_success == 1
    assert snapshot.tool_invocation_count == 1
    assert snapshot.tool_error_count == 0


@pytest.mark.asyncio
async def test_content_creation_is_executed_only_by_queue_worker() -> None:
    mcp = _VerificationMCP()
    result, events, operations, metrics, _ = await _run_through_queue(
        _context(
            run_id="content-creation",
            goal="Create a post about how to learn Java",
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}],
            mcp=mcp,
        )
    )

    assert result.success is True
    assert [name for name, _ in mcp.calls] == ["content.create_draft"]
    assert any(artifact["artifact_type"] == "DRAFT" for artifact in result.artifacts)
    _assert_trace_chain(result.execution_id, events, operations, "trace-content-creation")
    snapshot = metrics.snapshot()
    assert snapshot.execution_success == 1
    assert snapshot.step_total == 1
    assert snapshot.tool_invocation_count == 1


@pytest.mark.asyncio
async def test_create_and_schedule_publish_crosses_multiple_worker_steps() -> None:
    mcp = _VerificationMCP()
    result, events, operations, metrics, _ = await _run_through_queue(
        _context(
            run_id="scheduled-publish",
            goal="Create a Java post and schedule it for tomorrow morning",
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
            constraints=[{
                "type": "TIME",
                "value": "2026-08-11T08:00:00+08:00",
            }],
            mcp=mcp,
        )
    )

    assert result.success is True
    assert [name for name, _ in mcp.calls] == [
        "content.create_draft",
        "publication.schedule",
    ]
    assert any(artifact["artifact_type"] == "SCHEDULE" for artifact in result.artifacts)
    schedule_args = next(args for name, args in mcp.calls if name == "publication.schedule")
    assert schedule_args["draft_id"] == "draft-java-1"
    _assert_trace_chain(result.execution_id, events, operations, "trace-scheduled-publish")
    timeline = ExecutionTimelineService(events, operations).build(result.execution_id)
    assert any(item.kind == TimelineItemKind.TOOL for item in timeline.items)
    assert any(item.kind == TimelineItemKind.EXTERNAL_OPERATION for item in timeline.items)
    snapshot = metrics.snapshot()
    assert snapshot.execution_success == 1
    assert snapshot.tool_invocation_count == 2
    assert snapshot.average_execution_duration_ms >= 0


def _assert_trace_chain(
    execution_id: str,
    events: ExecutionEventStore,
    operations: ExternalOperationStore,
    trace_id: str,
) -> None:
    canonical = events.list_events(execution_id)
    assert canonical
    assert any(event.event_type == EventType.EXECUTION_STARTED for event in canonical)
    assert any(event.event_type == EventType.EXECUTION_COMPLETED for event in canonical)
    for event in canonical:
        assert event.trace_context is not None
        assert event.trace_context.trace_id == trace_id
        assert event.trace_context.execution_id == execution_id
    records = operations.find_by_execution_id(execution_id)
    assert records
    assert all(record.execution_id == execution_id for record in records)
    assert all(record.evidence is not None for record in records)
    assert all(record.evidence.trace_id == trace_id for record in records if record.evidence)


def test_retry_and_reconciliation_events_are_visible_in_timeline() -> None:
    registry = CapabilityRegistry()
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="timeline-recovery-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    store = ExecutionRepository()
    event_store = ExecutionEventStore()
    state = ExecutionStateManager(store, event_store=event_store)
    execution = state.init_execution(plan, executable)
    state.bind_trace_context(
        execution.execution_id,
        TraceContext(
            conversation_id="recovery-conversation",
            run_id="recovery-run",
            trace_id="recovery-trace",
            execution_id=execution.execution_id,
        ),
    )
    state.start_execution(execution.execution_id)
    step = state.start_step(execution.execution_id, execution.steps[0].step_execution_id)
    evidence = ExecutionEvidence(
        execution_id=execution.execution_id,
        step_id=step.step_id,
        invocation_id="recovery-invocation",
        operation_id="recovery-operation",
        request_sent=False,
        side_effect_state=SideEffectState.NONE,
        error_code="TIMEOUT",
        trace_id="recovery-trace",
    )
    event_store.append(ExecutionEvent(
        execution_id=execution.execution_id,
        event_type=EventType.STEP_FAILED,
        step_id=step.step_id,
        payload={
            "step_execution_id": step.step_execution_id,
            "error_code": "TIMEOUT",
            "error_message": "pre-send timeout",
            "retryable": True,
            "evidence": evidence.model_dump(mode="json"),
        },
    ))
    state.fail_step(
        execution.execution_id,
        step.step_execution_id,
        error_code="TIMEOUT",
        error_message="pre-send timeout",
    )

    retried = RetryManager(state_manager=state).retry_step(
        execution.execution_id,
        step.step_id,
        source="verification",
        user_requested_retry=True,
    )
    assert retried.status.value == "PENDING"

    operation_store = ExternalOperationStore()
    operation = ExternalOperationRecord(
        operation_id="recovery-operation",
        execution_id=execution.execution_id,
        step_id=step.step_id,
        tool_name="content.create_draft",
        status=OperationStatus.UNKNOWN,
        external_operation_id="creator-operation-1",
        evidence=evidence,
    )
    recovery = ReconciliationRecoveryService(
        state_manager=state,
        reconciliation=ReconciliationService(
            store=operation_store,
            query=lambda **_identifiers: OperationStatus.SUCCEEDED,
        ),
    )
    result = recovery.reconcile_operation(operation)
    assert result.status == OperationStatus.SUCCEEDED
    assert result.execution_updated is True

    timeline = ExecutionTimelineService(event_store, operation_store).build(
        execution.execution_id
    )
    kinds = [item.kind for item in timeline.items]
    assert TimelineItemKind.RETRY in kinds
    assert TimelineItemKind.RECONCILIATION in kinds
    assert TimelineItemKind.EXTERNAL_OPERATION in kinds
    assert state.get_execution(execution.execution_id).status.value == "COMPLETED"
