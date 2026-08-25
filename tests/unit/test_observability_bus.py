"""Observability bus focused tests: trace propagation, metric boundedness, no
sensitive fields, no double-counting of logical operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from greenbook_agent_core.observability.bus import RuntimeObservability
from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)


def test_trace_id_propagates_to_operation() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="obs-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="CREATE_DRAFT",
        trace_id="trace-abc",
    )
    assert op.trace_id == "trace-abc"


def test_worker_reclaim_keeps_same_trace_and_operation() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="obs-2",
        conversation_id="conv-1",
        execution_id="exec-1",
        semantic_action="UPDATE_DRAFT",
        resource_id="draft-1",
        resource_type="DRAFT",
        trace_id="trace-reclaim",
    )
    a = ledger.claim(op.operation_id, owner="worker-A")
    b = ledger.claim_after_lease_expiry(
        op.operation_id, owner="worker-B", now=datetime.now(UTC) + timedelta(seconds=100)
    )
    assert b is not None
    assert b.operation_id == op.operation_id
    assert b.trace_id == "trace-reclaim"
    assert b.claim_owner == "worker-B"
    assert b.claim_version > a.claim_version


def test_metrics_labels_are_low_cardinality() -> None:
    ob = RuntimeObservability()
    # These metrics must NOT accept user/task/conversation/operation ids.
    assert ob.turn_total().label_names == ("outcome",)
    assert ob.operation().label_names == ("semantic_action", "outcome")
    assert ob.reconciliation().label_names == ("outcome",)
    assert ob.operation_retry().label_names == ("retry_classification",)
    assert ob.fastpath().label_names == ("route",)
    assert ob.target_resolution().label_names == ("status",)


def test_logical_operation_not_double_counted() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="obs-3", conversation_id="c", execution_id="e",
        semantic_action="CREATE_SCHEDULE", trace_id="t3",
    )
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    ledger.complete(claimed, status=OperationStatus.SUCCEEDED)
    # a replayed begin_operation returns the SAME logical operation (dedupe).
    again = ledger.begin_operation(
        idempotency_key="obs-3", conversation_id="c", execution_id="e",
        semantic_action="CREATE_SCHEDULE", trace_id="t3",
    )
    assert again.operation_id == op.operation_id
    assert store.count() == 1


def test_reconciliation_keeps_trace_and_metric_outcome() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="obs-4", conversation_id="c", execution_id="e",
        semantic_action="UPDATE_SCHEDULE", resource_id="s1", resource_type="SCHEDULE",
        expected_postcondition={"expected": {"status": "SCHEDULED"}}, trace_id="trace-rec",
    )
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    ledger.mark_side_effect_started(claimed, request_sent=True)
    ledger.mark_result_unknown(claimed)
    after = store.get(op.operation_id)
    assert after.trace_id == "trace-rec"

    ob = RuntimeObservability()
    ob.result_unknown().inc()
    ob.reconciliation().inc(outcome="VERIFIED_COMPLETED")
    rendered = ob.render_metrics()
    assert "agent_result_unknown_total 1" in rendered
    assert 'agent_reconciliation_total{outcome="VERIFIED_COMPLETED"} 1' in rendered


def test_render_metrics_excludes_sensitive_values() -> None:
    ob = RuntimeObservability()
    ob.turn_total().inc(outcome="COMPLETED")
    ob.operation().inc(semantic_action="CREATE_DRAFT", outcome="SUCCEEDED")
    rendered = ob.render_metrics()
    assert "token" not in rendered.lower()
    assert "password" not in rendered.lower()


def test_trace_store_records_and_returns_timeline() -> None:
    ob = RuntimeObservability()
    ob.record_trace(
        "turn_received", trace_id="t-x", conversation_id="c1", status="STARTED"
    )
    ob.record_trace(
        "action_decided", trace_id="t-x", semantic_action="CREATE_DRAFT"
    )
    ob.record_trace(
        "operation_submitted", trace_id="t-x", operation_id="op1"
    )
    spans = ob.traces.get("t-x").spans()
    assert [s.stage for s in spans] == [
        "turn_received", "action_decided", "operation_submitted"
    ]
    assert spans[0].conversation_id == "c1"
    assert spans[2].operation_id == "op1"
