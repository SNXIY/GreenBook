"""Background worker loop for asynchronous moderation execution."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from core import settings
from moderation.schemas import ModerationTaskAccepted, ModerationTaskDetail, ModerationTaskStatus

logger = logging.getLogger(__name__)

CommunityResultApplier = Callable[[ModerationTaskDetail], Awaitable[None]]


class ModerationJobRunner(Protocol):
    async def claim_next_task(self, *, worker_id: str) -> UUID | None: ...

    async def process_task(self, task_id: UUID) -> ModerationTaskAccepted: ...


class ModerationWorkerLoop:
    """Poll the DB-backed job queue, claim tasks, and run the moderation graph."""

    def __init__(
        self,
        workflow: ModerationJobRunner,
        *,
        worker_id: str | None = None,
        poll_interval_ms: int | None = None,
        concurrency: int | None = None,
        apply_community_result: CommunityResultApplier | None = None,
    ) -> None:
        self.workflow = workflow
        self.worker_id = worker_id or settings.MODERATION_WORKER_ID or _default_worker_id()
        self.poll_interval_ms = (
            settings.MODERATION_WORKER_POLL_INTERVAL_MS
            if poll_interval_ms is None
            else poll_interval_ms
        )
        self.concurrency = (
            settings.MODERATION_WORKER_CONCURRENCY if concurrency is None else concurrency
        )
        self.apply_community_result = apply_community_result
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_forever(self) -> None:
        logger.info(
            "Moderation worker %s started (poll=%sms concurrency=%s)",
            self.worker_id,
            self.poll_interval_ms,
            self.concurrency,
        )
        semaphore = asyncio.Semaphore(self.concurrency)
        in_flight: set[asyncio.Task[None]] = set()
        try:
            while not self._stop.is_set():
                claimed = False
                while len(in_flight) < self.concurrency and not self._stop.is_set():
                    task_id = await self.workflow.claim_next_task(worker_id=self.worker_id)
                    if task_id is None:
                        break
                    claimed = True
                    job = asyncio.create_task(
                        self._run_claimed(semaphore, task_id),
                        name=f"moderation-job-{task_id}",
                    )
                    in_flight.add(job)
                    job.add_done_callback(in_flight.discard)

                if not claimed:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.poll_interval_ms / 1000,
                        )
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
        finally:
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            logger.info("Moderation worker %s stopped", self.worker_id)

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever(), name=f"moderation-worker-{self.worker_id}")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_claimed(self, semaphore: asyncio.Semaphore, task_id: UUID) -> None:
        async with semaphore:
            try:
                accepted = await self.workflow.process_task(task_id)
                if (
                    self.apply_community_result is not None
                    and accepted.task.status
                    in {
                        ModerationTaskStatus.COMPLETED,
                        ModerationTaskStatus.WAITING_REVIEW,
                        ModerationTaskStatus.FAILED,
                    }
                ):
                    await self.apply_community_result(accepted.task)
            except Exception:
                logger.exception("Moderation worker failed while processing task %s", task_id)


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
