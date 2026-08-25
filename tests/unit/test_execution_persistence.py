"""Persistence contract tests using SQLite with the PostgreSQL adapter."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
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
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.planning.validation import PlanValidator

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
                goal_id="goal-performance",
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
    assert step.goal_id == "goal-performance"
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
    # ``ExecutionRecoveryService`` was removed in Phase 4 — the durable
    # execution row + step CAS transitions already preserve completed steps
    # across a process restart without a separate recovery pass (see
    # ``test_save_and_read_execution_and_step``).
    repo = PostgresExecutionRepository(engine)
    state, execution = _execution(repo)
    state.start_execution(execution.execution_id)
    first, second = execution.steps[:2]
    state.start_step(execution.execution_id, first.step_execution_id)
    state.complete_step(execution.execution_id, first.step_execution_id)
    state.start_step(execution.execution_id, second.step_execution_id)

    restarted = PostgresExecutionRepository(engine).find_by_id(execution.execution_id)
    assert restarted is not None
    assert restarted.steps[0].status == StepStatus.COMPLETED
    assert restarted.steps[1].status == StepStatus.RUNNING


def test_lease_prevents_duplicate_workers() -> None:
    leases = ExecutionLeaseManager()
    assert leases.acquire("execution-1", "worker-a") is True
    assert leases.acquire("execution-1", "worker-b") is False
    assert leases.renew("execution-1", "worker-b") is False
    assert leases.renew("execution-1", "worker-a") is True
    assert leases.release("execution-1", "worker-b") is False
    assert leases.release("execution-1", "worker-a") is True
    assert leases.acquire("execution-1", "worker-b") is True


def test_lease_renew_keeps_owner_while_handler_runs(engine) -> None:
    """A long-running handler must keep renewing its execution lease so a
    second worker cannot claim the same execution mid-flight (design goal
    0813 — a tool longer than the lease TTL must never be double-executed)."""
    import asyncio

    from greenbook_agent_core.execution.execution_queue import ExecutionQueue
    from greenbook_agent_core.execution.execution_queue_worker import (
        ExecutionQueueWorker,
    )

    class _RecordingLeaseManager:
        def __init__(self) -> None:
            self.renews = 0
            self.acquires = 0

        def acquire(self, execution_id, worker_id, ttl_seconds=30) -> bool:
            self.acquires += 1
            return True

        def renew(self, execution_id, worker_id, ttl_seconds=30) -> bool:
            self.renews += 1
            return True

        def release(self, execution_id, worker_id) -> bool:
            return True

    queue = ExecutionQueue()
    queue.enqueue("execution-heartbeat", payload={"task_id": "t1"})
    leases = _RecordingLeaseManager()
    worker = ExecutionQueueWorker(
        queue=queue,
        # Handler runs 1.5s; with a 0.5s heartbeat interval the worker must
        # renew the lease while the handler is still running.
        execution_handler=lambda _message: asyncio.sleep(1.5),
        worker_id="heartbeat-worker",
        lease_seconds=1,
        poll_interval_seconds=0.01,
        batch_size=1,
        max_concurrency=1,
        lease_manager=leases,
    )

    async def run_and_probe() -> None:
        await worker.run_once()

    asyncio.run(run_and_probe())
    assert leases.acquires >= 1, "the worker must acquire the execution lease"
    assert leases.renews >= 1, (
        "the worker must renew the lease while the handler runs, otherwise a "
        "long tool lets a second worker claim the same execution"
    )


def test_step_start_cas_rejects_concurrent_claim(engine) -> None:
    """Two workers racing to start the same PENDING step: only one may win.

    ``start_step`` uses a status-CAS (UPDATE ... WHERE status='PENDING'); the
    loser must observe the conflict instead of running the step twice
    (design goal 0813 — no double side effects)."""
    from greenbook_agent_core.execution.state_manager import (
        ExecutionStateManager,
        _StepTransitionConflictError,
    )

    repository = PostgresExecutionRepository(engine)
    state = ExecutionStateManager(repository)
    execution = PlanExecution(
        plan_id="cas-plan",
        task_id="cas-task",
        steps=[StepExecution(step_id="generate", capability="GENERATE_CONTENT", ordinal=1)],
    )
    execution.steps[0].execution_id = execution.execution_id
    repository.save(execution)
    sid = execution.steps[0].step_execution_id

    # First worker claims the step.
    state.start_step(execution.execution_id, sid)
    assert repository.find_by_id(execution.execution_id).steps[0].status == StepStatus.RUNNING

    # Second worker attempts the same claim — either the read-check or the
    # status-CAS must reject it; the step must stay RUNNING (not double-run).
    with pytest.raises((_StepTransitionConflictError, ValueError)):
        state.start_step(execution.execution_id, sid)
    assert repository.find_by_id(execution.execution_id).steps[0].status == StepStatus.RUNNING
