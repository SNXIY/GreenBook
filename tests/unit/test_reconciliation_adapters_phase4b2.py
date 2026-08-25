"""Phase 4B.2 tests: Java reconciliation adapter + operation granularity.

The Java reconciliation adapter verifies RESULT_UNKNOWN operations against
Java authoritative state deterministically (no LLM, no write replay), using the
persisted expected postcondition.  Java responses are simulated with an explicit
stub client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.reconciliation_adapters import JavaReconciliationAdapter
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService


class StubJava:
    """Minimal JavaClient-shaped read stub for deterministic verification."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes: list[str] = []
        self._draft = None
        self._schedule = None
        self._post = None

    def set_draft(self, data: dict[str, Any] | None, *, ok: bool = True, code: str = "OK") -> None:
        self._draft = (ok, code, SimpleNamespace(**data) if data else None)

    def set_schedule(self, data: dict[str, Any] | None, *, ok: bool = True, code: str = "OK") -> None:
        self._schedule = (ok, code, SimpleNamespace(**data) if data else None)

    def set_post(self, data: dict[str, Any] | None, *, ok: bool = True, code: str = "OK") -> None:
        self._post = (ok, code, SimpleNamespace(**data) if data else None)

    async def get_draft(self, draft_id: str, *, bearer_token: str, trace_id: str | None = None, **kw):
        self.reads.append("get_draft")
        if self._draft is None:
            return SimpleNamespace(ok=False, code="NOT_FOUND", data=None)
        ok, code, data = self._draft
        return SimpleNamespace(ok=ok, code=code, data=data)

    async def get_schedule(self, schedule_id: str, *, bearer_token: str, trace_id: str | None = None, **kw):
        self.reads.append("get_schedule")
        if self._schedule is None:
            return SimpleNamespace(ok=False, code="NOT_FOUND", data=None)
        ok, code, data = self._schedule
        return SimpleNamespace(ok=ok, code=code, data=data)

    async def get_post(self, post_id: str, *, bearer_token: str, trace_id: str | None = None, **kw):
        self.reads.append("get_post")
        if self._post is None:
            return SimpleNamespace(ok=False, code="NOT_FOUND", data=None)
        ok, code, data = self._post
        return SimpleNamespace(ok=ok, code=code, data=data)

    # Writes must never be invoked during reconciliation.
    async def update_draft(self, *a, **k):
        self.writes.append("update_draft")
        return SimpleNamespace(ok=True, data=None)

    async def cancel_schedule(self, *a, **k):
        self.writes.append("cancel_schedule")
        return SimpleNamespace(ok=True, data=None)

    async def publish_now(self, *a, **k):
        self.writes.append("publish_now")
        return SimpleNamespace(ok=True, data=None)


def _ledger(store: Any | None = None) -> OperationLedger:
    return OperationLedger(store or ExternalOperationStore())


def _op(ledger: OperationLedger, *, action: str, resource_id: str, expected: dict[str, Any]) -> Any:
    return ledger.begin_operation(
        idempotency_key=f"k:{action}:{resource_id}",
        semantic_action=action,
        resource_id=resource_id,
        resource_type="DRAFT" if "DRAFT" in action else "SCHEDULE",
        expected_postcondition={"arguments": {"draft_id": resource_id}, "expected": expected},
    )


def _adapter(java: StubJava) -> JavaReconciliationAdapter:
    return JavaReconciliationAdapter(java, token_provider=lambda: "token")


@pytest.mark.asyncio
async def test_reconciliation_adapter_update_draft() -> None:
    java = StubJava()
    java.set_draft({"title": "新标题", "content": "正文"})
    ledger = _ledger()
    op = _op(ledger, action="UPDATE_DRAFT", resource_id="d1", expected={"title": "新标题"})
    assert await _adapter(java).reconcile(op) == OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconciliation_adapter_update_draft_failed_when_mismatch() -> None:
    java = StubJava()
    java.set_draft({"title": "旧标题"})
    ledger = _ledger()
    op = _op(ledger, action="UPDATE_DRAFT", resource_id="d1", expected={"title": "新标题"})
    assert await _adapter(java).reconcile(op) == OperationStatus.FAILED


@pytest.mark.asyncio
async def test_reconciliation_adapter_update_schedule() -> None:
    java = StubJava()
    java.set_schedule({"run_at": "2026-08-16T09:00:00+00:00", "status": "SCHEDULED"})
    ledger = _ledger()
    op = _op(ledger, action="UPDATE_SCHEDULE", resource_id="s1", expected={"run_at": "2026-08-16T09:00:00+00:00"})
    assert await _adapter(java).reconcile(op) == OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconciliation_adapter_cancel_schedule() -> None:
    java = StubJava()
    java.set_schedule({"status": "CANCELLED"})
    ledger = _ledger()
    op = _op(ledger, action="CANCEL_SCHEDULE", resource_id="s1", expected={"status": "CANCELLED"})
    assert await _adapter(java).reconcile(op) == OperationStatus.SUCCEEDED
    java.set_schedule({"status": "SCHEDULED"})
    assert await _adapter(java).reconcile(op) == OperationStatus.FAILED


@pytest.mark.asyncio
async def test_reconciliation_adapter_delete_draft() -> None:
    java = StubJava()
    java.set_draft(None, ok=False, code="NOT_FOUND")
    ledger = _ledger()
    op = _op(ledger, action="DELETE_DRAFT", resource_id="d1", expected={"deleted": True})
    assert await _adapter(java).reconcile(op) == OperationStatus.SUCCEEDED
    java.set_draft({"title": "还在"})
    assert await _adapter(java).reconcile(op) == OperationStatus.FAILED


@pytest.mark.asyncio
async def test_reconciliation_adapter_publish() -> None:
    java = StubJava()
    java.set_post({"post_id": "p1", "status": "published"})
    ledger = _ledger()
    op = _op(ledger, action="PUBLISH_NOW", resource_id="d1", expected={"post_id": "p1"})
    assert await _adapter(java).reconcile(op) == OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconciliation_never_invokes_write() -> None:
    java = StubJava()
    java.set_draft({"title": "x"})
    ledger = _ledger()
    op = _op(ledger, action="UPDATE_DRAFT", resource_id="d1", expected={"title": "x"})
    worker = ReconciliationWorker(ledger, _adapter(java))
    await worker.reconcile_operation(op)
    assert java.writes == [], "reconciliation must never invoke a write"
    assert java.reads, "reconciliation performs authoritative reads only"


@pytest.mark.asyncio
async def test_reconciliation_uses_persisted_expected_state() -> None:
    java = StubJava()
    java.set_draft({"title": "持久化的期望标题"})
    ledger = _ledger()
    op = _op(ledger, action="UPDATE_DRAFT", resource_id="d1", expected={"title": "持久化的期望标题"})
    worker = ReconciliationWorker(ledger, _adapter(java))
    await worker.reconcile_operation(op)
    fresh = ledger.store.get(op.operation_id)
    assert fresh.verified_status == "VERIFIED_COMPLETED"


# ── multi-step granularity / partial success ──────────────────────────────


def _multi_step_ctx() -> Any:
    class _Step:
        def __init__(self, sid: str, cap: str, constraints: dict[str, Any]) -> None:
            self.step_id = sid
            self.capability = cap
            self.constraints = constraints

    return SimpleNamespace(
        conversation_id="c1",
        task_id="t1",
        run_id="r1",
        trace_id="tr",
        execution_input=SimpleNamespace(
            steps=[
                _Step("s1", "MANAGE_DRAFT", {"draft_id": "d1", "title": "t"}),
                _Step("s2", "MANAGE_SCHEDULE", {"schedule_id": "s1", "run_at": "2026-08-16T09:00:00Z"}),
            ]
        ),
    )


def test_multi_step_write_creates_distinct_operations() -> None:
    service = RuntimeAgentService(operation_ledger=_ledger())
    assert service._dedupe_submission(_multi_step_ctx()) is None
    ledger = service._operation_ledger
    ops = ledger.store.list()
    assert len(ops) == 2, "one side effect == one OperationRecord"
    actions = {op.semantic_action for op in ops}
    assert actions == {"MANAGE_DRAFT", "MANAGE_SCHEDULE"}
    assert len({op.operation_id for op in ops}) == 2


def test_partial_success_keeps_distinct_operation_states() -> None:
    service = RuntimeAgentService(operation_ledger=_ledger())
    service._dedupe_submission(_multi_step_ctx())
    ledger = service._operation_ledger
    ops = sorted(ledger.store.list(), key=lambda o: o.semantic_action)
    # Step 1 succeeds, step 2 is RESULT_UNKNOWN.
    step1 = ledger.claim(ops[0].operation_id, owner="wA")
    ledger.complete(step1, status=OperationStatus.SUCCEEDED)
    step2 = ledger.claim(ops[1].operation_id, owner="wA")
    ledger.mark_result_unknown(step2)
    fresh = ledger.store.list()
    states = {o.semantic_action: o.status for o in fresh}
    assert states["MANAGE_DRAFT"] == OperationStatus.SUCCEEDED
    assert states["MANAGE_SCHEDULE"] == OperationStatus.RESULT_UNKNOWN


def test_duplicate_one_step_does_not_affect_other_step() -> None:
    service = RuntimeAgentService(operation_ledger=_ledger())
    service._dedupe_submission(_multi_step_ctx())
    ledger = service._operation_ledger
    ops = {o.semantic_action: o for o in ledger.store.list()}
    # Complete only the draft step.
    draft = ledger.claim(ops["MANAGE_DRAFT"].operation_id, owner="wA")
    ledger.complete(draft, status=OperationStatus.SUCCEEDED)
    # The schedule step is untouched and still independently reconcilable.
    sched = ledger.store.get(ops["MANAGE_SCHEDULE"].operation_id)
    assert sched.status == OperationStatus.PENDING
    assert sched.reconciliation_needed is False
