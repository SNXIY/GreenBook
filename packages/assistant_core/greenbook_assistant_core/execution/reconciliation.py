"""Read-only reconciliation of externally observable operations.

Reconciliation turns an operation record's ambiguous result into the latest
status an external system can prove.  It deliberately does not mutate an
Execution or invoke a compensating action; callers decide what to do with the
returned status.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStoreProtocol,
    OperationStatus,
)


OperationStatusQuery = Callable[..., Any]


class ReconciliationService:
    """Query one external operation and persist only the observed status."""

    def __init__(
        self,
        *,
        store: ExternalOperationStoreProtocol | None = None,
        query: OperationStatusQuery | None = None,
    ) -> None:
        self.store = store or ExternalOperationStore()
        self._query = query

    def reconcile(self, operation: ExternalOperationRecord) -> OperationStatus:
        """Return the external status without retrying or changing Execution.

        The external operation identifier is preferred over a receipt.  A
        missing query adapter or an ambiguous adapter response is treated as
        ``UNKNOWN`` so the Runtime never turns missing evidence into proof of
        success or absence.
        """

        self.store.save(operation)
        if self._query is None:
            return self._record_status(operation, OperationStatus.UNKNOWN)

        identifier = self._query_identifier(operation)
        if identifier is None:
            return self._record_status(operation, OperationStatus.UNKNOWN)

        try:
            raw_status = self._query(**identifier)
        except Exception:
            # A failed status lookup is itself an unknown observation.  The
            # service must not convert a query outage into NOT_FOUND.
            return self._record_status(operation, OperationStatus.UNKNOWN)

        status = self._normalize_status(raw_status)
        return self._record_status(operation, status)

    def _record_status(
        self,
        operation: ExternalOperationRecord,
        status: OperationStatus,
    ) -> OperationStatus:
        self.store.update_status(operation.operation_id, status)
        return status

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


__all__ = ["OperationStatusQuery", "ReconciliationService"]
