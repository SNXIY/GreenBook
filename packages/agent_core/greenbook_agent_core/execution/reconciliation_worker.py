"""Deterministic reconciliation worker for RESULT_UNKNOWN operations.

A background scan finds operations marked reconciliation-needed and queries the
external (Java) authoritative state through the read-only adapter — never an
LLM judgment.  Verified outcomes are written back through the fenced ledger;
still-unknown results are backed off, and after the retry budget they are left
for a manual boundary instead of being re-run as a write.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .operation_ledger import (
    LONG_RECONCILE_BACKOFF_SECONDS,
    MAX_RECONCILE_ATTEMPTS,
    OperationLedger,
    is_reconciliation_exhausted,
)
from .operation_tracking import (
    ExternalOperationRecord,
    OperationStatus,
    RetryClassification,
)

logger = logging.getLogger(__name__)

ReconciliationCompletion = Callable[
    [ExternalOperationRecord, OperationStatus], Awaitable[None] | None
]


def _observe_reconciliation(operation: ExternalOperationRecord, outcome: str) -> None:
    try:
        from greenbook_agent_core.observability.bus import observability

        ob = observability()
        ob.reconciliation().inc(outcome=outcome)
        ob.record_trace(
            "reconciliation_" + ("completed" if outcome in {"SUCCEEDED", "FAILED"} else "deferred"),
            trace_id=operation.trace_id,
            conversation_id=operation.conversation_id,
            operation_id=operation.operation_id,
            semantic_action=operation.semantic_action,
            status=outcome,
        )
    except Exception:  # noqa: BLE001 - observability must never break the loop
        pass


class ReconciliationWorker:
    """Reconcile one RESULT_UNKNOWN operation (or a scan of them)."""

    def __init__(
        self,
        ledger: OperationLedger,
        adapter: Any | None = None,
        *,
        max_reconcile_attempts: int = MAX_RECONCILE_ATTEMPTS,
        on_reconciled: ReconciliationCompletion | None = None,
    ) -> None:
        self._ledger = ledger
        self._adapter = adapter
        self._max_attempts = max(1, max_reconcile_attempts)
        self._on_reconciled = on_reconciled

    async def reconcile_operation(self, operation: ExternalOperationRecord) -> str:
        """Reconcile one operation against authoritative state.

        Returns the observed OperationStatus value after reconciliation
        (SUCCEEDED / FAILED / NOT_FOUND / RESULT_UNKNOWN).
        """
        status = await self._query_status(operation)
        if status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.NOT_FOUND,
        }:
            # Projection is a durable, idempotent local continuation of the
            # read-only reconciliation result.  Run it before the ledger
            # terminal CAS so a process crash/failure between the two writes
            # leaves the operation RESULT_UNKNOWN and therefore recoverable on
            # the next scan.  A successful projection followed by a crash is
            # safe too: Objective/Execution/Observation projections are
            # idempotent, and the next reconciliation simply replays them.
            if self._on_reconciled is not None:
                callback_result = self._on_reconciled(operation, status)
                if inspect.isawaitable(callback_result):
                    await callback_result
            self._ledger.reconcile_unknown(
                operation,
                status=status,
                reason=_reason_for(operation, status),
            )
            _observe_reconciliation(operation, status.value)
            return status.value
        # Still unknown: use the existing short backoff budget first, then
        # lower the query frequency without changing RESULT_UNKNOWN or ever
        # submitting a second write.
        attempts = int(operation.reconcile_attempts or 0) + 1
        if attempts > self._max_attempts:
            self._ledger.reconcile_unknown(
                operation,
                status=OperationStatus.UNKNOWN,
                reason="reconciliation budget exhausted; awaiting manual handling",
                backoff_seconds=LONG_RECONCILE_BACKOFF_SECONDS,
            )
            return OperationStatus.RESULT_UNKNOWN.value
        self._ledger.reconcile_unknown(
            operation,
            status=OperationStatus.UNKNOWN,
            reason="authoritative state still unknown; scheduled for backoff retry",
        )
        return OperationStatus.RESULT_UNKNOWN.value

    async def reconcile_due(self, *, limit: int = 50) -> list[str]:
        """Reconcile every operation that is currently due."""
        outcomes: list[str] = []
        for operation in self._ledger.find_reconciliation_needed(limit=limit):
            # The durable row is intentionally retained for manual handling,
            # but an exhausted operation is no longer a live reconciliation
            # candidate.  Re-querying it would turn historical uncertainty
            # into perpetual current activity without changing the truth.
            if is_reconciliation_exhausted(operation):
                outcomes.append(OperationStatus.RESULT_UNKNOWN.value)
                continue
            if _permanently_retryable(operation):
                self._ledger.reconcile_unknown(
                    operation,
                    status=OperationStatus.UNKNOWN,
                    reason="retry classification is permanent; no reconciliation",
                )
                outcomes.append(OperationStatus.RESULT_UNKNOWN.value)
                continue
            try:
                outcome = await self.reconcile_operation(operation)
            except Exception:  # noqa: BLE001 - a query outage is itself unknown
                outcome = OperationStatus.RESULT_UNKNOWN.value
            outcomes.append(outcome)
        return outcomes

    async def _query_status(self, operation: ExternalOperationRecord) -> OperationStatus:
        adapter = self._adapter
        if adapter is None:
            return OperationStatus.UNKNOWN
        # Preferred: a full reconcile(operation) using persisted postcondition.
        reconcile = getattr(adapter, "reconcile", None)
        if callable(reconcile):
            try:
                value = reconcile(operation)
                value = await value if inspect.isawaitable(value) else value
                return _normalize(value)
            except Exception:  # noqa: BLE001 - query outage => unknown
                return OperationStatus.UNKNOWN
        # Fallback: the read-only identifier query contract.
        if not callable(getattr(adapter, "query_operation_status", None)):
            return OperationStatus.UNKNOWN
        identifier = _query_identifier(operation)
        if identifier is None:
            return OperationStatus.UNKNOWN
        try:
            value = adapter.query_operation_status(**identifier)
            value = await value if inspect.isawaitable(value) else value
        except Exception:  # noqa: BLE001 - query outage => unknown, not NOT_FOUND
            return OperationStatus.UNKNOWN
        return _normalize(value)


def _query_identifier(operation: ExternalOperationRecord) -> dict[str, str | None] | None:
    if operation.external_operation_id:
        return {"external_operation_id": operation.external_operation_id, "receipt_id": None}
    if operation.receipt_id:
        return {"external_operation_id": None, "receipt_id": operation.receipt_id}
    # Fall back to the idempotency key as the query handle when present.
    if operation.idempotency_key:
        return {"external_operation_id": operation.idempotency_key, "receipt_id": None}
    return None


def _normalize(value: Any) -> OperationStatus:
    if isinstance(value, OperationStatus):
        return value
    if isinstance(value, dict):
        for key in ("status", "operation_status", "state"):
            if key in value:
                return _normalize(value[key])
        return OperationStatus.UNKNOWN
    if value is None:
        return OperationStatus.UNKNOWN
    aliases = {
        "SUCCESS": OperationStatus.SUCCEEDED,
        "SUCCEEDED": OperationStatus.SUCCEEDED,
        "COMPLETED": OperationStatus.SUCCEEDED,
        "DONE": OperationStatus.SUCCEEDED,
        "FAILURE": OperationStatus.FAILED,
        "FAILED": OperationStatus.FAILED,
        "ERROR": OperationStatus.FAILED,
        "CANCELLED": OperationStatus.FAILED,
        "CANCELED": OperationStatus.FAILED,
        # This is the operation/idempotency lookup result, not a resource
        # business rejection.  The ledger may return a provably unapplied
        # logical write to PENDING for a safe retry.
        "NOT_FOUND": OperationStatus.NOT_FOUND,
        "404": OperationStatus.NOT_FOUND,
        "UNKNOWN": OperationStatus.UNKNOWN,
    }
    return aliases.get(str(value).strip().upper(), OperationStatus.UNKNOWN)


def _permanently_retryable(operation: ExternalOperationRecord) -> bool:
    return operation.retry_classification == RetryClassification.PERMANENT_FAILURE.value


def _reason_for(operation: ExternalOperationRecord, status: OperationStatus) -> str:
    action = operation.semantic_action or operation.tool_name or operation.step_id
    if status == OperationStatus.SUCCEEDED:
        return f"{action}: authoritative state confirms completion"
    if status == OperationStatus.NOT_FOUND:
        return f"{action}: operation identity was not found; original write was not applied"
    return f"{action}: authoritative state confirms failure"


__all__ = ["ReconciliationWorker"]
