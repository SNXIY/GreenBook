"""Standalone reconciliation auto-scan: reconcile_due resolves RESULT_UNKNOWN
without manual invocation, and two reconcilers cannot double-finalize.

The worker loop calls reconcile_due periodically; this validates that the scan
itself picks up reconciliation_needed operations and resolves them via a
read-only adapter (never a replay write).
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


def _mk_result_unknown(store: ExternalOperationStore) -> Any:
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="auto-reconcile-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="UPDATE_SCHEDULE",
        resource_id="schedule-1",
        resource_type="SCHEDULE",
        expected_postcondition={"expected": {"status": "SCHEDULED"}},
        claim_owner="worker-A",
    )
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    ledger.mark_result_unknown(claimed)
    return store.get(op.operation_id)


@pytest.mark.asyncio
async def test_reconcile_due_auto_resolves_result_unknown() -> None:
    store = ExternalOperationStore()
    _mk_result_unknown(store)

    class ReadOnlyAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.SUCCEEDED

    worker = ReconciliationWorker(
        OperationLedger(store), adapter=ReadOnlyAdapter()
    )
    outcomes = await worker.reconcile_due()
    assert OperationStatus.SUCCEEDED.value in outcomes
    # every reconciliation_needed operation is now resolved (auto-cleared).
    assert list(store.find_reconciliation_needed()) == []


@pytest.mark.asyncio
async def test_reconcile_due_unknown_read_back_keeps_operation_for_backoff() -> None:
    store = ExternalOperationStore()
    _mk_result_unknown(store)

    class UnknownAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.UNKNOWN

    worker = ReconciliationWorker(OperationLedger(store), adapter=UnknownAdapter())
    outcomes = await worker.reconcile_due()
    assert OperationStatus.RESULT_UNKNOWN.value in outcomes
    # still one operation, still reconciliation-needed (backoff), never replayed.
    pending = list(store.find_reconciliation_needed())
    assert len(pending) == 1
    assert pending[0].status == OperationStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_two_reconcilers_cannot_both_finalize() -> None:
    store = ExternalOperationStore()
    op = _mk_result_unknown(store)

    class ReadOnlyAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.SUCCEEDED

    ledger = OperationLedger(store)
    worker = ReconciliationWorker(ledger, adapter=ReadOnlyAdapter())

    # Reconciler-1 reconciles and finalizes (fenced on the current version).
    assert await worker.reconcile_operation(store.get(op.operation_id)) == OperationStatus.SUCCEEDED.value

    # Reconciler-2 holding the OLD version cannot finalize again (CAS).
    stale = store.get(op.operation_id).model_copy(update={"claim_version": op.claim_version})
    second = ledger.reconcile_unknown(stale, status=OperationStatus.SUCCEEDED)
    assert second is None
    assert store.get(op.operation_id).status == OperationStatus.SUCCEEDED
    assert store.count() == 1


@pytest.mark.asyncio
async def test_reconcile_success_invokes_completion_projection_callback() -> None:
    store = ExternalOperationStore()
    operation = _mk_result_unknown(store)
    projected: list[tuple[str, OperationStatus]] = []

    class ReadOnlyAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.SUCCEEDED

    async def on_reconciled(record: Any, status: OperationStatus) -> None:
        projected.append((record.operation_id, status))

    worker = ReconciliationWorker(
        OperationLedger(store),
        adapter=ReadOnlyAdapter(),
        on_reconciled=on_reconciled,
    )
    assert await worker.reconcile_operation(operation) == OperationStatus.SUCCEEDED.value
    assert projected == [(operation.operation_id, OperationStatus.SUCCEEDED)]


@pytest.mark.asyncio
async def test_projection_crash_keeps_unknown_for_restart_replay() -> None:
    """A crash before the terminal projection must remain recoverable."""

    store = ExternalOperationStore()
    operation = _mk_result_unknown(store)
    projection_calls = 0

    class ReadOnlyAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.SUCCEEDED

    async def crash_once(record: Any, status: OperationStatus) -> None:
        nonlocal projection_calls
        projection_calls += 1
        if projection_calls == 1:
            raise RuntimeError("injected process crash before terminal CAS")

    first = ReconciliationWorker(
        OperationLedger(store),
        adapter=ReadOnlyAdapter(),
        on_reconciled=crash_once,
    )
    with pytest.raises(RuntimeError):
        await first.reconcile_operation(operation)
    assert store.get(operation.operation_id).status == OperationStatus.RESULT_UNKNOWN
    assert list(store.find_reconciliation_needed())

    second = ReconciliationWorker(
        OperationLedger(store),
        adapter=ReadOnlyAdapter(),
        on_reconciled=crash_once,
    )
    assert await second.reconcile_due() == [OperationStatus.SUCCEEDED.value]
    assert store.get(operation.operation_id).status == OperationStatus.SUCCEEDED
    assert projection_calls == 2
