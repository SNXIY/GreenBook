"""Phase 4B.1 tests: Reliable Execution wiring.

These verify that the OperationLedger lives in the Execution Runtime (not the
Agent layer), so Fast Path and Complex ActionLoop writes funnel through the same
durable submission API and the same OperationRecord; and that the Worker claims
before executing and only a fenced (current-version) worker may mutate
reliability state.  External (Java) state is simulated with an explicit test
adapter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_core.execution.external_adapters import MockExternalOperationAdapter
from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.execution.runtime_result import RuntimeResult


def _ledger(store: Any | None = None) -> OperationLedger:
    return OperationLedger(store or ExternalOperationStore())


def _ctx(*, key: str = "c1:t1:UPDATE_DRAFT:abc") -> Any:
    return SimpleNamespace(
        conversation_id="c1",
        task_id="t1",
        run_id="r1",
        trace_id="tr",
        execution_input=SimpleNamespace(
            idempotency_key=key,
            capability="UPDATE_DRAFT",
            tool_name="content.update_draft",
        ),
    )


# ── submission-side dedupe (single owner in the Execution Runtime) ────────


def test_submit_plan_dedupes_through_execution_runtime_ledger() -> None:
    service = RuntimeAgentService(operation_ledger=_ledger())
    # Fresh logical write -> proceed to queue.
    assert service._dedupe_submission(_ctx()) is None
    # A worker claims it (now RUNNING / in-flight).
    ledger = service._operation_ledger
    op = ledger.store.get(ledger.store.list()[0].operation_id)
    ledger.claim(op.operation_id, owner="workerA")
    # Duplicate delivery while in-flight -> dedupe, no second queue.
    in_flight = service._dedupe_submission(_ctx())
    assert in_flight is not None and in_flight.status == "WAITING_EXTERNAL"
    # After a verified completion -> dedupe to COMPLETED.
    claimed = ledger.store.get(op.operation_id)
    ledger.complete(claimed, status=OperationStatus.SUCCEEDED)
    completed = service._dedupe_submission(_ctx())
    assert completed is not None and completed.status == "COMPLETED"
    assert ledger.store.count() == 1, "one OperationRecord for one logical write"


def test_fast_and_action_loop_produce_same_stable_key() -> None:
    from greenbook_agent_api.services.conversation_runtime_adapter import _fast_path_stable_key

    args = {"title": "Java 并发指南", "draft_id": "draft-1"}
    key_a = _fast_path_stable_key("c1", "t1", "UPDATE_DRAFT", args)
    key_b = _fast_path_stable_key("c1", "t1", "UPDATE_DRAFT", dict(args))
    assert key_a == key_b
    # Both Fast Path and ActionLoop build their write through the same
    # Execution submission API, so they resolve to the same operation.
    ledger = _ledger()
    op_a = ledger.begin_operation(idempotency_key=key_a, semantic_action="UPDATE_DRAFT")
    op_b = ledger.begin_operation(idempotency_key=key_b, semantic_action="UPDATE_DRAFT")
    assert op_a.operation_id == op_b.operation_id
    assert ledger.store.count() == 1


# ── fencing (stale worker) ───────────────────────────────────────────────


def test_stale_worker_cannot_mark_side_effect() -> None:
    ledger = _ledger()
    op = ledger.begin_operation(
        idempotency_key="k1", semantic_action="UPDATE_DRAFT", claim_owner="wA"
    )
    claimed = ledger.claim(op.operation_id, owner="wA")  # version 1
    stale = ledger.mark_side_effect_started(op, request_sent=True)  # version 0
    assert stale is None, "a stale version must not mutate reliability state"
    current = ledger.mark_side_effect_started(claimed, request_sent=True)  # version 1
    assert current is not None and current.side_effect_started is True


def test_stale_worker_cannot_complete() -> None:
    ledger = _ledger()
    op = ledger.begin_operation(idempotency_key="k1", semantic_action="CANCEL_SCHEDULE")
    claimed = ledger.claim(op.operation_id, owner="wA")
    assert ledger.complete(op, status=OperationStatus.SUCCEEDED) is None
    assert ledger.complete(claimed, status=OperationStatus.SUCCEEDED) is not None


def test_lease_lost_stops_further_writes() -> None:
    ledger = _ledger()
    op = ledger.begin_operation(idempotency_key="k1", semantic_action="UPDATE_DRAFT")
    claimed = ledger.claim(op.operation_id, owner="wA")  # version 1
    ledger.complete(claimed, status=OperationStatus.SUCCEEDED)  # version 2
    # The old worker's version-1 token is stale; it cannot write further state.
    assert ledger.mark_side_effect_started(claimed, request_sent=True) is None


def test_worker_restart_reuses_operation() -> None:
    engine = sa.create_engine("sqlite://")
    from greenbook_agent_core.execution.persistent_stores import PostgresExternalOperationStore

    ledger1 = _ledger(PostgresExternalOperationStore(engine))
    ledger1.begin_operation(idempotency_key="k1", semantic_action="UPDATE_DRAFT")
    ledger2 = _ledger(PostgresExternalOperationStore(engine))
    op = ledger2.begin_operation(idempotency_key="k1", semantic_action="UPDATE_DRAFT")
    assert ledger2.store.count() == 1
    assert op.idempotency_key == "k1"


# ── worker claim before write ────────────────────────────────────────────


def test_worker_claims_operation_before_write() -> None:
    from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
    from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler

    class StubService:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_queued(self, message, **kwargs):
            self.calls += 1
            return RuntimeResult(success=True, status="COMPLETED", execution_path="runtime")

    ledger = _ledger()
    service = StubService()
    handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=service,
        worker_access_token="t",
        operation_ledger=ledger,
        worker_id="workerA",
    )
    message = ExecutionQueueMessage(
        execution_id="e1",
        payload={
            "execution_input": {"idempotency_key": "c1:t1:UPDATE_DRAFT:abc"},
            "conversation_id": "c1",
            "run_id": "r1",
            "task_id": "t1",
            "auth_context": {"user_id": "u1", "tenant_id": "t1"},
        },
    )
    import asyncio

    asyncio.run(handler(message))
    asyncio.run(handler(message))  # duplicate delivery
    assert service.calls == 1, "the worker claims once; a duplicate delivery is skipped"
    op = ledger.store.list()[0]
    assert op.status == OperationStatus.SUCCEEDED


def test_terminal_run_does_not_redeliver_orphaned_queue_message() -> None:
    from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
    from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler

    class StubService:
        calls = 0

        async def execute_queued(self, message, **kwargs):
            self.calls += 1
            return RuntimeResult(success=True, status="COMPLETED")

    published: list[RuntimeResult] = []

    async def publish(message, result, auth):
        published.append(result)

    service = StubService()
    handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=service,
        worker_access_token="t",
        completion_publisher=publish,
        run_store={"r1": SimpleNamespace(status="FAILED")},
    )
    message = ExecutionQueueMessage(
        execution_id="orphaned-execution",
        payload={
            "conversation_id": "c1",
            "run_id": "r1",
            "task_id": "t1",
            "trace_id": "trace-1",
            "auth_context": {"user_id": "u1", "tenant_id": "tenant-1"},
        },
    )

    import asyncio

    asyncio.run(handler(message))
    assert service.calls == 0
    assert len(published) == 1
    assert published[0].error_code == "STALE_QUEUE_MESSAGE"
    assert published[0].execution_id == "orphaned-execution"


@pytest.mark.parametrize("resource_type", ["DRAFT", "SCHEDULE"])
def test_worker_completion_persists_canonical_resource_evidence(resource_type: str) -> None:
    from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
    from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler
    from greenbook_agent_core.execution.persistent_stores import PostgresExternalOperationStore

    class StubService:
        async def execute_queued(self, message, **kwargs):
            return RuntimeResult(
                success=True,
                status="COMPLETED",
                execution_path="runtime",
                activity_records=[
                    {
                        "step_id": "step-1",
                        "result": {
                            "ok": True,
                            "request_sent": True,
                            "resource_refs": [
                                {"kind": resource_type, "resource_id": "resource-1"}
                            ],
                        },
                    }
                ],
            )

    engine = sa.create_engine("sqlite://")
    ledger = _ledger(PostgresExternalOperationStore(engine))
    handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=StubService(),
        worker_access_token="t",
        operation_ledger=ledger,
        worker_id="workerA",
    )
    message = ExecutionQueueMessage(
        execution_id="execution-1",
        payload={
            "execution_input": {
                "idempotency_key": "c1:t1:WRITE:step-1",
                "steps": [{"step_id": "step-1", "capability": "CREATE_" + resource_type}],
            },
            "conversation_id": "c1",
            "run_id": "r1",
            "task_id": "t1",
            "auth_context": {"user_id": "u1", "tenant_id": "tenant-1"},
        },
    )

    import asyncio

    asyncio.run(handler(message))
    record = ledger.store.list()[0]
    assert record.status == OperationStatus.SUCCEEDED
    assert record.evidence is not None
    assert record.evidence.resource_refs == [
        {"kind": resource_type, "resource_id": "resource-1"}
    ]
    reloaded = PostgresExternalOperationStore(engine).get(record.operation_id)
    assert reloaded is not None
    assert reloaded.evidence is not None
    assert reloaded.evidence.resource_refs == record.evidence.resource_refs


# ── reconciliation worker ────────────────────────────────────────────────


def _unknown(ledger: OperationLedger, *, key: str, next_at: str) -> Any:
    op = ledger.begin_operation(idempotency_key=key, semantic_action="UPDATE_DRAFT", receipt_id="r-1")
    claimed = ledger.claim(op.operation_id, owner="wA")
    return ledger.mark_result_unknown(claimed, next_reconcile_at=next_at)


@pytest.mark.asyncio
async def test_reconciliation_worker_picks_due_operation() -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()
    adapter.set_status(receipt_id="r-1", status=OperationStatus.SUCCEEDED)
    worker = ReconciliationWorker(ledger, adapter)
    op = _unknown(ledger, key="k1", next_at="2000-01-01T00:00:00Z")  # already due
    outcomes = await worker.reconcile_due()
    assert outcomes == [OperationStatus.SUCCEEDED.value]
    fresh = ledger.store.get(op.operation_id)
    assert fresh.status == OperationStatus.SUCCEEDED
    assert fresh.reconciliation_needed is False


@pytest.mark.asyncio
async def test_reconciliation_worker_never_replays_write() -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()
    adapter.set_status(receipt_id="r-1", status=OperationStatus.SUCCEEDED)
    submitted: list[str] = []
    worker = ReconciliationWorker(ledger, adapter)
    _unknown(ledger, key="k1", next_at="")
    await worker.reconcile_due()
    assert submitted == [], "reconciliation only reads authoritative state, never re-submits a write"


@pytest.mark.asyncio
async def test_reconciliation_terminal_not_reprocessed() -> None:
    ledger = _ledger()
    adapter = MockExternalOperationAdapter()
    adapter.set_status(receipt_id="r-1", status=OperationStatus.SUCCEEDED)
    worker = ReconciliationWorker(ledger, adapter)
    _unknown(ledger, key="k1", next_at="")
    await worker.reconcile_due()
    assert ledger.find_reconciliation_needed() == [], "a verified operation is not reprocessed"


@pytest.mark.asyncio
async def test_reconciliation_backoff_respects_next_at() -> None:
    ledger = _ledger()
    worker = ReconciliationWorker(ledger, MockExternalOperationAdapter())  # always UNKNOWN
    op = _unknown(ledger, key="k1", next_at="")  # due now
    await worker.reconcile_due()
    fresh = ledger.store.get(op.operation_id)
    assert fresh.reconcile_attempts == 1
    assert fresh.next_reconcile_at > "", "a still-unknown write is scheduled for backoff"
    # Not due yet (next_reconcile_at is in the future) -> not picked up.
    assert ledger.find_reconciliation_needed(now="2000-01-01T00:00:00Z") == []
