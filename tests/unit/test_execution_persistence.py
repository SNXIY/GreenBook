"""Persistence contract tests using SQLite with the PostgreSQL adapter."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.lease import ExecutionLeaseManager
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.execution.persistence import execution_metadata
from greenbook_agent_core.execution.persistent_stores import (
    PostgresCheckpointStore,
    PostgresExecutionEventStore,
)
from greenbook_agent_core.execution.postgres_repository import PostgresExecutionRepository
from greenbook_agent_core.execution.recovery_service import ExecutionRecoveryService
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.planning.validation import PlanValidator
from greenbook_contracts import SideEffectState

from tests.plan_factory import GoalPlanFactory


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
    plan = GoalPlanFactory(registry).generate_plan(
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


def test_resolved_tool_boundary_survives_repository_restart(engine) -> None:
    repo = PostgresExecutionRepository(engine)
    execution = PlanExecution(
        execution_id="execution-resolved-tool",
        plan_id="plan-resolved-tool",
        task_id="task-resolved-tool",
        steps=[
            StepExecution(
                execution_id="execution-resolved-tool",
                step_id="analyze",
                capability="ANALYZE_PERFORMANCE",
                tool_name="analytics.get_account_summary",
                arguments={"page": 1},
                idempotency_key="task-resolved-tool:analyze",
                execution_mode="QUEUE",
                policy_snapshot={"name": "analytics.get_account_summary"},
                checkpoint_data={"constraints": {"page": 1}},
            )
        ],
    )

    repo.save(execution)
    restarted = PostgresExecutionRepository(engine).find_by_id(
        "execution-resolved-tool"
    )

    assert restarted is not None
    step = restarted.steps[0]
    assert step.tool_name == "analytics.get_account_summary"
    assert step.arguments == {"page": 1}
    assert step.idempotency_key == "task-resolved-tool:analyze"
    assert step.policy_snapshot["name"] == "analytics.get_account_summary"
    assert step.checkpoint_data == {"constraints": {"page": 1}}


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
    from greenbook_agent_core.execution.checkpoint import ExecutionCheckpoint
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
    event_store = PostgresExecutionEventStore(engine)
    state, execution = _execution(repo)
    state._event_store = event_store
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
    event_store.append(
        ExecutionEvent(
            execution_id=execution.execution_id,
            event_type=EventType.STEP_FAILED,
            step_id=second.step_id,
            payload={
                "step_execution_id": second.step_execution_id,
                "error_code": "TIMEOUT",
                "retryable": True,
                "evidence": ExecutionEvidence(
                    request_sent=False,
                    side_effect_state=SideEffectState.NONE,
                ).model_dump(mode="json"),
            },
        )
    )

    recovered_state = ExecutionStateManager(
        PostgresExecutionRepository(engine),
        event_store=PostgresExecutionEventStore(engine),
    )
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
