"""Phase 11-B background retry worker tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from greenbook_assistant_core.execution.retry_scheduler import RetryScheduler
from greenbook_assistant_core.execution.retry_task import RetryTask, RetryTaskStatus
from greenbook_assistant_core.execution.retry_worker import RetryBackgroundWorker


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _task() -> RetryTask:
    return RetryTask(
        execution_id="execution-11b",
        step_id="step-1",
        attempt=1,
        next_retry_time=NOW,
        backoff=0,
        reason="safe retry",
    )


@pytest.mark.asyncio
async def test_background_worker_calls_retry_manager_and_completes_task() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW, worker_id="worker-1")
    task = scheduler.schedule(_task())
    calls: list[dict[str, object]] = []

    class RetryManager:
        def retry_step(self, execution_id: str, step_id: str, **kwargs):
            calls.append({"execution_id": execution_id, "step_id": step_id, **kwargs})
            return SimpleNamespace(status="PENDING")

    worker = RetryBackgroundWorker(
        scheduler=scheduler,
        retry_manager=RetryManager(),
        worker_id="worker-1",
    )
    results = await worker.run_once(now=NOW)

    assert results[0][0].task_id == task.task_id
    assert results[0][1].status == "PENDING"
    assert calls[0]["source"] == "retry_background_worker"
    assert scheduler.task_store.get(task.task_id).status == RetryTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_background_worker_releases_task_when_retry_manager_fails() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW, worker_id="worker-1")
    task = scheduler.schedule(_task())

    class RetryManager:
        def retry_step(self, *_args, **_kwargs):
            raise RuntimeError("transient worker failure")

    worker = RetryBackgroundWorker(
        scheduler=scheduler,
        retry_manager=RetryManager(),
        worker_id="worker-1",
    )
    assert await worker.run_once(now=NOW) == []
    restored = scheduler.task_store.get(task.task_id)
    assert restored.status == RetryTaskStatus.READY
    assert scheduler.count() == 1


@pytest.mark.asyncio
async def test_shutdown_releases_claimed_task_without_executing_it() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW, worker_id="worker-1")
    task = scheduler.schedule(_task())

    class RetryManager:
        def retry_step(self, *_args, **_kwargs):
            raise AssertionError("shutdown must not execute a retry")

    worker = RetryBackgroundWorker(
        scheduler=scheduler,
        retry_manager=RetryManager(),
        worker_id="worker-1",
    )
    worker.request_shutdown()
    assert await worker.run_once(now=NOW) == []
    assert scheduler.task_store.get(task.task_id).status == RetryTaskStatus.READY
