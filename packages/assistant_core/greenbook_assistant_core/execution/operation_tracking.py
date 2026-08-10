"""Runtime-owned tracking for external logical operations.

The tracker records observed operation identity and status.  It does not call
an external service, reconcile an unknown result, or trigger a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from greenbook_contracts import ExternalAgentFailure, SideEffectState
from pydantic import BaseModel, ConfigDict, Field

from .evidence import ExecutionEvidence


class OperationStatus(StrEnum):
    """Observed lifecycle states for one external logical operation."""

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


class ExternalOperationRecord(BaseModel):
    """Runtime record that links a Step to an external operation."""

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


class ExternalOperationStore:
    """Small idempotent Runtime store for operation records."""

    def __init__(self) -> None:
        self._records: dict[str, ExternalOperationRecord] = {}

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
    ) -> ExternalOperationRecord:
        return self.observe(
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            evidence=evidence,
            failure=failure,
        )

    def observe_success(
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
            status=OperationStatus.SUCCEEDED,
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
]
