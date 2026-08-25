"""Phase 11-A durable external operation store tests."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationRecord,
    OperationStatus,
)
from greenbook_agent_core.execution.persistence import execution_metadata
from greenbook_agent_core.execution.persistent_stores import (
    PostgresExternalOperationStore,
)


@pytest.fixture
def engine():
    db = sa.create_engine("sqlite+pysqlite:///:memory:")
    execution_metadata.create_all(db)
    try:
        yield db
    finally:
        db.dispose()


def _record(**overrides: Any) -> ExternalOperationRecord:
    values: dict[str, Any] = {
        "operation_id": "operation-11a",
        "execution_id": "execution-11a",
        "step_id": "step-1",
        "tool_name": "content.publish",
        "status": OperationStatus.UNKNOWN,
        "external_operation_id": "creator-operation-1",
        "receipt_id": "receipt-1",
        "runtime_idempotency_key": "runtime-key",
        "external_idempotency_key": "external-key",
        "evidence": ExecutionEvidence(
            execution_id="execution-11a",
            step_id="step-1",
            operation_id="operation-11a",
            external_operation_id="creator-operation-1",
            receipt_id="receipt-1",
            request_sent=True,
        ),
    }
    values.update(overrides)
    return ExternalOperationRecord(**values)


def test_external_operation_survives_store_recreation_and_supports_queries(engine) -> None:
    store = PostgresExternalOperationStore(engine)
    record = _record()
    store.create(record)

    restarted = PostgresExternalOperationStore(engine)
    restored = restarted.get(record.operation_id)
    assert restored is not None
    assert restored.evidence is not None
    assert restored.evidence.external_operation_id == "creator-operation-1"
    assert restarted.find_by_execution_id("execution-11a")[0].operation_id == record.operation_id
    assert (
        restarted.find_by_external_operation_id("creator-operation-1").operation_id
        == record.operation_id
    )

    updated = restarted.update_status(record.operation_id, OperationStatus.SUCCEEDED)
    assert updated is not None
    assert updated.status == OperationStatus.SUCCEEDED
    assert PostgresExternalOperationStore(engine).get(record.operation_id).status == OperationStatus.SUCCEEDED
