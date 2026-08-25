"""Runtime-owned tracking for external logical operations.

The tracker records observed operation identity and status.  It does not call
an external service, reconcile an unknown result, or trigger a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol

from greenbook_contracts import ExternalAgentFailure, SideEffectState
from pydantic import BaseModel, ConfigDict, Field

from .evidence import ExecutionEvidence


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class OperationStatus(StrEnum):
    """Observed lifecycle states for one external logical operation.

    ``RESULT_UNKNOWN`` is the durable, reconciliation-required state for a write
    whose Java-side outcome could not be resolved: it must never be re-run as a
    normal retry, only reconciled against authoritative state.
    """

    CREATED = "CREATED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


class RetryClassification(StrEnum):
    """How a failed operation may safely be retried."""

    SAFE_RETRY = "SAFE_RETRY"          # request provably not sent / read / precondition
    RESULT_UNKNOWN = "RESULT_UNKNOWN"  # a write may have reached Java; reconcile first
    PERMANENT_FAILURE = "PERMANENT_FAILURE"  # schema / permission / 404/409


class VerifiedStatus(StrEnum):
    """Outcome of a deterministic reconciliation against authoritative state."""

    VERIFIED_COMPLETED = "VERIFIED_COMPLETED"
    VERIFIED_FAILED = "VERIFIED_FAILED"
    VERIFIED_UNKNOWN = "VERIFIED_UNKNOWN"


class ExternalOperationRecord(BaseModel):
    """Runtime record that links a Step to an external operation.

    Reliability fields (claim/fencing) let a durable ledger reject a stale
    worker and dedupe duplicate delivery without depending on Java idempotency
    alone.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str
    execution_id: str
    step_id: str
    tool_name: str = ""
    status: OperationStatus = OperationStatus.CREATED
    external_operation_id: str | None = None
    receipt_id: str | None = None
    idempotency_key: str | None = None
    runtime_idempotency_key: str | None = None
    external_idempotency_key: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    evidence: ExecutionEvidence | None = None

    # ── Phase 4B reliability fields ──────────────────────────────────
    trace_id: str = ""
    conversation_id: str = ""
    semantic_action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    # Immutable correlation id: the Objective that initiated this Operation.
    # Set once at submit time; preserved across WAITING_EXTERNAL/retry/reconcile
    # so a verified Resource always binds to the SAME Objective, never re-inferred
    # from the result-time current Objective.  Optional for legacy records.
    objective_id: str | None = None
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0
    claim_owner: str = ""
    claim_version: int = 0
    fencing_token: str = ""
    lease_expires_at: str = ""
    side_effect_started: bool = False
    reconciliation_needed: bool = False
    retry_classification: str = ""
    reconcile_attempts: int = 0
    verified_status: str = ""
    verified_reason: str = ""
    next_reconcile_at: str = ""


class ExternalOperationStoreProtocol(Protocol):
    """Storage contract shared by memory and SQLAlchemy operation stores."""

    def create(self, record: ExternalOperationRecord) -> ExternalOperationRecord: ...

    def save(self, record: ExternalOperationRecord) -> ExternalOperationRecord: ...

    def get(self, operation_id: str) -> ExternalOperationRecord | None: ...

    def update_status(
        self,
        operation_id: str,
        status: OperationStatus,
    ) -> ExternalOperationRecord | None: ...

    def find(
        self,
        *,
        execution_id: str | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalOperationRecord | None: ...

    def find_by_execution_id(self, execution_id: str) -> list[ExternalOperationRecord]: ...

    def find_by_external_operation_id(
        self,
        external_operation_id: str,
    ) -> ExternalOperationRecord | None: ...

    def list(self) -> list[ExternalOperationRecord]: ...

    def save_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        updates: dict[str, Any],
    ) -> ExternalOperationRecord | None: ...

    def claim(
        self,
        operation_id: str,
        *,
        expected_status: OperationStatus,
        new_status: OperationStatus,
        owner: str,
    ) -> ExternalOperationRecord | None: ...

    def complete_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        status: OperationStatus,
        evidence: ExecutionEvidence | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalOperationRecord | None: ...

    def find_reconciliation_needed(
        self,
        *,
        now: str = "",
        limit: int = 50,
    ) -> list[ExternalOperationRecord]: ...


class ExternalOperationStore:
    """Small idempotent Runtime store for operation records."""

    def __init__(self) -> None:
        self._records: dict[str, ExternalOperationRecord] = {}
        self._lock = RLock()

    def get(self, operation_id: str) -> ExternalOperationRecord | None:
        record = self._records.get(operation_id)
        return record.model_copy(deep=True) if record is not None else None

    def create(self, record: ExternalOperationRecord) -> ExternalOperationRecord:
        return self.save(record)

    def save(self, record: ExternalOperationRecord) -> ExternalOperationRecord:
        self._records[record.operation_id] = record.model_copy(deep=True)
        return record.model_copy(deep=True)

    def update_status(
        self,
        operation_id: str,
        status: OperationStatus,
    ) -> ExternalOperationRecord | None:
        current = self._records.get(operation_id)
        if current is None:
            return None
        updated = current.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            deep=True,
        )
        return self.save(updated)

    def find(
        self,
        *,
        execution_id: str | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalOperationRecord | None:
        for record in self._records.values():
            if execution_id is not None and record.execution_id != execution_id:
                continue
            if external_operation_id is None and receipt_id is None:
                if execution_id is not None:
                    return record.model_copy(deep=True)
                continue
            if (
                external_operation_id is not None
                and record.external_operation_id == external_operation_id
            ):
                return record.model_copy(deep=True)
            if receipt_id is not None and record.receipt_id == receipt_id:
                return record.model_copy(deep=True)
        return None

    def find_by_execution_id(self, execution_id: str) -> list[ExternalOperationRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._records.values()
            if record.execution_id == execution_id
        ]

    def find_by_external_operation_id(
        self,
        external_operation_id: str,
    ) -> ExternalOperationRecord | None:
        return self.find(external_operation_id=external_operation_id)

    def find_by_receipt_id(self, receipt_id: str) -> ExternalOperationRecord | None:
        return self.find(receipt_id=receipt_id)

    def list(self) -> list[ExternalOperationRecord]:
        return [record.model_copy(deep=True) for record in self._records.values()]

    def claim(
        self,
        operation_id: str,
        *,
        expected_status: OperationStatus,
        new_status: OperationStatus,
        owner: str,
        lease_expires_at: str = "",
    ) -> ExternalOperationRecord | None:
        """Atomically claim an operation only if it is still in the expected
        state.  Each successful claim bumps the version (fencing token), so a
        stale worker holding an older version can never advance the operation.
        """
        with self._lock:
            current = self._records.get(operation_id)
            if current is None or current.status != expected_status:
                return None
            updated = current.model_copy(
                update={
                    "status": new_status,
                    "claim_owner": owner,
                    "attempt": current.attempt + 1,
                    "claim_version": current.claim_version + 1,
                    "fencing_token": f"{current.claim_version + 1}:{owner}",
                    "lease_expires_at": lease_expires_at or current.lease_expires_at,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                deep=True,
            )
            self._records[operation_id] = updated
            return updated.model_copy(deep=True)

    def reclaim_expired_lease(
        self,
        operation_id: str,
        *,
        owner: str,
        now: str,
    ) -> ExternalOperationRecord | None:
        """Reclaim a RUNNING operation whose lease expired, only when no side
        effect has started.  Advances the fencing token so the stale worker is
        rejected.  A side effect that may have reached Java (side_effect_started
        true) is never reclaimed as a fresh write — it stays for reconciliation.
        """
        with self._lock:
            current = self._records.get(operation_id)
            if current is None:
                return None
            if current.status != OperationStatus.RUNNING:
                return None
            if current.side_effect_started:
                return None
            lease_expires = _parse_iso(current.lease_expires_at)
            if lease_expires is None or lease_expires > _parse_iso(now):
                return None
            updated = current.model_copy(
                update={
                    "claim_owner": owner,
                    "attempt": current.attempt + 1,
                    "claim_version": current.claim_version + 1,
                    "fencing_token": f"{current.claim_version + 1}:{owner}",
                    "lease_expires_at": current.lease_expires_at,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                deep=True,
            )
            self._records[operation_id] = updated
            return updated.model_copy(deep=True)

    def complete_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        status: OperationStatus,
        evidence: ExecutionEvidence | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
        verified_status: str = "",
        verified_reason: str = "",
    ) -> ExternalOperationRecord | None:
        """Fenced completion: only a claimer holding the current version may
        mark the operation terminal.  A stale worker (older version) is
        rejected, so it can never overwrite a newer worker's result.
        """
        with self._lock:
            current = self._records.get(operation_id)
            if current is None or current.claim_version != expected_version:
                return None
            updated = current.model_copy(
                update={
                    "status": status,
                    "claim_version": current.claim_version + 1,
                    "fencing_token": f"{current.claim_version + 1}:{current.claim_owner}",
                    "evidence": evidence or current.evidence,
                    "external_operation_id": external_operation_id or current.external_operation_id,
                    "receipt_id": receipt_id or current.receipt_id,
                    "reconciliation_needed": False,
                    "verified_status": verified_status or current.verified_status,
                    "verified_reason": verified_reason or current.verified_reason,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                deep=True,
            )
            self._records[operation_id] = updated
            return updated.model_copy(deep=True)

    def save_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        updates: dict[str, Any],
    ) -> ExternalOperationRecord | None:
        """Fenced partial update: a stale worker (older version) is rejected."""
        with self._lock:
            current = self._records.get(operation_id)
            if current is None or current.claim_version != expected_version:
                return None
            updated = current.model_copy(
                update={
                    **updates,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                deep=True,
            )
            self._records[operation_id] = updated
            return updated.model_copy(deep=True)

    def find_reconciliation_needed(
        self,
        *,
        now: str = "",
        limit: int = 50,
    ) -> list[ExternalOperationRecord]:
        with self._lock:
            result = [
                record.model_copy(deep=True)
                for record in self._records.values()
                if record.reconciliation_needed
                and (not record.next_reconcile_at or not now or record.next_reconcile_at <= now)
            ]
        return result[: max(1, int(limit))]

    def count(self) -> int:
        return len(self._records)

    def clear(self, execution_id: str | None = None) -> None:
        if execution_id is None:
            self._records.clear()
            return
        for operation_id, record in list(self._records.items()):
            if record.execution_id == execution_id:
                del self._records[operation_id]


class ExternalOperationTracker:
    """Observe Runtime results into an idempotent operation store."""

    _TERMINAL = frozenset({
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.NOT_FOUND,
    })

    def __init__(
        self,
        store: ExternalOperationStoreProtocol | None = None,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store or ExternalOperationStore()
        self._now = now_factory or (lambda: datetime.now(UTC))

    def observe(
        self,
        *,
        execution_id: str,
        step_id: str,
        tool_name: str,
        evidence: ExecutionEvidence,
        status: OperationStatus | None = None,
        failure: ExternalAgentFailure | None = None,
        pending: bool = False,
        objective_id: str | None = None,
    ) -> ExternalOperationRecord:
        """Create or update one record without creating duplicate operations."""

        operation_id = evidence.operation_id or self._stable_operation_id(
            execution_id,
            step_id,
        )
        observed_status = status or self._status_for(
            evidence,
            failure=failure,
            pending=pending,
        )
        existing = self.store.get(operation_id)
        if existing is not None and existing.status in self._TERMINAL:
            observed_status = existing.status
        now = self._now().isoformat()
        external_key = evidence.external_idempotency_key
        runtime_key = evidence.runtime_idempotency_key
        record = ExternalOperationRecord(
            operation_id=operation_id,
            execution_id=execution_id or evidence.execution_id or "",
            step_id=step_id or evidence.step_id or "",
            tool_name=tool_name,
            status=observed_status,
            external_operation_id=evidence.external_operation_id,
            receipt_id=evidence.receipt_id,
            idempotency_key=external_key or runtime_key,
            runtime_idempotency_key=runtime_key,
            external_idempotency_key=external_key,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            evidence=evidence,
            objective_id=objective_id,
        )
        return self.store.save(record)

    def observe_failure(
        self,
        *,
        execution_id: str,
        step_id: str,
        tool_name: str,
        evidence: ExecutionEvidence,
        failure: ExternalAgentFailure,
        objective_id: str | None = None,
    ) -> ExternalOperationRecord:
        return self.observe(
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            evidence=evidence,
            failure=failure,
            objective_id=objective_id,
        )

    def observe_success(
        self,
        *,
        execution_id: str,
        step_id: str,
        tool_name: str,
        evidence: ExecutionEvidence,
        objective_id: str | None = None,
    ) -> ExternalOperationRecord:
        return self.observe(
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            evidence=evidence,
            status=OperationStatus.SUCCEEDED,
            objective_id=objective_id,
        )

    def observe_pending(
        self,
        *,
        execution_id: str,
        step_id: str,
        tool_name: str,
        evidence: ExecutionEvidence,
    ) -> ExternalOperationRecord:
        return self.observe(
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            evidence=evidence,
            status=OperationStatus.PROCESSING,
            pending=True,
        )

    @staticmethod
    def _stable_operation_id(execution_id: str, step_id: str) -> str:
        material = f"greenbook:operation:{execution_id}:{step_id}"
        return f"op-{uuid.uuid5(uuid.NAMESPACE_URL, material)}"

    @staticmethod
    def _status_for(
        evidence: ExecutionEvidence,
        *,
        failure: ExternalAgentFailure | None,
        pending: bool,
    ) -> OperationStatus:
        if pending:
            return OperationStatus.PROCESSING
        if failure is None:
            return OperationStatus.SUCCEEDED
        if (
            evidence.request_sent is False
            and evidence.side_effect_state
            in {SideEffectState.NONE, SideEffectState.NOT_STARTED}
        ):
            return OperationStatus.FAILED
        return OperationStatus.UNKNOWN


__all__ = [
    "ExternalOperationRecord",
    "ExternalOperationStore",
    "ExternalOperationStoreProtocol",
    "ExternalOperationTracker",
    "OperationStatus",
    "RetryClassification",
    "VerifiedStatus",
]
