"""Background retry task consumer for the Runtime scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from .execution_queue import ExecutionQueueProtocol, ExecutionQueueStatus
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
        execution_queue: ExecutionQueueProtocol | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._retry_manager = retry_manager
        self._poll_interval = max(0.0, poll_interval_seconds)
        self._batch_size = max(1, batch_size)
        self._worker_id = worker_id or scheduler.worker_id
        self._execution_queue = execution_queue
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
            queue_message = None
            if self._execution_queue is not None:
                queue_message = self._execution_queue.get_by_execution_id(
                    task.execution_id
                )
                if queue_message is None:
                    logger.error(
                        "Retry task has no execution queue message execution_id=%s",
                        task.execution_id,
                    )
                    self._release(task)
                    continue
                if queue_message.status == ExecutionQueueStatus.CLAIMED:
                    # The original execution has not been acked yet.  Leave
                    # the retry task ready and let the next poll re-dispatch
                    # after the original queue claim is settled.
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
            # A denied retry returns the step unchanged (still FAILED*): the
            # task must not be recorded as completed — release it so the next
            # poll can apply the retry policy, and its budget eventually
            # terminates it instead of pretending the retry happened (design
            # goal 0813 — a denied retry is never reported as applied).
            step_status = str(getattr(step, "status", "") or "")
            if hasattr(step_status, "value"):
                step_status = str(step_status.value)
            if step_status.upper() != "PENDING":
                logger.warning(
                    "Retry task not applied execution_id=%s step_id=%s status=%s",
                    task.execution_id,
                    task.step_id,
                    step_status,
                )
                self._release(task)
                continue
            if self._execution_queue is not None and queue_message is not None:
                try:
                    queued = self._execution_queue.enqueue(
                        task.execution_id,
                        trace_id=queue_message.trace_id,
                        payload=queue_message.payload,
                        requeue=True,
                    )
                except Exception:
                    logger.exception(
                        "Retry execution requeue failed execution_id=%s",
                        task.execution_id,
                    )
                    self._release(task)
                    continue
                if queued.status != ExecutionQueueStatus.READY:
                    logger.warning(
                        "Retry execution was not made ready execution_id=%s status=%s",
                        task.execution_id,
                        queued.status,
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
                except TimeoutError:
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
