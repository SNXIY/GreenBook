from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from uuid import UUID

import httpx

from community.providers import CommunityDataProvider
from database import DatabaseManager
from moderation.repositories import (
    ModerationActionLogRepository,
    ModerationCallbackOutboxRepository,
    ModerationTaskRepository,
)
from moderation.schemas import (
    ActionLogEvent,
    DecisionSource,
    ModerationCallbackDeliveryView,
)
from moderation.services.mappers import task_to_detail

logger = logging.getLogger(__name__)


class ModerationCallbackDispatcher:
    def __init__(
        self,
        *,
        database: DatabaseManager,
        provider: CommunityDataProvider,
        worker_id: str | None = None,
        poll_seconds: float = 0.5,
        concurrency: int = 2,
        lease_seconds: float = 30.0,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        self.database = database
        self.provider = provider
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:callback:{uuid.uuid4().hex[:8]}"
        )
        self.poll_seconds = poll_seconds
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.outbox = ModerationCallbackOutboxRepository()
        self.tasks = ModerationTaskRepository()
        self.logs = ModerationActionLogRepository()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self.run_forever(),
                name=f"moderation-callback-{self.worker_id}",
            )
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run_forever(self) -> None:
        logger.info(
            "Moderation callback dispatcher %s started (concurrency=%s)",
            self.worker_id,
            self.concurrency,
        )
        try:
            while not self._stop.is_set():
                self._reap()
                claimed = False
                while len(self._active) < self.concurrency:
                    delivery = await self._claim()
                    if delivery is None:
                        break
                    claimed = True
                    task = asyncio.create_task(
                        self._deliver(
                            delivery.id,
                            delivery.task_id,
                            delivery.task_version,
                            delivery.attempts,
                        ),
                        name=f"moderation-callback-delivery-{delivery.id}",
                    )
                    self._active.add(task)
                if not claimed:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.poll_seconds,
                        )
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
        finally:
            if self._active:
                await asyncio.gather(*self._active, return_exceptions=True)
            logger.info("Moderation callback dispatcher %s stopped", self.worker_id)

    async def list_deliveries(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ModerationCallbackDeliveryView]:
        async with self.database.session() as session:
            rows = await self.outbox.list(
                session,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [
                ModerationCallbackDeliveryView.model_validate(
                    row,
                    from_attributes=True,
                )
                for row in rows
            ]

    async def _claim(self):
        async with self.database.session() as session:
            delivery = await self.outbox.claim_next(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            await session.commit()
            return delivery

    async def _deliver(
        self,
        delivery_id: UUID,
        task_id: UUID,
        task_version: int,
        attempt: int,
    ) -> None:
        try:
            async with self.database.session() as session:
                task = await self.tasks.get(session, task_id)
                if task.version != task_version:
                    return
                detail = task_to_detail(task)
            await self.provider.apply_moderation_result(detail)
            async with self.database.session() as session:
                delivered = await self.outbox.mark_delivered(
                    session,
                    delivery_id=delivery_id,
                    worker_id=self.worker_id,
                    expected_attempt=attempt,
                    task_version=task_version,
                )
                if delivered:
                    await self.logs.add(
                        session,
                        task_id=task_id,
                        event=ActionLogEvent.COMMUNITY_STATUS_UPDATED,
                        source=DecisionSource.SYSTEM,
                        details={
                            "delivery_id": str(delivery_id),
                            "attempt": attempt,
                            "task_version": task_version,
                        },
                    )
                await session.commit()
        except Exception as exc:
            http_status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            logger.warning(
                "Moderation callback failed task=%s delivery=%s attempt=%s: %s",
                task_id,
                delivery_id,
                attempt,
                exc,
            )
            async with self.database.session() as session:
                await self.outbox.mark_failed(
                    session,
                    delivery_id=delivery_id,
                    worker_id=self.worker_id,
                    expected_attempt=attempt,
                    task_version=task_version,
                    error=str(exc) or type(exc).__name__,
                    http_status=http_status,
                    retry_base_seconds=self.retry_base_seconds,
                    retry_max_seconds=self.retry_max_seconds,
                )
                await session.commit()

    def _reap(self) -> None:
        finished = {task for task in self._active if task.done()}
        for task in finished:
            self._active.discard(task)
            try:
                task.result()
            except Exception:
                logger.exception("Unhandled moderation callback delivery failure")
