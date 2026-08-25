"""Durable Operation Ledger: dedupe, claim, and fencing for logical writes.

The ledger is the reliability boundary for a write before it reaches the
external (Java) system.  It guarantees, without depending on Java idempotency
alone:

  * duplicate delivery / restart is deduped via a stable operation id derived
    from the idempotency key;
  * exactly one worker claims an operation (row-level CAS on status + version);
  * every later state write is fenced on the claim version, so a stale worker
    that lost its lease can never advance or overwrite a newer worker's result;
  * RESULT_UNKNOWN is never re-run as a normal retry — it is marked for
    reconciliation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .evidence import ExecutionEvidence
from .operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStoreProtocol,
    OperationStatus,
)

DEFAULT_RECONCILE_BACKOFF_SECONDS = [10, 30, 90, 300]
# Once the short reconciliation budget is exhausted, keep the operation in
# RESULT_UNKNOWN but reduce query frequency.  This reuses next_reconcile_at;
# it is not a second scheduler or terminal state.
LONG_RECONCILE_BACKOFF_SECONDS = 3600


def _observe_operation(
    record: ExternalOperationRecord | None,
    stage: str,
    outcome: str = "",
    *,
    retry_classification: str = "",
) -> None:
    """Record operation metrics + trace without ever breaking the ledger."""
    if record is None:
        return
    try:
        from greenbook_agent_core.observability.bus import observability

        ob = observability()
        action = record.semantic_action or record.tool_name or "WRITE"
        ob.operation().inc(semantic_action=action, outcome=outcome or stage)
        if outcome == "RESULT_UNKNOWN":
            ob.result_unknown().inc()
        if retry_classification:
            ob.operation_retry().inc(retry_classification=retry_classification)
        if stage == "reclaimed":
            ob.worker_reclaim().inc()
        ob.record_trace(
            "operation_" + stage,
            trace_id=record.trace_id,
            conversation_id=record.conversation_id,
            operation_id=record.operation_id,
            semantic_action=action,
            status=outcome or record.status.value,
        )
    except Exception:  # noqa: BLE001 - observability must never break the ledger
        pass
MAX_RECONCILE_ATTEMPTS = len(DEFAULT_RECONCILE_BACKOFF_SECONDS)


def is_reconciliation_exhausted(operation: Any) -> bool:
    """Identify an unknown operation that crossed the safe query budget.

    The ledger row remains durable for manual handling.  This predicate is
    intentionally read-only and is used only to keep that historical,
    unresolved fact out of current activity/target projections.
    """

    status = str(getattr(getattr(operation, "status", None), "value", getattr(operation, "status", "")) or "").upper()
    verified_status = str(getattr(operation, "verified_status", "") or "").upper()
    reason = str(getattr(operation, "verified_reason", "") or "").lower()
    return bool(
        status == OperationStatus.RESULT_UNKNOWN.value
        and bool(getattr(operation, "reconciliation_needed", False))
        and verified_status == "VERIFIED_UNKNOWN"
        and "budget exhausted" in reason
    )


def stable_operation_id(idempotency_key: str) -> str:
    """Derive a deterministic operation id so a restarted process reuses it."""
    material = f"greenbook:operation:{idempotency_key}"
    return f"op-{uuid.uuid5(uuid.NAMESPACE_URL, material)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class OperationLedger:
    """Persistent, fenced ledger for one durable write operation."""

    def __init__(
        self,
        store: ExternalOperationStoreProtocol,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._now = now_factory or (lambda: datetime.now(UTC))

    # ── begin / dedupe ────────────────────────────────────────────────

    def begin_operation(
        self,
        *,
        idempotency_key: str,
        conversation_id: str = "",
        task_id: str = "",
        execution_id: str = "",
        step_id: str = "",
        tool_name: str = "",
        semantic_action: str = "",
        resource_type: str = "",
        resource_id: str = "",
        expected_postcondition: dict[str, Any] | None = None,
        claim_owner: str = "",
        receipt_id: str | None = None,
        external_operation_id: str | None = None,
        trace_id: str = "",
    ) -> ExternalOperationRecord:
        """Return the existing operation for this key, or create a PENDING one.

        A duplicate delivery (or a restart that re-submits the same logical
        write) returns the already-recorded operation instead of starting a
        second one, so the business operation is not duplicated.
        """
        op_id = stable_operation_id(idempotency_key)
        existing = self.store.get(op_id)
        if existing is not None:
            return existing
        record = ExternalOperationRecord(
            operation_id=op_id,
            trace_id=trace_id,
            conversation_id=conversation_id,
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            status=OperationStatus.PENDING,
            idempotency_key=idempotency_key,
            runtime_idempotency_key=idempotency_key,
            semantic_action=semantic_action,
            resource_type=resource_type,
            resource_id=resource_id,
            expected_postcondition=dict(expected_postcondition or {}),
            claim_owner=claim_owner,
            receipt_id=receipt_id,
            external_operation_id=external_operation_id,
            attempt=0,
            claim_version=0,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        stored = self.store.create(record)
        _observe_operation(stored, "created")
        return stored

    # ── claim / fencing ───────────────────────────────────────────────

    def claim(
        self,
        operation_id: str,
        *,
        owner: str,
        expected_status: OperationStatus = OperationStatus.PENDING,
        lease_seconds: int = 60,
    ) -> ExternalOperationRecord | None:
        """Atomically claim an operation only if it is still PENDING.

        Only one worker can claim; the returned record carries the fencing
        ``claim_version`` that all subsequent writes must present.  A lease
        deadline is stamped so a crashed worker's claim can be reclaimed.
        """
        lease_expires_at = (
            (self._now() + timedelta(seconds=max(1, lease_seconds))).isoformat()
            if lease_seconds
            else ""
        )
        return self.store.claim(
            operation_id,
            expected_status=expected_status,
            new_status=OperationStatus.RUNNING,
            owner=owner,
            lease_expires_at=lease_expires_at,
        )

    def claim_after_lease_expiry(
        self,
        operation_id: str,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> ExternalOperationRecord | None:
        """Reclaim a RUNNING operation after its lease expired.

        Only safe when no side effect has started (``side_effect_started`` is
        False).  A side effect that may have reached Java is never reclaimed as
        a fresh write — it stays RESULT_UNKNOWN for reconciliation.  The
        fencing token advances so a stale worker can no longer write a terminal
        state.
        """
        reclaim = getattr(self.store, "reclaim_expired_lease", None)
        if not callable(reclaim):
            return None
        now_iso = (now or self._now()).isoformat()
        result = reclaim(operation_id, owner=owner, now=now_iso)
        _observe_operation(result, "reclaimed", retry_classification=result.retry_classification if result else "")
        return result

    def mark_side_effect_started(
        self,
        operation: ExternalOperationRecord,
        *,
        request_sent: bool,
    ) -> ExternalOperationRecord | None:
        """Record that the external request was (or may have been) sent.

        From this point the operation must never be re-run as a normal retry.
        The update is fenced on the claim version so a stale worker cannot
        mutate this reliability state.
        """
        return self.store.save_if_version(
            operation.operation_id,
            expected_version=operation.claim_version,
            updates={
                "side_effect_started": True,
                "retry_classification": (
                    "RESULT_UNKNOWN" if request_sent else operation.retry_classification
                ),
            },
        )

    def mark_result_unknown(
        self,
        operation: ExternalOperationRecord,
        *,
        next_reconcile_at: str = "",
        evidence: ExecutionEvidence | None = None,
    ) -> ExternalOperationRecord | None:
        """Mark a write whose outcome is unknown, requiring reconciliation."""
        updates: dict[str, Any] = {
            "status": OperationStatus.RESULT_UNKNOWN.value,
            "reconciliation_needed": True,
            "retry_classification": "RESULT_UNKNOWN",
            "side_effect_started": True,
            "next_reconcile_at": next_reconcile_at or _now_iso(),
        }
        if evidence is not None:
            # Preserve the observed resource identity even when the final
            # acknowledgement was lost; reconciliation must read Java truth
            # without replaying the write.
            updates["evidence"] = evidence.model_dump(mode="json")
        result = self.store.save_if_version(
            operation.operation_id,
            expected_version=operation.claim_version,
            updates=updates,
        )
        _observe_operation(result, "result_unknown", "RESULT_UNKNOWN")
        return result

    def release_claim(
        self,
        operation: ExternalOperationRecord,
    ) -> ExternalOperationRecord | None:
        """Release a claim when execution paused before side effects began.

        A user pause is not an external failure and must remain retryable on
        resume.  Advance the fencing version while returning the operation to
        PENDING so a stale worker cannot complete it after the next claim.
        """
        return self.store.save_if_version(
            operation.operation_id,
            expected_version=operation.claim_version,
            updates={
                "status": OperationStatus.PENDING.value,
                "claim_owner": "",
                "lease_expires_at": "",
                "claim_version": operation.claim_version + 1,
                "side_effect_started": False,
                "reconciliation_needed": False,
                "retry_classification": "",
            },
        )

    # ── completion (fenced) ───────────────────────────────────────────

    def complete(
        self,
        operation: ExternalOperationRecord,
        *,
        status: OperationStatus,
        evidence: ExecutionEvidence | None = None,
        receipt_id: str | None = None,
        external_operation_id: str | None = None,
    ) -> ExternalOperationRecord | None:
        """Fenced terminal write.  Returns None if a stale version is present."""
        result = self.store.complete_if_version(
            operation.operation_id,
            expected_version=operation.claim_version,
            status=status,
            evidence=evidence,
            receipt_id=receipt_id,
            external_operation_id=external_operation_id,
        )
        _observe_operation(result, "completed", status.value)
        return result

    # ── reconciliation ────────────────────────────────────────────────

    def reconcile_unknown(
        self,
        operation: ExternalOperationRecord,
        *,
        status: OperationStatus,
        reason: str = "",
        backoff_seconds: int | None = None,
    ) -> ExternalOperationRecord | None:
        """Fenced reconciliation with one safe ``NOT_FOUND`` recovery.

        ``NOT_FOUND`` is safe to replay only when it is returned by the
        operation/idempotency lookup and therefore proves that the original
        request was not applied.  Resource-level business ``NOT_FOUND``
        responses are classified before this method and never use this path.
        """
        if status == OperationStatus.SUCCEEDED:
            return self.store.complete_if_version(
                operation.operation_id,
                expected_version=operation.claim_version,
                status=OperationStatus.SUCCEEDED,
                verified_status="VERIFIED_COMPLETED",
                verified_reason=reason or "reconciled against authoritative state",
            )
        if status == OperationStatus.FAILED:
            return self.store.complete_if_version(
                operation.operation_id,
                expected_version=operation.claim_version,
                status=OperationStatus.FAILED,
                verified_status="VERIFIED_FAILED",
                verified_reason=reason or "reconciled against authoritative state",
            )
        if status == OperationStatus.NOT_FOUND:
            next_version = operation.claim_version + 1
            return self.store.save_if_version(
                operation.operation_id,
                expected_version=operation.claim_version,
                updates={
                    # The authoritative operation lookup proved that this
                    # logical write was not applied, so it may return to the
                    # normal durable pending/claim path.
                    "status": OperationStatus.PENDING.value,
                    "claim_owner": "",
                    "lease_expires_at": "",
                    "claim_version": next_version,
                    "fencing_token": f"{next_version}:",
                    "side_effect_started": False,
                    "reconciliation_needed": False,
                    "retry_classification": "SAFE_RETRY",
                    "verified_status": "",
                    "verified_reason": reason or "operation identity not found; safe to retry",
                    "next_reconcile_at": "",
                },
            )
        # Still unknown: back off, do not retry the write.  Fenced on version.
        attempts = int(operation.reconcile_attempts or 0) + 1
        delay = (
            max(0, int(backoff_seconds))
            if backoff_seconds is not None
            else _backoff(attempts)
        )
        next_at = (self._now() + timedelta(seconds=delay)).isoformat()
        return self.store.save_if_version(
            operation.operation_id,
            expected_version=operation.claim_version,
            updates={
                "status": OperationStatus.RESULT_UNKNOWN.value,
                "reconciliation_needed": True,
                "reconcile_attempts": attempts,
                "verified_status": "VERIFIED_UNKNOWN",
                "verified_reason": reason or "authoritative state still unknown",
                "next_reconcile_at": next_at,
            },
        )

    def find_reconciliation_needed(
        self,
        *,
        now: str = "",
        limit: int = 50,
    ) -> list[ExternalOperationRecord]:
        return self.store.find_reconciliation_needed(
            now=now or _now_iso(),
            limit=limit,
        )


def _backoff(attempt: int) -> int:
    if attempt <= 0:
        return DEFAULT_RECONCILE_BACKOFF_SECONDS[0]
    index = min(attempt - 1, len(DEFAULT_RECONCILE_BACKOFF_SECONDS) - 1)
    return DEFAULT_RECONCILE_BACKOFF_SECONDS[index]


__all__ = [
    "DEFAULT_RECONCILE_BACKOFF_SECONDS",
    "LONG_RECONCILE_BACKOFF_SECONDS",
    "MAX_RECONCILE_ATTEMPTS",
    "OperationLedger",
    "is_reconciliation_exhausted",
    "stable_operation_id",
]
