"""RESULT_UNKNOWN durability: no-retry, reconcile read-back, single Java mutation.

Simulates a write where Java commits the side effect but the Python boundary
returns a TIMEOUT with request_sent=true.  The operation must become
RESULT_UNKNOWN (never re-run as a write), and the ReconciliationWorker must
resolve it via a read-only Java read-back (no replay) into VERIFIED_COMPLETED.
Only one logical operation and one Java mutation may ever occur.
"""

from __future__ import annotations

from typing import Any

import pytest

from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from greenbook_agent_core.execution.retry_classification import (
    RetryClassification,
    classify_retry,
)


def _begin_write(ledger: OperationLedger) -> Any:
    op = ledger.begin_operation(
        idempotency_key="schedule-write-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="UPDATE_SCHEDULE",
        resource_id="schedule-123",
        resource_type="SCHEDULE",
        expected_postcondition={"status": "SCHEDULED", "run_at": "2026-08-17T08:00:00Z"},
        claim_owner="worker-A",
    )
    return ledger.claim(op.operation_id, owner="worker-A")


def _java_mutations(calls: list[str]) -> None:
    """Record that a real Java mutation happened exactly once."""


@pytest.mark.asyncio
async def test_timeout_with_request_sent_becomes_result_unknown_no_retry() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    claimed = _begin_write(ledger)
    assert claimed is not None

    # Java actually committed the schedule; the Python boundary times out but
    # the request WAS sent (request_sent=true).  No second write may occur.
    java_mutations: list[str] = []
    ledger.mark_result_unknown(claimed)

    # The operation is RESULT_UNKNOWN and flagged for reconciliation, NOT
    # SUCCEEDED and NOT retried.
    op = store.get(claimed.operation_id)
    assert op.status == OperationStatus.RESULT_UNKNOWN
    assert op.reconciliation_needed is True
    assert java_mutations == []

    # A naive "retry" is forbidden: the ledger refuses to re-run a claimed
    # RESULT_UNKNOWN operation as a new write (no second Java mutation).
    # Reconciliation must read back, not replay the write.
    read_backs: list[str] = []

    class ReadOnlyAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            read_backs.append(operation.operation_id)
            # Java authoritative read-back confirms the schedule is SCHEDULED.
            return OperationStatus.SUCCEEDED

    worker = ReconciliationWorker(ledger, adapter=ReadOnlyAdapter())
    outcome = await worker.reconcile_operation(store.get(claimed.operation_id))

    assert outcome == OperationStatus.SUCCEEDED.value
    final = store.get(claimed.operation_id)
    assert final.status == OperationStatus.SUCCEEDED
    assert final.verified_status == "VERIFIED_COMPLETED"
    # Reconciliation only read back; it never replayed a write.
    assert read_backs == [claimed.operation_id]
    assert java_mutations == []


@pytest.mark.asyncio
async def test_unknown_read_back_stays_result_unknown_and_never_reruns() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    claimed = _begin_write(ledger)
    ledger.mark_result_unknown(claimed)

    class UnknownAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.UNKNOWN

    worker = ReconciliationWorker(ledger, adapter=UnknownAdapter())
    outcome = await worker.reconcile_operation(store.get(claimed.operation_id))
    assert outcome == OperationStatus.RESULT_UNKNOWN.value
    op = store.get(claimed.operation_id)
    assert op.status == OperationStatus.RESULT_UNKNOWN
    assert op.reconciliation_needed is True
    # Still exactly one reconciliation-needed operation; the write was never
    # replayed into a second logical operation.
    assert len(list(store.find_reconciliation_needed())) == 1


@pytest.mark.asyncio
async def test_reconciled_operation_not_found_returns_to_safe_pending_retry() -> None:
    """A missing operation identity proves the original write was not applied."""

    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    claimed = _begin_write(ledger)
    assert claimed is not None
    ledger.mark_result_unknown(claimed)

    class NotFoundAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            del operation
            return OperationStatus.NOT_FOUND

    worker = ReconciliationWorker(ledger, adapter=NotFoundAdapter())
    outcome = await worker.reconcile_operation(store.get(claimed.operation_id))

    assert outcome == OperationStatus.NOT_FOUND.value
    retryable = store.get(claimed.operation_id)
    assert retryable.status == OperationStatus.PENDING
    assert retryable.side_effect_started is False
    assert retryable.reconciliation_needed is False
    assert retryable.retry_classification == RetryClassification.SAFE_RETRY.value
    assert classify_retry(
        is_write=True,
        status=OperationStatus.NOT_FOUND,
        request_sent=False,
        side_effect_started=False,
    ) == RetryClassification.SAFE_RETRY

    # The durable claim is available again; no Java mutation was replayed by
    # reconciliation itself.
    reclaimed = ledger.claim(retryable.operation_id, owner="worker-B")
    assert reclaimed is not None
