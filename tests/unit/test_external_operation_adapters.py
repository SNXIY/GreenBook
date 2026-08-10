"""Phase 11-D external operation adapter contract tests."""

from __future__ import annotations

from greenbook_assistant_core.execution.external_adapters import (
    CreatorAdapter,
    JavaCommunityAdapter,
    MockExternalOperationAdapter,
)
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_assistant_core.execution.reconciliation import ReconciliationService


def _operation() -> ExternalOperationRecord:
    return ExternalOperationRecord(
        operation_id="operation-adapter-1",
        execution_id="execution-adapter-1",
        step_id="step-1",
        tool_name="content.publish",
        external_operation_id="external-adapter-1",
        receipt_id="receipt-adapter-1",
        status=OperationStatus.UNKNOWN,
    )


def test_mock_adapter_is_consumed_by_reconciliation_service() -> None:
    adapter = MockExternalOperationAdapter(
        {"external:external-adapter-1": OperationStatus.SUCCEEDED}
    )
    result = ReconciliationService(
        store=ExternalOperationStore(),
        adapter=adapter,
    ).reconcile_result(_operation())

    assert result.status == OperationStatus.SUCCEEDED
    assert adapter.calls == [
        {
            "external_operation_id": "external-adapter-1",
            "receipt_id": None,
        }
    ]


def test_creator_and_java_adapters_forward_query_identity() -> None:
    creator_calls: list[dict[str, str | None]] = []
    java_calls: list[dict[str, str | None]] = []

    creator = CreatorAdapter(
        lambda **identifiers: creator_calls.append(identifiers) or "PROCESSING"
    )
    java = JavaCommunityAdapter(
        lambda **identifiers: java_calls.append(identifiers) or "FAILED"
    )

    assert creator.query_operation_status(external_operation_id="creator-1") == "PROCESSING"
    assert java.query_operation_status(receipt_id="java-receipt-1") == "FAILED"
    assert creator_calls == [
        {"external_operation_id": "creator-1", "receipt_id": None}
    ]
    assert java_calls == [
        {"external_operation_id": None, "receipt_id": "java-receipt-1"}
    ]
