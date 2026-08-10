"""Phase 13-B Runtime metrics tests."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
)
from greenbook_assistant_core.execution.reconciliation import ReconciliationService
from greenbook_assistant_core.observability.context import TraceContext
from greenbook_assistant_core.observability.metrics import MemoryMetricsCollector

from apps.assistant_api.greenbook_assistant_api.models.runtime_context import (
    RuntimeContext,
    TaskContext,
)
from apps.assistant_api.greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.task.models import TaskIntent


def test_memory_metrics_collector_aggregates_runtime_dimensions() -> None:
    metrics = MemoryMetricsCollector()
    context = TraceContext(trace_id="trace-metrics")

    metrics.record_execution(status="COMPLETED", duration_ms=10.0, context=context)
    metrics.record_execution(status="FAILED", duration_ms=20.0, context=context)
    metrics.record_step(status="COMPLETED", latency_ms=5.0, context=context)
    metrics.record_step(status="FAILED", latency_ms=7.0, context=context)
    metrics.record_tool(status="COMPLETED", latency_ms=3.0, context=context)
    metrics.record_tool(status="TIMEOUT", latency_ms=9.0, context=context)
    metrics.record_retry(context=context)
    metrics.record_retry(success=True, context=context)
    metrics.record_reconciliation(status="UNKNOWN", context=context)
    metrics.record_reconciliation(status="SUCCEEDED", context=context)

    snapshot = metrics.snapshot()

    assert snapshot.execution_total == 2
    assert snapshot.execution_success == 1
    assert snapshot.execution_failed == 1
    assert snapshot.step_failure == 1
    assert snapshot.tool_invocation_count == 2
    assert snapshot.tool_error_count == 1
    assert snapshot.retry_count == 1
    assert snapshot.retry_success == 1
    assert snapshot.reconciliation_unknown == 1
    assert snapshot.reconciliation_resolved == 1
    assert snapshot.tool_error_rate == 0.5
    assert snapshot.as_dict()["execution"]["success_rate"] == 0.5


@pytest.mark.asyncio
async def test_runtime_service_records_execution_step_and_tool_metrics() -> None:
    metrics = MemoryMetricsCollector()

    class MCP:
        async def execute_tool(self, _name: str, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "code": "",
                "data": {
                    "draft_id": "metrics-draft",
                    "title": "Metrics",
                    "content": "Runtime metrics",
                },
            }

    intent = TaskIntent(
        relation="NEW_TASK",
        goal="Create a metrics post",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    context = RuntimeContext(
        conversation_id="conversation-metrics",
        run_id="run-metrics",
        trace_id="trace-metrics",
        task_id="task-metrics",
        user_id="user-metrics",
        tenant_id="tenant-metrics",
        user_message="Create a metrics post",
        task_intent=intent,
        task_context=TaskContext(
            task_id="task-metrics",
            goal="Create a metrics post",
            task_intent=intent,
        ),
        mcp=MCP(),
    )

    result = await RuntimeAgentService(metrics_collector=metrics).execute(context)
    snapshot = metrics.snapshot()

    assert result.status == "COMPLETED"
    assert snapshot.execution_total == 1
    assert snapshot.execution_success == 1
    assert snapshot.step_total >= 1
    assert snapshot.tool_invocation_count == 1
    assert snapshot.tool_error_count == 0
    assert snapshot.execution_duration_ms >= 0.0


def test_reconciliation_records_unknown_and_resolved_metrics() -> None:
    metrics = MemoryMetricsCollector()
    store = ExternalOperationStore()
    operation = ExternalOperationRecord(
        operation_id="operation-metrics",
        execution_id="execution-metrics",
        step_id="step-metrics",
        external_operation_id="external-metrics",
    )
    statuses = iter(["UNKNOWN", "SUCCEEDED"])
    service = ReconciliationService(
        store=store,
        query=lambda **_kwargs: next(statuses),
        metrics_collector=metrics,
    )

    service.reconcile_result(operation)
    service.reconcile_result(operation)

    snapshot = metrics.snapshot()
    assert snapshot.reconciliation_unknown == 1
    assert snapshot.reconciliation_resolved == 1
