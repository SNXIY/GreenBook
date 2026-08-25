"""Phase 11-B durable retry-task store tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from greenbook_agent_core.execution.persistence import execution_metadata
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.retry_task import RetryTask, RetryTaskStatus
from greenbook_agent_core.execution.retry_task_store import PostgresRetryTaskStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    db = sa.create_engine("sqlite+pysqlite:///:memory:")
    execution_metadata.create_all(db)
    try:
        yield db
    finally:
        db.dispose()


def _task() -> RetryTask:
    return RetryTask(
        execution_id="execution-11b",
        step_id="step-1",
        attempt=1,
        next_retry_time=NOW,
        backoff=0,
        reason="safe retry",
        retry_budget=1,
        max_attempts=1,
    )


def test_retry_task_survives_store_recreation_and_claims_once(engine) -> None:
    first_store = PostgresRetryTaskStore(engine)
    task = first_store.create(_task())

    restarted_store = PostgresRetryTaskStore(engine)
    scheduler = RetryScheduler(
        now_factory=lambda: NOW,
        task_store=restarted_store,
        worker_id="worker-b",
    )
    claimed = scheduler.due()

    assert len(claimed) == 1
    assert claimed[0].task_id == task.task_id
    assert restarted_store.get(task.task_id).status == RetryTaskStatus.CLAIMED


def test_expired_claim_is_recovered_after_worker_restart(engine) -> None:
    store = PostgresRetryTaskStore(engine)
    task = store.create(_task())
    first_claim = store.claim_due(
        NOW,
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert first_claim[0].task_id == task.task_id

    recovered = PostgresRetryTaskStore(engine).claim_due(
        NOW + timedelta(seconds=31),
        worker_id="worker-b",
        lease_seconds=30,
    )
    assert recovered[0].task_id == task.task_id
    assert recovered[0].claimed_by == "worker-b"


def test_release_consumes_budget_and_task_eventually_cancels(engine) -> None:
    """An always-undispatchable retry task must terminate: every release
    consumes one unit of retry budget and the zero-budget guard cancels the
    task instead of looping forever (design goal 0813)."""
    store = PostgresRetryTaskStore(engine)
    task = store.create(_task())  # retry_budget=1

    # First claim -> release (retry not applied): budget 1 -> 0.
    claimed = store.claim_due(NOW, worker_id="worker-a", lease_seconds=30)
    assert claimed[0].task_id == task.task_id
    released = store.release(claimed[0].task_id, worker_id="worker-a")
    assert released is not None
    assert released.retry_budget == 0

    # Next poll: the zero-budget guard cancels the task instead of re-claiming.
    again = store.claim_due(NOW, worker_id="worker-a", lease_seconds=30)
    assert again == []
    cancelled = store.get(task.task_id)
    assert cancelled is not None
    assert cancelled.status == RetryTaskStatus.CANCELLED
