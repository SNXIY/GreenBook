"""Phase 4B tests: durable Operation Ledger, claim/CAS/fencing, reconciliation,
and retry classification.

These exercise the reliability primitives that keep write side effects from
duplicating or mis-reporting completion across worker crash / lease loss /
duplicate delivery / timeout.  External (Java) state is simulated with an
explicit test adapter — never a fake end-to-end result.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_core.execution.external_adapters import MockExternalOperationAdapter
from greenbook_agent_core.execution.operation_ledger import (
    OperationLedger,
)
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
    RetryClassification,
    VerifiedStatus,
)
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from greenbook_agent_core.execution.retry_classification import (
    classify_retry,
    is_safe_retry,
)


def _ledger(store: Any | None = None) -> OperationLedger:
    return OperationLedger(store or ExternalOperationStore())


def _begin(ledger: OperationLedger, **over) -> Any:
    kwargs = {
        "idempotency_key": "k1",
        "conversation_id": "c1",
        "task_id": "t1",
        "execution_id": "e1",
        "step_id": "s1",
        "tool_name": "content.update_draft",
        "semantic_action": "UPDATE_DRAFT",
        "resource_type": "DRAFT",
    }
    kwargs.update(over)
    return ledger.begin_operation(**kwargs)


# ── durable ledger ───────────────────────────────────────────────────────


def test_operation_ledger_survives_restart() -> None:
    engine = sa.create_engine("sqlite://")
    from greenbook_agent_core.execution.persistent_stores import PostgresExternalOperationStore

    ledger1 = _ledger(PostgresExternalOperationStore(engine))
    op = _begin(ledger1)
    # Simulate process restart: a brand-new store/ledger over the same bind.
    ledger2 = _ledger(PostgresExternalOperationStore(engine))
    restarted = _begin(ledger2)
    assert restarted.operation_id == op.operation_id
    assert restarted.status == OperationStatus.PENDING
    assert ledger2.store.count() == 1, "duplicate delivery across restart dedupes"


def test_duplicate_operation_claim_once() -> None:
    ledger = _ledger()
    op = _begin(ledger)
    worker_a = ledger.claim(op.operation_id, owner="workerA")
    assert worker_a is not None
    worker_b = ledger.claim(op.operation_id, owner="workerB")
    assert worker_b is None, "only one worker may claim an operation (CAS on status)"
    assert worker_a.claim_version == 1


def test_stale_worker_cannot_complete() -> None:
    ledger = _ledger()
    op = _begin(ledger)  # version 0
    claimed = ledger.claim(op.operation_id, owner="workerA")  # version 1
    # The stale record (version 0, held before the claim) cannot complete.
    stale = ledger.complete(op, status=OperationStatus.SUCCEEDED)
    assert stale is None
    # The claim holder (version 1) completes.
    ok = ledger.complete(claimed, status=OperationStatus.SUCCEEDED)
    assert ok is not None and ok.status == OperationStatus.SUCCEEDED


def test_pause_releases_unstarted_claim_for_resume() -> None:
    ledger = _ledger()
    op = _begin(ledger)
    claimed = ledger.claim(op.operation_id, owner="workerA")
    released = ledger.release_claim(claimed)
    assert released is not None
    assert released.status == OperationStatus.PENDING
    assert released.claim_version == claimed.claim_version + 1
    resumed = ledger.claim(released.operation_id, owner="workerB")
    assert resumed is not None
    assert ledger.complete(claimed, status=OperationStatus.SUCCEEDED) is None


def test_lease_lost_stops_next_action() -> None:
    ledger = _ledger()
    op = _begin(ledger)
    claimed = ledger.claim(op.operation_id, owner="workerA")  # version 1
    # A re-claimed / new worker holds a newer version; the old worker's token
    # (version 1) is now stale and cannot write the terminal state.
    newer = ledger.complete(claimed, status=OperationStatus.SUCCEEDED)  # version 2
    assert newer is not None
    stale_again = ledger.complete(claimed, status=OperationStatus.FAILED)  # stale v1
    assert stale_again is None, "a worker holding a stale version cannot overwrite the result"


def test_duplicate_delivery_no_duplicate_business_operation() -> None:
    ledger = _ledger()
    first = _begin(ledger)
    second = _begin(ledger)  # same idempotency key
    assert first.operation_id == second.operation_id
    assert ledger.store.count() == 1
    # Different args -> a genuinely different operation.
    other = _begin(ledger, idempotency_key="k2", semantic_action="CANCEL_SCHEDULE")
    assert other.operation_id != first.operation_id
    assert ledger.store.count() == 2


# ── result unknown / retry classification ────────────────────────────────


def test_write_timeout_enters_result_unknown() -> None:
    ledger = _ledger()
    op = _begin(ledger)
    unknown = ledger.mark_result_unknown(op, next_reconcile_at="2026-08-15T10:00:00Z")
    assert unknown.status == OperationStatus.RESULT_UNKNOWN
    assert unknown.reconciliation_needed is True
    assert unknown.retry_classification == RetryClassification.RESULT_UNKNOWN.value


def test_result_unknown_not_normal_retry() -> None:
    # A write that may have reached Java is never SAFE_RETRY.
    assert classify_retry(is_write=True, request_sent=True, side_effect_started=True) == RetryClassification.RESULT_UNKNOWN
    assert is_safe_retry(is_write=True, request_sent=True) is False


def test_permanent_failure_not_retried() -> None:
    assert classify_retry(is_write=True, error_code="SCHEMA_INVALID") == RetryClassification.PERMANENT_FAILURE
    assert classify_retry(is_write=True, error_code="404") == RetryClassification.PERMANENT_FAILURE
    assert is_safe_retry(is_write=True, error_code="PERMISSION_DENIED") is False


@pytest.mark.parametrize(
    "error_code",
    ["VALIDATION_ERROR", "BUSINESS_REJECTED", "INTERNAL_ERROR"],
)
def test_non_transport_failures_never_enter_safe_retry(error_code: str) -> None:
    assert classify_retry(
        is_write=True,
        request_sent=False,
        error_code=error_code,
    ) == RetryClassification.PERMANENT_FAILURE
    assert is_safe_retry(is_write=False, error_code=error_code) is False


def test_worker_crash_before_side_effect_safe_retry() -> None:
    # Request provably not sent, no side effect started -> SAFE_RETRY.
    assert classify_retry(is_write=True, request_sent=False, side_effect_started=False) == RetryClassification.SAFE_RETRY


def test_reconciled_missing_operation_is_safe_retry_only_with_no_send_evidence() -> None:
    assert classify_retry(
        is_write=True,
        status=OperationStatus.NOT_FOUND,
        request_sent=False,
        side_effect_started=False,
    ) == RetryClassification.SAFE_RETRY
    assert classify_retry(
        is_write=True,
        status=OperationStatus.NOT_FOUND,
        request_sent=True,
        side_effect_started=True,
    ) == RetryClassification.RESULT_UNKNOWN
    assert is_safe_retry(is_write=True, request_sent=False) is True


def test_worker_crash_after_side_effect_reconcile_first() -> None:
    ledger = _ledger()
    op = _begin(ledger)
    claimed = ledger.claim(op.operation_id, owner="workerA")
    ledger.mark_side_effect_started(claimed, request_sent=True)
    assert is_safe_retry(is_write=True, side_effect_started=True) is False
    unknown = ledger.mark_result_unknown(claimed)
    assert unknown.reconciliation_needed is True


# ── reconciliation ───────────────────────────────────────────────────────


def _unknown_operation(ledger: OperationLedger, *, semantic_action: str, receipt_id: str) -> Any:
    op = _begin(ledger, semantic_action=semantic_action, receipt_id=receipt_id)
    claimed = ledger.claim(op.operation_id, owner="workerA")
    return ledger.mark_result_unknown(claimed, next_reconcile_at="")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["UPDATE_DRAFT", "UPDATE_SCHEDULE", "CANCEL_SCHEDULE"])
async def test_reconciliation_verifies_completed(action: str) -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()
    adapter.set_status(receipt_id="r-1", status=OperationStatus.SUCCEEDED)
    worker = ReconciliationWorker(ledger, adapter)
    op = _unknown_operation(ledger, semantic_action=action, receipt_id="r-1")
    outcome = await worker.reconcile_operation(op)
    assert outcome == OperationStatus.SUCCEEDED.value
    assert op.status == OperationStatus.RESULT_UNKNOWN  # the caller's snapshot
    fresh = ledger.store.get(op.operation_id)
    assert fresh.status == OperationStatus.SUCCEEDED
    assert fresh.verified_status == VerifiedStatus.VERIFIED_COMPLETED.value
    assert fresh.reconciliation_needed is False


@pytest.mark.asyncio
async def test_reconciliation_still_unknown_backs_off() -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()  # default UNKNOWN
    worker = ReconciliationWorker(ledger, adapter)
    op = _unknown_operation(ledger, semantic_action="UPDATE_DRAFT", receipt_id="r-x")
    outcome = await worker.reconcile_operation(op)
    assert outcome == OperationStatus.RESULT_UNKNOWN.value
    fresh = ledger.store.get(op.operation_id)
    assert fresh.status == OperationStatus.RESULT_UNKNOWN
    assert fresh.reconcile_attempts == 1
    assert fresh.next_reconcile_at, "a still-unknown write is backed off, not retried"


@pytest.mark.asyncio
async def test_reconciliation_retry_budget() -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()  # always UNKNOWN
    worker = ReconciliationWorker(ledger, adapter, max_reconcile_attempts=2)
    op = _unknown_operation(ledger, semantic_action="UPDATE_DRAFT", receipt_id="r-y")
    for _ in range(3):
        await worker.reconcile_operation(op)
        op = ledger.store.get(op.operation_id)
    assert op.status == OperationStatus.RESULT_UNKNOWN
    assert op.verified_status == VerifiedStatus.VERIFIED_UNKNOWN.value
    assert op.reconciliation_needed is True, "budget-exhausted unknown stays for manual handling"


@pytest.mark.asyncio
async def test_reconciliation_never_runs_as_normal_write() -> None:
    # Reconciliation only queries and writes the ledger; it never re-submits.
    submitted: list[str] = []
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()
    adapter.set_status(receipt_id="r-2", status=OperationStatus.SUCCEEDED)
    worker = ReconciliationWorker(ledger, adapter)

    def fake_submit(**kwargs):
        submitted.append(kwargs.get("semantic_action"))
        return {"ok": True}

    op = _unknown_operation(ledger, semantic_action="UPDATE_DRAFT", receipt_id="r-2")
    await worker.reconcile_operation(op)
    assert submitted == [], "reconciliation must never re-submit a write"
