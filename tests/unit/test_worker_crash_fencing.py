"""Worker crash durability: fencing + CAS prevent stale terminal writes.

A worker claims an operation (claim_version), crashes mid-write.  A stale
worker must not be able to write a terminal state (fenced on claim_version),
and only the claiming worker (or a legitimately re-claimed owner) may complete.
No duplicate side effect is possible.
"""

from __future__ import annotations

import pytest

from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)


def _store_and_ledger():
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = ledger.begin_operation(
        idempotency_key="crash-write-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="CREATE_SCHEDULE",
        resource_id="draft-1",
        resource_type="SCHEDULE",
        claim_owner="worker-A",
    )
    return store, ledger, op


def test_stale_worker_cannot_claim_or_complete() -> None:
    store, ledger, op = _store_and_ledger()
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    assert claimed is not None
    assert claimed.claim_version >= 1

    # A second worker cannot claim an already-claimed operation.
    assert ledger.claim(op.operation_id, owner="worker-B") is None

    # A stale reference (stale claim_version) cannot write a terminal state:
    # CAS rejects it, so a crashed/duplicated worker cannot fake completion.
    stale = claimed.model_copy(update={"claim_version": 0})
    completed = ledger.complete(stale, status=OperationStatus.SUCCEEDED)
    assert completed is None or completed.status != OperationStatus.SUCCEEDED


def test_claiming_worker_can_complete_but_no_second_side_effect() -> None:
    store, ledger, op = _store_and_ledger()
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    completed = ledger.complete(claimed, status=OperationStatus.SUCCEEDED)
    assert completed is not None
    assert completed.status == OperationStatus.SUCCEEDED

    # Exactly one operation exists; completion is not a new logical operation.
    assert len(list(store.find_reconciliation_needed())) == 0
    # A replayed begin_operation returns the SAME operation (idempotent), so a
    # duplicate delivery cannot start a second side effect.
    again = ledger.begin_operation(
        idempotency_key="crash-write-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="CREATE_SCHEDULE",
        resource_id="draft-1",
        resource_type="SCHEDULE",
        claim_owner="worker-A",
    )
    assert again.operation_id == completed.operation_id
    assert again.status == OperationStatus.SUCCEEDED


def test_result_unknown_is_never_claimed_by_new_worker_as_new_write() -> None:
    store, ledger, op = _store_and_ledger()
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    ledger.mark_result_unknown(claimed)
    op_after = store.get(claimed.operation_id)
    assert op_after.status == OperationStatus.RESULT_UNKNOWN

    # A RESULT_UNKNOWN operation must be reconciled, not re-claimed as a new
    # write; claiming it again is refused (already claimed) and no second
    # side-effect is started.
    assert ledger.claim(op_after.operation_id, owner="worker-B") is None
