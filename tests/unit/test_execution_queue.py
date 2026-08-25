"""Phase 12-B Execution Queue contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from greenbook_agent_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueMessage,
    ExecutionQueueStatus,
    PostgresExecutionQueue,
)
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.execution.lease import ExecutionLeaseManager

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_memory_queue_claim_ack_is_idempotent() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    first = queue.enqueue("execution-12b", trace_id="trace-12b")
    duplicate = queue.enqueue("execution-12b", trace_id="other-trace")

    assert duplicate.message_id == first.message_id
    claimed = queue.claim(NOW, worker_id="worker-a", lease_seconds=30)
    assert claimed[0].attempt == 1
    assert claimed[0].status == ExecutionQueueStatus.CLAIMED
    assert queue.ack(first.message_id, worker_id="worker-b") is None
    acked = queue.ack(first.message_id, worker_id="worker-a")
    assert acked is not None
    assert acked.status == ExecutionQueueStatus.ACKED


def test_memory_queue_release_reclaims_after_worker_shutdown() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-release")
    queue.claim(NOW, worker_id="worker-a", lease_seconds=30)

    released = queue.release(message.message_id, worker_id="worker-a")
    assert released is not None
    assert released.status == ExecutionQueueStatus.READY
    reclaimed = queue.claim(NOW, worker_id="worker-b", lease_seconds=30)
    assert reclaimed[0].execution_id == "execution-release"
    assert reclaimed[0].attempt == 2


def test_memory_queue_requeue_reopens_acknowledged_execution() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-retry")
    claimed = queue.claim(NOW, worker_id="worker-a", lease_seconds=30)
    assert queue.ack(claimed[0].message_id, worker_id="worker-a") is not None

    requeued = queue.enqueue("execution-retry", requeue=True)

    assert requeued.message_id == message.message_id
    assert requeued.status == ExecutionQueueStatus.READY
    assert queue.claim(NOW, worker_id="worker-b", lease_seconds=30)[0].attempt == 2


def test_postgres_queue_survives_store_recreation() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    queue = PostgresExecutionQueue(engine)
    message = queue.enqueue(
        ExecutionQueueMessage(
            execution_id="execution-persistent",
            trace_id="trace-persistent",
            payload={"safe": "dispatch metadata"},
        )
    )

    restarted = PostgresExecutionQueue(engine)
    found = restarted.get_by_execution_id("execution-persistent")
    assert found is not None
    assert found.message_id == message.message_id
    assert found.payload == {"safe": "dispatch metadata"}


@pytest.mark.asyncio
async def test_queue_worker_delegates_and_acks_without_business_logic() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-worker", trace_id="trace-worker")
    handled: list[str] = []

    async def handler(item: ExecutionQueueMessage) -> None:
        handled.append(item.execution_id)

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-queue",
    )
    results = await worker.run_once(now=NOW)

    assert handled == ["execution-worker"]
    assert results[0].message_id == message.message_id
    assert queue.get(message.message_id).status == ExecutionQueueStatus.ACKED


@pytest.mark.asyncio
async def test_queue_worker_fails_message_when_handler_raises() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-failed")

    async def handler(_item: ExecutionQueueMessage) -> None:
        raise RuntimeError("handler unavailable")

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-queue",
    )
    assert await worker.run_once(now=NOW) == []
    failed = queue.get(message.message_id)
    assert failed is not None
    assert failed.status == ExecutionQueueStatus.FAILED
    assert failed.last_error == "handler unavailable"


@pytest.mark.asyncio
async def test_queue_worker_acquires_and_releases_execution_lease() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-leased")
    leases = ExecutionLeaseManager()

    async def handler(_item: ExecutionQueueMessage) -> None:
        assert leases.get(message.execution_id) is not None

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-lease",
        lease_manager=leases,
    )

    await worker.run_once(now=NOW)

    assert leases.get(message.execution_id) is None


@pytest.mark.asyncio
async def test_queue_worker_releases_execution_lease_on_handler_failure() -> None:
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue("execution-leased-failure")
    leases = ExecutionLeaseManager()

    async def handler(_item: ExecutionQueueMessage) -> None:
        raise RuntimeError("handler unavailable")

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-lease",
        lease_manager=leases,
    )

    await worker.run_once(now=NOW)

    assert leases.get(message.execution_id) is None


@pytest.mark.asyncio
async def test_conflict_deferral_uses_backoff_and_does_not_busy_loop() -> None:
    """A message deferred by a resource conflict must be re-scheduled with a
    forward availability (backoff), not released for immediate re-claim
    (design goal 0813 — no busy-loop / starvation)."""
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue(
        "execution-conflicted",
        payload={"task_id": "t-conflict", "draft_id": "draft-1"},
    )

    def resource_provider():
        # Simulate an always-present conflicting execution on the same draft.
        return [{
            "execution_id": "other", "task_id": "t-other",
            "goal_id": "", "status": "RUNNING",
            "draft_id": "draft-1",
            "policy_snapshot": {
                "side_effect": {"has_side_effect": True, "access_mode": "WRITE"},
            },
        }]

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=lambda _item: None,
        worker_id="worker-conflict",
        resource_access_provider=resource_provider,
    )

    await worker.run_once(now=NOW)

    deferred = queue.get(message.message_id)
    assert deferred is not None
    assert deferred.status == ExecutionQueueStatus.READY
    # The message must not be immediately reclaimable: availability is pushed
    # forward past the claim time.
    assert deferred.available_at > NOW.isoformat()
    assert deferred.attempt >= 1


@pytest.mark.asyncio
async def test_conflict_deferral_fails_after_attempt_cap() -> None:
    """An always-conflicting message must eventually dead-letter instead of
    spinning forever."""
    queue = ExecutionQueue(now_factory=lambda: NOW)
    message = queue.enqueue(
        "execution-starved",
        payload={"task_id": "t-starved", "draft_id": "draft-9"},
    )

    def resource_provider():
        return [{
            "execution_id": "other", "task_id": "t-other",
            "goal_id": "", "status": "RUNNING",
            "draft_id": "draft-9",
            "policy_snapshot": {
                "side_effect": {"has_side_effect": True, "access_mode": "WRITE"},
            },
        }]

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=lambda _item: None,
        worker_id="worker-starved",
        resource_access_provider=resource_provider,
    )
    # A single conflict is enough to cross the cap: the message dead-letters
    # instead of being released for another (starving) claim.
    worker._max_deferrals = 1

    await worker.run_once(now=NOW)

    failed = queue.get(message.message_id)
    assert failed is not None
    assert failed.status == ExecutionQueueStatus.FAILED
    assert "deferred too many times" in failed.last_error


class _RetryableRuntimeError(RuntimeError):
    """An exception explicitly marked retryable by its raising boundary."""

    retryable = True


class _FakeConnectError(Exception):
    """Stand-in for httpx.ConnectError when httpx is not installed."""


_FakeConnectError.__module__ = "httpx"


@pytest.mark.asyncio
async def test_transient_handler_error_defers_message_instead_of_failing() -> None:
    """A transient handler failure (network/timeout/retryable) must be
    re-scheduled with backoff, never dead-lettered: a healthy dependency could
    complete the work moments later and a permanent FAIL would silently drop
    user work (design goal 0813 — the queue recovers, it does not lose work)."""
    queue = ExecutionQueue(now_factory=lambda: NOW)

    async def handler(_item: ExecutionQueueMessage) -> None:
        raise _FakeConnectError("upstream unreachable")

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-transient",
    )
    message = queue.enqueue("execution-transient")

    assert await worker.run_once(now=NOW) == []
    deferred = queue.get(message.message_id)
    assert deferred is not None
    assert deferred.status == ExecutionQueueStatus.READY
    # Availability is pushed forward (backoff), not immediately reclaimable.
    assert deferred.available_at > NOW.isoformat()
    assert deferred.attempt >= 1
    assert deferred.last_error is None or deferred.last_error == ""


@pytest.mark.asyncio
async def test_retryable_flag_and_cause_chain_defers_message() -> None:
    """Both an explicit ``retryable=True`` attribute and a wrapped
    (__cause__-chained) timeout must count as transient."""
    queue = ExecutionQueue(now_factory=lambda: NOW)

    async def handler(_item: ExecutionQueueMessage) -> None:
        try:
            raise TimeoutError("mcp call timed out")
        except TimeoutError as cause:
            raise _RetryableRuntimeError("wrapped upstream timeout") from cause

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="worker-transient-2",
    )
    message = queue.enqueue("execution-transient-chain")

    await worker.run_once(now=NOW)

    deferred = queue.get(message.message_id)
    assert deferred is not None
    assert deferred.status == ExecutionQueueStatus.READY
    assert deferred.available_at > NOW.isoformat()
