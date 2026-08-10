"""Phase 12-B Execution Queue contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
import pytest

from greenbook_assistant_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueMessage,
    ExecutionQueueStatus,
    PostgresExecutionQueue,
)
from greenbook_assistant_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_assistant_core.execution.lease import ExecutionLeaseManager


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
