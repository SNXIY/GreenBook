"""Phase 10-G external operation tracking tests."""

from __future__ import annotations

from typing import Any

from greenbook_assistant_core.execution.evidence import ExecutionEvidence
from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationStore,
    ExternalOperationTracker,
    OperationStatus,
)
from greenbook_contracts import SideEffectState, ToolResult, normalize_external_failure


def _evidence(**overrides: Any) -> ExecutionEvidence:
    values: dict[str, Any] = {
        "execution_id": "execution-1",
        "step_id": "step-1",
        "invocation_id": "invocation-1",
        "operation_id": "operation-1",
        "request_sent": True,
        "side_effect_state": SideEffectState.POSSIBLE,
        "external_operation_id": "external-operation-1",
        "receipt_id": "receipt-1",
        "runtime_idempotency_key": "runtime-key",
        "external_idempotency_key": "external-key",
    }
    values.update(overrides)
    return ExecutionEvidence(**values)


def _failure(evidence: ExecutionEvidence):
    return normalize_external_failure(
        ToolResult(
            ok=False,
            code="DEPENDENCY_UNAVAILABLE",
            retryable=True,
            request_sent=evidence.request_sent,
            state={"side_effect_state": evidence.side_effect_state.value},
        ),
        evidence=evidence,
    )


def test_possible_side_effect_creates_unknown_operation_record() -> None:
    store = ExternalOperationStore()
    tracker = ExternalOperationTracker(store)
    evidence = _evidence()

    record = tracker.observe_failure(
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.publish",
        evidence=evidence,
        failure=_failure(evidence),
    )

    assert record.status == OperationStatus.UNKNOWN
    assert record.external_operation_id == "external-operation-1"
    assert record.receipt_id == "receipt-1"
    assert record.idempotency_key == "external-key"
    assert store.count() == 1


def test_operation_identity_is_stable_and_updates_idempotently() -> None:
    store = ExternalOperationStore()
    tracker = ExternalOperationTracker(store)
    first = _evidence(
        operation_id=None,
        external_operation_id=None,
        receipt_id=None,
    )
    second = first.model_copy(update={"side_effect_state": SideEffectState.UNKNOWN})

    first_record = tracker.observe_failure(
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.publish",
        evidence=first,
        failure=_failure(first),
    )
    second_record = tracker.observe_failure(
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.publish",
        evidence=second,
        failure=_failure(second),
    )

    assert first_record.operation_id == second_record.operation_id
    assert store.count() == 1
    assert store.get(first_record.operation_id).status == OperationStatus.UNKNOWN


def test_success_observation_closes_unknown_operation() -> None:
    store = ExternalOperationStore()
    tracker = ExternalOperationTracker(store)
    evidence = _evidence()
    tracker.observe_failure(
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.publish",
        evidence=evidence,
        failure=_failure(evidence),
    )

    success = tracker.observe_success(
        execution_id="execution-1",
        step_id="step-1",
        tool_name="content.publish",
        evidence=evidence,
    )

    assert success.status == OperationStatus.SUCCEEDED
    assert store.find(receipt_id="receipt-1").status == OperationStatus.SUCCEEDED
