"""Background retry task consumer for the Runtime scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from .retry_scheduler import RetryScheduler
from .retry_task import RetryTask

if TYPE_CHECKING:
    from .models import StepExecution
    from .retry_manager import RetryManager


logger = logging.getLogger(__name__)


class RetryBackgroundWorker:
    """Poll due tasks and hand them to RetryManager.

    The worker owns polling, claim leases and shutdown. It never invokes a
    capability or tool directly; RetryManager remains the single retry
    decision/state boundary.
    """

    def __init__(
        self,
        *,
        scheduler: RetryScheduler,
        retry_manager: RetryManager,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 20,
        worker_id: str | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._retry_manager = retry_manager
        self._poll_interval = max(0.0, poll_interval_seconds)
        self._batch_size = max(1, batch_size)
        self._worker_id = worker_id or scheduler.worker_id
        self._stop = asyncio.Event()
        self._claimed: set[str] = set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> list[tuple[RetryTask, StepExecution]]:
        """Claim one batch, applying policy through RetryManager."""

        tasks = self._scheduler.due(
            now,
            worker_id=self._worker_id,
            limit=self._batch_size,
        )
        results: list[tuple[RetryTask, StepExecution]] = []
        for task in tasks:
            self._claimed.add(task.task_id)
            if self.stopped:
                self._release(task)
                continue
            try:
                step = self._retry_manager.retry_step(
                    task.execution_id,
                    task.step_id,
                    source="retry_background_worker",
                    user_requested_retry=False,
                )
            except Exception:
                logger.exception(
                    "Retry task failed before completion task_id=%s",
                    task.task_id,
                )
                self._release(task)
                continue
            self._scheduler.task_store.complete(
                task.task_id,
                worker_id=self._worker_id,
            )
            self._claimed.discard(task.task_id)
            results.append((task, step))
        return results

    async def run(self) -> None:
        """Run until shutdown is requested or the task is cancelled."""

        try:
            while not self.stopped:
                await self.run_once()
                if self.stopped:
                    break
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._poll_interval,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            self.request_shutdown()
            raise
        finally:
            await self.shutdown()

    def request_shutdown(self) -> None:
        """Signal the loop to stop after the current safe boundary."""

        self._stop.set()

    async def shutdown(self) -> None:
        """Stop polling and release claims not completed by this worker."""

        self.request_shutdown()
        for task_id in list(self._claimed):
            task = self._scheduler.task_store.get(task_id)
            if task is not None:
                self._scheduler.task_store.release(
                    task_id,
                    worker_id=self._worker_id,
                )
            self._claimed.discard(task_id)

    def _release(self, task: RetryTask) -> None:
        self._scheduler.task_store.release(
            task.task_id,
            worker_id=self._worker_id,
        )
        self._claimed.discard(task.task_id)


__all__ = ["RetryBackgroundWorker"]
