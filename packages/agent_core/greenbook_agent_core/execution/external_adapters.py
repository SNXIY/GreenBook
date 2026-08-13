"""Runtime-side external operation query contracts and test adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .operation_tracking import OperationStatus

ExternalStatus = OperationStatus | str | Mapping[str, Any] | None


class ExternalOperationAdapter(Protocol):
    """Read-only contract for querying an external logical operation."""

    name: str

    def query_operation_status(
        self,
        *,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalStatus: ...


ExternalStatusQuery = Callable[..., ExternalStatus]


class _CallbackExternalOperationAdapter:
    name = "external"

    def __init__(self, query: ExternalStatusQuery) -> None:
        self._query = query

    def query_operation_status(
        self,
        *,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalStatus:
        return self._query(
            external_operation_id=external_operation_id,
            receipt_id=receipt_id,
        )


class CreatorAdapter(_CallbackExternalOperationAdapter):
    """Runtime adapter boundary for future Creator operation queries."""

    name = "creator"


class JavaCommunityAdapter(_CallbackExternalOperationAdapter):
    """Runtime adapter boundary for future Java Community operation queries."""

    name = "java-community"


class MockExternalOperationAdapter:
    """Deterministic adapter for Runtime tests and local integration wiring."""

    name = "mock"

    def __init__(
        self,
        statuses: Mapping[str, ExternalStatus] | None = None,
        *,
        default: ExternalStatus = OperationStatus.UNKNOWN,
    ) -> None:
        self._statuses = dict(statuses or {})
        self._default = default
        self.calls: list[dict[str, str | None]] = []

    def set_status(
        self,
        *,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
        status: ExternalStatus,
    ) -> None:
        key = self._key(external_operation_id, receipt_id)
        if key is None:
            raise ValueError("An external operation id or receipt id is required")
        self._statuses[key] = status

    def query_operation_status(
        self,
        *,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalStatus:
        self.calls.append(
            {
                "external_operation_id": external_operation_id,
                "receipt_id": receipt_id,
            }
        )
        key = self._key(external_operation_id, receipt_id)
        return self._statuses.get(key, self._default) if key is not None else OperationStatus.UNKNOWN

    @staticmethod
    def _key(
        external_operation_id: str | None,
        receipt_id: str | None,
    ) -> str | None:
        if external_operation_id:
            return f"external:{external_operation_id}"
        if receipt_id:
            return f"receipt:{receipt_id}"
        return None


__all__ = [
    "CreatorAdapter",
    "ExternalOperationAdapter",
    "ExternalStatus",
    "ExternalStatusQuery",
    "JavaCommunityAdapter",
    "MockExternalOperationAdapter",
]
