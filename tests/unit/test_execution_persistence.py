"""Persistence contract tests using SQLite with the PostgreSQL adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.lease import ExecutionLeaseManager
from greenbook_assistant_core.execution.models import ExecutionStatus, StepStatus
from greenbook_assistant_core.execution.persistence import execution_metadata
from greenbook_assistant_core.execution.persistent_stores import (
    PostgresCheckpointStore,
    PostgresExecutionEventStore,
)
from greenbook_assistant_core.execution.postgres_repository import PostgresExecutionRepository
from greenbook_assistant_core.execution.recovery_service import ExecutionRecoveryService
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


@pytest.fixture
def engine():
    db = sa.create_engine("sqlite+pysqlite:///:memory:")
    execution_metadata.create_all(db)
    try:
        yield db
    finally:
        db.dispose()


def _execution(repo):
    registry = CapabilityRegistry()
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="persist-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "SEARCH"}, {"type": "ANALYZE"}, {"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    state = ExecutionStateManager(repo)
    execution = state.init_execution(plan, executable)
    return state, execution


def test_save_and_read_execution_and_step(engine) -> None:
    repo = PostgresExecutionRepository(engine)
    state, execution = _execution(repo)
    state.start_execution(execution.execution_id)
    step = state.start_step(execution.execution_id, execution.steps[0].step_execution_id)
    state.fail_step(
        execution.execution_id,
        step.step_execution_id,
        error_code="TIMEOUT",
        error_message="temporary outage",
    )

    restarted = PostgresExecutionRepository(engine).find_by_id(execution.execution_id)
    assert restarted is not None
    assert restarted.status == ExecutionStatus.RUNNING
    assert restarted.steps[0].status == StepStatus.FAILED_RETRYABLE
    assert restarted.steps[0].retry_count == 1
    assert restarted.steps[0].error_code == "TIMEOUT"


def test_event_and_checkpoint_survive_store_recreation(engine) -> None:
    event_store = PostgresExecutionEventStore(engine)
    event = ExecutionEvent(
        execution_id="execution-1",
        event_type=EventType.STEP_FAILED,
        step_id="search",
        payload={"error_code": "TIMEOUT"},
    )
    event_store.append(event)
    assert PostgresExecutionEventStore(engine).list_events("execution-1")[0].event_id == event.event_id

    checkpoint_store = PostgresCheckpointStore(engine)
    from greenbook_assistant_core.execution.checkpoint import ExecutionCheckpoint
    checkpoint_store.save(ExecutionCheckpoint(
        execution_id="execution-1",
        completed_steps=["search"],
        current_step="analyze",
        snapshot={"draft_id": "d1"},
    ))
    restored = PostgresCheckpointStore(engine).latest("execution-1")
    assert restored is not None
    assert restored.completed_steps == ["search"]
    assert restored.current_step == "analyze"
    assert restored.snapshot == {"draft_id": "d1"}


def test_recovery_service_preserves_completed_steps_after_restart(engine) -> None:
    repo = PostgresExecutionRepository(engine)
    state, execution = _execution(repo)
    state.start_execution(execution.execution_id)
    first, second = execution.steps[:2]
    state.start_step(execution.execution_id, first.step_execution_id)
    state.complete_step(execution.execution_id, first.step_execution_id)
    state.start_step(execution.execution_id, second.step_execution_id)
    state.fail_step(
        execution.execution_id,
        second.step_execution_id,
        error_code="TIMEOUT",
    )

    recovered_state = ExecutionStateManager(PostgresExecutionRepository(engine))
    recovered = ExecutionRecoveryService(recovered_state).restore_execution(
        execution.execution_id
    )
    assert recovered.steps[0].status == StepStatus.COMPLETED
    assert recovered.steps[1].status == StepStatus.PENDING
    assert recovered.steps[1].retry_count == 1


def test_lease_prevents_duplicate_workers() -> None:
    leases = ExecutionLeaseManager()
    assert leases.acquire("execution-1", "worker-a") is True
    assert leases.acquire("execution-1", "worker-b") is False
    assert leases.renew("execution-1", "worker-b") is False
    assert leases.renew("execution-1", "worker-a") is True
    assert leases.release("execution-1", "worker-b") is False
    assert leases.release("execution-1", "worker-a") is True
    assert leases.acquire("execution-1", "worker-b") is True
