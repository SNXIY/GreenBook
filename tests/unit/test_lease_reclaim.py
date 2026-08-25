"""Worker crash + lease expiry + reclaim (Case W1 / W2).

W1: worker-A claims (RUNNING), crashes before request_sent; after lease expiry a
new worker-B must reclaim the SAME operation (claim_version advances, fencing
token advances), and stale worker-A must be rejected.
W2: after request_sent=true the operation must converge to RESULT_UNKNOWN and
never be replayed as a blind retry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)


def _begin(store):
    ledger = OperationLedger(store)
    return ledger.begin_operation(
        idempotency_key="op-reclaim-1",
        conversation_id="conv-1",
        task_id="task-1",
        execution_id="exec-1",
        semantic_action="UPDATE_DRAFT",
        resource_id="draft-1",
        resource_type="DRAFT",
        expected_postcondition={"expected": {"content": "x"}},
        claim_owner="",
    )


def test_w1_expired_lease_can_be_reclaimed_by_new_worker() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = _begin(store)
    claimed_a = ledger.claim(op.operation_id, owner="worker-A")
    assert claimed_a is not None
    assert claimed_a.claim_owner == "worker-A"
    v1 = claimed_a.claim_version
    assert claimed_a.status == OperationStatus.RUNNING

    # worker-A crashes before request_sent.  Lease expires.
    # Worker-B must reclaim the SAME operation.
    reclaimed = ledger.claim_after_lease_expiry(
        op.operation_id, owner="worker-B", now=datetime.now(UTC) + timedelta(seconds=100)
    )
    assert reclaimed is not None
    assert reclaimed.operation_id == op.operation_id
    assert reclaimed.claim_owner == "worker-B"
    assert reclaimed.claim_version > v1
    assert reclaimed.fencing_token != claimed_a.fencing_token

    # stale worker-A with the OLD claim_version cannot complete.
    stale = store.get(op.operation_id).model_copy(update={"claim_version": v1})
    assert ledger.complete(stale, status=OperationStatus.SUCCEEDED) is None

    # worker-B (current version) can complete.
    done = ledger.complete(reclaimed, status=OperationStatus.SUCCEEDED)
    assert done is not None and done.status == OperationStatus.SUCCEEDED
    assert store.count() == 1


def test_w1_crash_before_request_sent_can_safely_continue() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = _begin(store)
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    # never marked side_effect_started => safe to reclaim and re-run once.
    assert claimed.side_effect_started is False
    reclaimed = ledger.claim_after_lease_expiry(
        op.operation_id, owner="worker-B", now=datetime.now(UTC) + timedelta(seconds=100)
    )
    assert reclaimed is not None
    assert reclaimed.side_effect_started is False
    assert reclaimed.claim_owner == "worker-B"


def test_w2_request_sent_never_replays_write() -> None:
    store = ExternalOperationStore()
    ledger = OperationLedger(store)
    op = _begin(store)
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    # Java may have executed; Python lost the response (request_sent=true).
    ledger.mark_side_effect_started(claimed, request_sent=True)
    after = store.get(op.operation_id)
    assert after.side_effect_started is True
    assert after.retry_classification == "RESULT_UNKNOWN"
    # The crash-recovery marks the outcome unknown (reconciliation required).
    ledger.mark_result_unknown(after)
    unknown = store.get(op.operation_id)
    assert unknown.status == OperationStatus.RESULT_UNKNOWN
    assert unknown.reconciliation_needed is True

    # Lease expiry must NOT allow a blind re-claim+replay: the op must stay
    # RESULT_UNKNOWN, never be re-run as a write.
    reclaimed = ledger.claim_after_lease_expiry(
        op.operation_id, owner="worker-B", now=datetime.now(UTC) + timedelta(seconds=100)
    )
    assert reclaimed is None
    final = store.get(op.operation_id)
    assert final.status == OperationStatus.RESULT_UNKNOWN
    assert store.count() == 1
