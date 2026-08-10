"""Read-only reconciliation of externally observable operations.

Reconciliation turns an operation record's ambiguous result into the latest
status an external system can prove.  It deliberately does not mutate an
Execution or invoke a compensating action; callers decide what to do with the
returned status.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .external_adapters import ExternalOperationAdapter
from .operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStoreProtocol,
    OperationStatus,
)
from .state_manager import ExecutionStateManager


OperationStatusQuery = Callable[..., Any]


class ReconciliationAction(StrEnum):
    """Execution-facing consequence of an observed operation status."""

    RECOVER_EXECUTION = "RECOVER_EXECUTION"
    MARK_FAILED = "MARK_FAILED"
    REQUIRE_MANUAL_INTERVENTION = "REQUIRE_MANUAL_INTERVENTION"
    KEEP_UNKNOWN = "KEEP_UNKNOWN"


class ReconciliationResult(BaseModel):
    """Auditable result of querying one external operation."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    execution_id: str
    step_id: str
    status: OperationStatus
    action: ReconciliationAction
    execution_updated: bool = False
    reason: str = ""


class ReconciliationService:
    """Query one external operation and persist only the observed status."""

    def __init__(
        self,
        *,
        store: ExternalOperationStoreProtocol | None = None,
        query: OperationStatusQuery | None = None,
        adapter: ExternalOperationAdapter | None = None,
    ) -> None:
        self.store = store or ExternalOperationStore()
        self._query = query
        self._adapter = adapter

    def reconcile(self, operation: ExternalOperationRecord) -> OperationStatus:
        """Backward-compatible status-only facade.

        New recovery callers should use :meth:`reconcile_result` so the
        execution-facing action is preserved.
        """

        return self.reconcile_result(operation).status

    def reconcile_result(
        self,
        operation: ExternalOperationRecord,
    ) -> ReconciliationResult:
        """Query and return an explicit recovery decision without applying it.

        The external operation identifier is preferred over a receipt.  A
        missing query adapter or an ambiguous adapter response is treated as
        ``UNKNOWN`` so the Runtime never turns missing evidence into proof of
        success or absence.
        """

        self.store.save(operation)
        status = self._query_status(operation)
        self._record_status(operation, status)
        action = self._action_for(status)
        return ReconciliationResult(
            operation_id=operation.operation_id,
            execution_id=operation.execution_id,
            step_id=operation.step_id,
            status=status,
            action=action,
            reason=self._reason_for(status),
        )

    def _query_status(self, operation: ExternalOperationRecord) -> OperationStatus:
        if self._adapter is None and self._query is None:
            return OperationStatus.UNKNOWN

        identifier = self._query_identifier(operation)
        if identifier is None:
            return OperationStatus.UNKNOWN

        try:
            if self._adapter is not None:
                raw_status = self._adapter.query_operation_status(**identifier)
            else:
                raw_status = self._query(**identifier)
        except Exception:
            # A failed status lookup is itself an unknown observation.  The
            # service must not convert a query outage into NOT_FOUND.
            return OperationStatus.UNKNOWN
        return self._normalize_status(raw_status)

    def _record_status(
        self,
        operation: ExternalOperationRecord,
        status: OperationStatus,
    ) -> OperationStatus:
        self.store.update_status(operation.operation_id, status)
        return status

    @staticmethod
    def _action_for(status: OperationStatus) -> ReconciliationAction:
        if status == OperationStatus.SUCCEEDED:
            return ReconciliationAction.RECOVER_EXECUTION
        if status == OperationStatus.FAILED:
            return ReconciliationAction.MARK_FAILED
        if status == OperationStatus.NOT_FOUND:
            return ReconciliationAction.REQUIRE_MANUAL_INTERVENTION
        return ReconciliationAction.KEEP_UNKNOWN

    @staticmethod
    def _reason_for(status: OperationStatus) -> str:
        return {
            OperationStatus.SUCCEEDED: "External operation succeeded; Execution may be recovered.",
            OperationStatus.FAILED: "External operation failed; Execution should remain failed.",
            OperationStatus.NOT_FOUND: "External operation was not found; manual handling is required.",
            OperationStatus.UNKNOWN: "External operation status remains unknown; no Execution mutation is safe.",
        }.get(status, "External operation is still in progress; no Execution mutation is applied.")

    @staticmethod
    def _query_identifier(
        operation: ExternalOperationRecord,
    ) -> dict[str, str | None] | None:
        if operation.external_operation_id:
            return {
                "external_operation_id": operation.external_operation_id,
                "receipt_id": None,
            }
        if operation.receipt_id:
            return {
                "external_operation_id": None,
                "receipt_id": operation.receipt_id,
            }
        return None

    @classmethod
    def _normalize_status(cls, value: Any) -> OperationStatus:
        if isinstance(value, OperationStatus):
            return value
        if isinstance(value, Mapping):
            for key in ("status", "operation_status", "state"):
                if key in value:
                    return cls._normalize_status(value[key])
            return OperationStatus.UNKNOWN
        status_value = getattr(value, "status", None)
        if status_value is not None and status_value is not value:
            return cls._normalize_status(status_value)
        if value is None:
            return OperationStatus.UNKNOWN

        normalized = str(value).strip().upper()
        aliases = {
            "SUCCESS": OperationStatus.SUCCEEDED,
            "SUCCEEDED": OperationStatus.SUCCEEDED,
            "COMPLETED": OperationStatus.SUCCEEDED,
            "FAILURE": OperationStatus.FAILED,
            "FAILED": OperationStatus.FAILED,
            "ERROR": OperationStatus.FAILED,
            "PENDING": OperationStatus.PROCESSING,
            "PROCESSING": OperationStatus.PROCESSING,
            "SUBMITTED": OperationStatus.SUBMITTED,
            "CREATED": OperationStatus.CREATED,
            "NOT_FOUND": OperationStatus.NOT_FOUND,
            "404": OperationStatus.NOT_FOUND,
            "UNKNOWN": OperationStatus.UNKNOWN,
        }
        return aliases.get(normalized, OperationStatus.UNKNOWN)


class ReconciliationRecoveryService:
    """Apply a reconciliation result to existing Execution transitions."""

    def __init__(
        self,
        *,
        state_manager: ExecutionStateManager,
        reconciliation: ReconciliationService,
    ) -> None:
        self._state = state_manager
        self._reconciliation = reconciliation

    def reconcile_operation(
        self,
        operation: ExternalOperationRecord,
    ) -> ReconciliationResult:
        result = self._reconciliation.reconcile_result(operation)
        step_execution_id = self._step_execution_id(operation)
        if step_execution_id is None:
            return result.model_copy(
                update={
                    "execution_updated": False,
                    "reason": "Execution step for the operation was not found.",
                }
            )

        try:
            if result.status == OperationStatus.SUCCEEDED:
                self._state.reconcile_step_succeeded(
                    operation.execution_id,
                    step_execution_id,
                    operation_id=operation.operation_id,
                )
            elif result.status == OperationStatus.FAILED:
                self._state.reconcile_step_failed(
                    operation.execution_id,
                    step_execution_id,
                    error_code="EXTERNAL_OPERATION_FAILED",
                    error_message="External operation status is FAILED.",
                    operation_id=operation.operation_id,
                )
            elif result.status == OperationStatus.NOT_FOUND:
                self._state.mark_reconciliation_required(
                    operation.execution_id,
                    step_execution_id,
                    operation_id=operation.operation_id,
                )
            else:
                return result
        except ValueError as exc:
            return result.model_copy(
                update={
                    "execution_updated": False,
                    "reason": str(exc),
                }
            )
        return result.model_copy(update={"execution_updated": True})

    def _step_execution_id(
        self,
        operation: ExternalOperationRecord,
    ) -> str | None:
        for step in self._state.list_steps(operation.execution_id):
            if step.step_id == operation.step_id or step.step_execution_id == operation.step_id:
                return step.step_execution_id
        return None


__all__ = [
    "OperationStatusQuery",
    "ReconciliationAction",
    "ReconciliationRecoveryService",
    "ReconciliationResult",
    "ReconciliationService",
]
