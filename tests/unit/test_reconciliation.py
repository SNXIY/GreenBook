"""Phase 10-H reconciliation foundation tests."""

from __future__ import annotations

from typing import Any

from greenbook_assistant_core.execution.evidence import ExecutionEvidence
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_assistant_core.execution.reconciliation import ReconciliationService


def _operation(**overrides: Any) -> ExternalOperationRecord:
    values: dict[str, Any] = {
        "operation_id": "operation-1",
        "execution_id": "execution-1",
        "step_id": "step-1",
        "tool_name": "content.publish",
        "status": OperationStatus.UNKNOWN,
        "external_operation_id": "external-operation-1",
        "receipt_id": "receipt-1",
        "evidence": ExecutionEvidence(
            execution_id="execution-1",
            step_id="step-1",
            operation_id="operation-1",
            external_operation_id="external-operation-1",
            receipt_id="receipt-1",
        ),
    }
    values.update(overrides)
    return ExternalOperationRecord(**values)


def test_external_success_is_recorded_without_replaying_the_operation() -> None:
    store = ExternalOperationStore()
    calls: list[dict[str, str | None]] = []

    def query(**identifiers: str | None) -> str:
        calls.append(identifiers)
        return "success"

    service = ReconciliationService(store=store, query=query)
    operation = _operation()

    status = service.reconcile(operation)

    assert status == OperationStatus.SUCCEEDED
    assert store.get(operation.operation_id).status == OperationStatus.SUCCEEDED
    assert calls == [
        {"external_operation_id": "external-operation-1", "receipt_id": None}
    ]


def test_receipt_is_used_when_external_operation_id_is_missing() -> None:
    store = ExternalOperationStore()
    calls: list[dict[str, str | None]] = []

    def query(**identifiers: str | None) -> OperationStatus:
        calls.append(identifiers)
        return OperationStatus.NOT_FOUND

    service = ReconciliationService(store=store, query=query)
    operation = _operation(external_operation_id=None)

    assert service.reconcile(operation) == OperationStatus.NOT_FOUND
    assert calls == [{"external_operation_id": None, "receipt_id": "receipt-1"}]


def test_lookup_failure_and_missing_identity_remain_unknown() -> None:
    store = ExternalOperationStore()

    def query(**_: str | None) -> OperationStatus:
        raise RuntimeError("dependency unavailable")

    service = ReconciliationService(store=store, query=query)
    operation = _operation()
    assert service.reconcile(operation) == OperationStatus.UNKNOWN
    assert store.get(operation.operation_id).status == OperationStatus.UNKNOWN

    no_identity = _operation(
        operation_id="operation-2",
        external_operation_id=None,
        receipt_id=None,
    )
    assert service.reconcile(no_identity) == OperationStatus.UNKNOWN


def test_unknown_response_is_not_treated_as_not_found() -> None:
    store = ExternalOperationStore()
    service = ReconciliationService(
        store=store,
        query=lambda **_: {"status": "unavailable"},
    )

    operation = _operation()
    assert service.reconcile(operation) == OperationStatus.UNKNOWN
