from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from datetime import datetime, timezone
from typing import Protocol

from app.creator.application.harness import CreatorAgentHarness
from app.creator.domain.models import CreatorRunStatus


logger = logging.getLogger("uvicorn.error")


class CreatorRunDispatcher(Protocol):
    execution_mode: str

    def schedule_run(self, run_id: str) -> bool: ...

    async def recover(self, run_ids: tuple[str, ...]) -> int: ...

    async def aclose(self) -> None: ...


class CreatorOutboxRunDispatcher:
    """Command-only API adapter; the external worker consumes the Outbox."""

    execution_mode = "outbox-worker"

    def schedule_run(self, run_id: str) -> bool:
        return False

    async def recover(self, run_ids: tuple[str, ...]) -> int:
        return 0

    async def aclose(self) -> None:
        return None


class CreatorLocalRunDispatcher:
    """Bounded API-process adapter for driving durable Creator runs.

    Global concurrency caps process load; per-tenant concurrency keeps one
    noisy tenant from starving others on the same API worker.
    """

    execution_mode = "local-durable-dispatcher"

    def __init__(
        self,
        harness: CreatorAgentHarness,
        *,
        worker_prefix: str = "creator-api",
        concurrency: int = 2,
        tenant_concurrency: int = 1,
        user_concurrency: int = 1,
        retry_delay_seconds: float = 1.0,
        shutdown_grace_seconds: float = 10.0,
    ) -> None:
        self._harness = harness
        self._worker_prefix = worker_prefix
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._tenant_concurrency = max(1, tenant_concurrency)
        self._tenant_semaphores: dict[str, asyncio.Semaphore] = {}
        self._user_concurrency = max(1, user_concurrency)
        self._user_semaphores: dict[str, asyncio.Semaphore] = {}
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._shutdown_grace_seconds = max(0.0, shutdown_grace_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self.dispatcher_instance_id = f"{worker_prefix}:{uuid.uuid4().hex[:12]}"
        self.dispatcher_started_at = datetime.now(timezone.utc)
        self.dispatcher_last_heartbeat = self.dispatcher_started_at
        self.dispatcher_last_claim_at: datetime | None = None
        self.dispatcher_last_error: str | None = None
        self.dispatcher_loop_iteration = 0
        self.dispatcher_stopped_at: datetime | None = None
        logger.info(
            "Creator dispatcher started instance_id=%s started_at=%s",
            self.dispatcher_instance_id,
            self.dispatcher_started_at.isoformat(),
        )

    def schedule_run(self, run_id: str) -> bool:
        if self._closing or run_id in self._tasks:
            return False
        task = asyncio.create_task(
            self._drive_run(run_id),
            name=f"creator-run:{run_id}",
        )
        self._tasks[run_id] = task
        logger.info(
            "Creator dispatcher task_seen instance_id=%s run_id=%s active=%s",
            self.dispatcher_instance_id,
            run_id,
            len(self._tasks),
        )
        task.add_done_callback(lambda done: self._task_done(run_id, done))
        return True

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self.dispatcher_last_heartbeat = datetime.now(timezone.utc)
        if task.cancelled():
            logger.info(
                "Creator dispatcher task_cancelled instance_id=%s run_id=%s",
                self.dispatcher_instance_id,
                run_id,
            )
            return
        exception = task.exception()
        if exception is not None:
            self.dispatcher_last_error = repr(exception)
            logger.error(
                "Creator dispatcher exception instance_id=%s run_id=%s",
                self.dispatcher_instance_id,
                run_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )
        else:
            logger.info(
                "Creator dispatcher task_execution_finished instance_id=%s run_id=%s",
                self.dispatcher_instance_id,
                run_id,
            )

    def diagnostics(self) -> dict[str, object]:
        self.dispatcher_loop_iteration += 1
        self.dispatcher_last_heartbeat = datetime.now(timezone.utc)
        active_stacks: dict[str, str] = {}
        for run_id, task in self._tasks.items():
            if task.done():
                continue
            try:
                active_stacks[run_id] = "".join(
                    line
                    for frame in task.get_stack(limit=8)
                    for line in traceback.format_stack(frame)
                )
            except Exception as exc:
                active_stacks[run_id] = f"<stack unavailable: {type(exc).__name__}: {exc}>"
        return {
            "dispatcher_instance_id": self.dispatcher_instance_id,
            "dispatcher_started_at": self.dispatcher_started_at.isoformat(),
            "dispatcher_loop_iteration": self.dispatcher_loop_iteration,
            "dispatcher_alive": not self._closing,
            "dispatcher_last_heartbeat": self.dispatcher_last_heartbeat.isoformat(),
            "dispatcher_last_claim_at": (
                self.dispatcher_last_claim_at.isoformat()
                if self.dispatcher_last_claim_at else None
            ),
            "dispatcher_last_error": self.dispatcher_last_error,
            "active_task_count": len(self._tasks),
            "active_task_ids": tuple(self._tasks),
            "active_task_stacks": active_stacks,
        }

    async def recover(self, run_ids: tuple[str, ...]) -> int:
        return sum(1 for run_id in run_ids if self.schedule_run(run_id))

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(
                *tuple(self._tasks.values()),
                return_exceptions=True,
            )

    async def aclose(self) -> None:
        self._closing = True
        self.dispatcher_stopped_at = datetime.now(timezone.utc)
        logger.info(
            "Creator dispatcher stopping instance_id=%s active=%s",
            self.dispatcher_instance_id,
            len(self._tasks),
        )
        pending = tuple(self._tasks.values())
        if not pending:
            return
        done, remaining = await asyncio.wait(
            pending,
            timeout=self._shutdown_grace_seconds,
        )
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except Exception:
                pass
        logger.info(
            "Creator dispatcher stopped instance_id=%s stopped_at=%s",
            self.dispatcher_instance_id,
            self.dispatcher_stopped_at.isoformat(),
        )

    def _tenant_semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        semaphore = self._tenant_semaphores.get(tenant_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._tenant_concurrency)
            self._tenant_semaphores[tenant_id] = semaphore
        return semaphore

    def _user_semaphore(self, creator_id: str) -> asyncio.Semaphore:
        semaphore = self._user_semaphores.get(creator_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._user_concurrency)
            self._user_semaphores[creator_id] = semaphore
        return semaphore

    async def _drive_run(self, run_id: str) -> None:
        worker_id = f"{self._worker_prefix}:{uuid.uuid4().hex}"
        try:
            async with self._semaphore:
                try:
                    tenant_id, creator_id = await self._harness.get_run_scope(run_id)
                except Exception:
                    tenant_id = "unknown"
                    creator_id = f"unknown:{run_id}"
                async with self._tenant_semaphore(tenant_id):
                    async with self._user_semaphore(creator_id):
                        while not self._closing:
                            self.dispatcher_loop_iteration += 1
                            self.dispatcher_last_heartbeat = datetime.now(timezone.utc)
                            logger.info(
                                "Creator dispatcher task_claim_attempt instance_id=%s run_id=%s worker_id=%s attempt=%s",
                                self.dispatcher_instance_id,
                                run_id,
                                worker_id,
                                self.dispatcher_loop_iteration,
                            )
                            result = await self._harness.start_run(
                                run_id,
                                worker_id=worker_id,
                            )
                            self.dispatcher_last_claim_at = datetime.now(timezone.utc)
                            logger.info(
                                "Creator dispatcher task_claimed instance_id=%s run_id=%s status=%s",
                                self.dispatcher_instance_id,
                                run_id,
                                result.run_status.value,
                            )
                            if result.run_status != CreatorRunStatus.RETRYING:
                                return
                            await asyncio.sleep(self._retry_delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.dispatcher_last_error = "dispatcher task exception; see traceback"
            logger.exception(
                "Creator local dispatcher stopped run_id=%s worker_id=%s",
                run_id,
                worker_id,
            )
