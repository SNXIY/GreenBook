from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.creator.application.harness import CreatorAgentHarness
from app.creator.application.ports import CreatorUnitOfWorkFactory
from app.creator.domain.errors import (
    CreatorHarnessError,
    CreatorInvalidTransitionError,
    CreatorRunLeaseConflictError,
)
from app.creator.domain.models import CreatorOutboxMessage

logger = logging.getLogger(__name__)


class CreatorWorkerError(RuntimeError):
    pass


class CreatorUnknownOutboxTopicError(CreatorWorkerError):
    pass


class CreatorOutboxLeaseLostError(CreatorWorkerError):
    pass


@dataclass(frozen=True)
class CreatorOutboxWorkerPolicy:
    worker_prefix: str = "creator-runtime"
    concurrency: int = 4
    batch_size: int = 8
    poll_seconds: float = 0.5
    outbox_lease_seconds: int = 300
    heartbeat_seconds: float = 30.0
    max_attempts: int = 8
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    shutdown_grace_seconds: float = 30.0
    tenant_concurrency: int = 1
    user_concurrency: int = 1
    health_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.worker_prefix.strip():
            raise ValueError("worker_prefix cannot be empty")
        if self.concurrency < 1 or self.batch_size < 1:
            raise ValueError("worker concurrency and batch size must be positive")
        if self.tenant_concurrency < 1:
            raise ValueError("tenant_concurrency must be positive")
        if self.user_concurrency < 1:
            raise ValueError("user_concurrency must be positive")
        if self.poll_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("worker polling and heartbeat must be positive")
        if self.outbox_lease_seconds <= self.heartbeat_seconds * 2:
            raise ValueError("outbox lease must exceed two heartbeat intervals")
        if self.max_attempts < 1:
            raise ValueError("worker max_attempts must be positive")
        if self.retry_base_seconds < 0 or self.retry_max_seconds < 0:
            raise ValueError("worker retry delays cannot be negative")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("worker retry max must not be below retry base")
        if self.shutdown_grace_seconds < 0:
            raise ValueError("worker shutdown grace cannot be negative")


class CreatorOutboxWorker:
    """Claims durable Creator commands and executes them through the Harness."""

    def __init__(
        self,
        *,
        uow_factory: CreatorUnitOfWorkFactory,
        harness: CreatorAgentHarness,
        policy: CreatorOutboxWorkerPolicy,
        clock: Callable[[], datetime] | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._harness = harness
        self._policy = policy
        self._clock = clock or _utc_now
        self.worker_id = instance_id or f"{policy.worker_prefix}:{uuid.uuid4().hex}"
        self._active: set[asyncio.Task[None]] = set()
        self._tenant_semaphores: dict[str, asyncio.Semaphore] = {}
        self._user_semaphores: dict[str, asyncio.Semaphore] = {}

    async def run_once(self) -> int:
        messages = await self._claim(
            min(self._policy.batch_size, self._policy.concurrency)
        )
        if not messages:
            self._write_health()
            return 0
        tasks = {
            asyncio.create_task(
                self._process(message),
                name=f"creator-outbox:{message.id}",
            )
            for message in messages
        }
        self._active.update(tasks)
        try:
            await asyncio.gather(*tasks)
        finally:
            self._active.difference_update(tasks)
            self._write_health()
        return len(messages)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self._write_health()
        try:
            while not stop_event.is_set():
                self._reap_finished()
                capacity = self._policy.concurrency - len(self._active)
                if capacity > 0:
                    messages = await self._claim(min(capacity, self._policy.batch_size))
                    for message in messages:
                        task = asyncio.create_task(
                            self._process(message),
                            name=f"creator-outbox:{message.id}",
                        )
                        self._active.add(task)
                self._write_health()
                await self._wait_for_progress(stop_event)
        finally:
            await self._drain_active()

    async def _claim(self, limit: int) -> tuple[CreatorOutboxMessage, ...]:
        if limit <= 0:
            return ()
        now = self._clock()
        async with self._uow_factory() as uow:
            messages = await uow.outbox.claim_ready(
                worker_id=self.worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.outbox_lease_seconds),
                limit=limit,
            )
            await uow.commit()
        return messages

    def _tenant_semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        semaphore = self._tenant_semaphores.get(tenant_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._policy.tenant_concurrency)
            self._tenant_semaphores[tenant_id] = semaphore
        return semaphore

    def _user_semaphore(self, creator_id: str) -> asyncio.Semaphore:
        semaphore = self._user_semaphores.get(creator_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._policy.user_concurrency)
            self._user_semaphores[creator_id] = semaphore
        return semaphore

    async def _process(self, message: CreatorOutboxMessage) -> None:
        logger.info(
            "Creator outbox claimed message_id=%s topic=%s attempt=%s worker_id=%s",
            message.id,
            message.topic,
            message.attempts,
            self.worker_id,
        )
        tenant_id = str(message.payload.get("tenant_id") or "unknown")
        creator_id = str(
            message.payload.get("creator_id")
            or f"unknown:{message.aggregate_id}"
        )
        async with self._tenant_semaphore(tenant_id), self._user_semaphore(creator_id):
            try:
                await self._execute_with_heartbeat(message)
            except asyncio.CancelledError:
                raise
            except CreatorOutboxLeaseLostError:
                logger.warning(
                    "Creator outbox lease lost message_id=%s worker_id=%s",
                    message.id,
                    self.worker_id,
                )
                return
            except Exception as exc:
                await self._record_failure(message, exc)
                return
            try:
                await self._mark_completed(message)
            except CreatorOutboxLeaseLostError:
                logger.warning(
                    "Creator outbox completion lost lease message_id=%s worker_id=%s",
                    message.id,
                    self.worker_id,
                )

    async def _execute_with_heartbeat(
        self,
        message: CreatorOutboxMessage,
    ) -> None:
        execution = asyncio.create_task(
            self._dispatch(message),
            name=f"creator-command:{message.id}",
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=self._policy.heartbeat_seconds,
                )
                if done:
                    await execution
                    return
                if not await self._renew_outbox_lease(message.id):
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    raise CreatorOutboxLeaseLostError(message.id)
                await self._renew_run_lease(message)
                self._write_health()
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise

    async def _dispatch(self, message: CreatorOutboxMessage) -> None:
        run_id = _required_payload_id(message, "run_id")
        if run_id != message.aggregate_id:
            raise CreatorUnknownOutboxTopicError(
                f"Outbox aggregate {message.aggregate_id} does not match run {run_id}"
            )
        if message.topic == "creator.run.start":
            await self._harness.start_run(run_id, worker_id=self.worker_id)
            return
        if message.topic == "creator.decision.resume":
            decision_id = _required_payload_id(message, "decision_id")
            await self._harness.resume_decision(
                decision_id,
                worker_id=self.worker_id,
            )
            return
        if message.topic == "creator.run.cancel":
            await self._harness.recover_run(
                run_id,
                worker_id=self.worker_id,
            )
            return
        raise CreatorUnknownOutboxTopicError(
            f"Unsupported Creator outbox topic {message.topic}"
        )

    async def _renew_outbox_lease(self, message_id: str) -> bool:
        now = self._clock()
        async with self._uow_factory() as uow:
            renewed = await uow.outbox.renew_lease(
                message_id,
                worker_id=self.worker_id,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._policy.outbox_lease_seconds),
            )
            await uow.commit()
        return renewed

    async def _renew_run_lease(self, message: CreatorOutboxMessage) -> None:
        if message.topic not in {
            "creator.run.start",
            "creator.decision.resume",
        }:
            return
        run_id = message.payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        try:
            await self._harness.renew_run_lease(
                run_id,
                worker_id=self.worker_id,
            )
        except (CreatorRunLeaseConflictError, CreatorInvalidTransitionError):
            logger.debug(
                "Creator run lease not renewable run_id=%s worker_id=%s",
                run_id,
                self.worker_id,
            )

    async def _mark_completed(self, message: CreatorOutboxMessage) -> None:
        now = self._clock()
        async with self._uow_factory() as uow:
            completed = await uow.outbox.mark_completed(
                message.id,
                worker_id=self.worker_id,
                now=now,
            )
            await uow.commit()
        if not completed:
            raise CreatorOutboxLeaseLostError(message.id)
        logger.info(
            "Creator outbox completed message_id=%s topic=%s worker_id=%s",
            message.id,
            message.topic,
            self.worker_id,
        )

    async def _record_failure(
        self,
        message: CreatorOutboxMessage,
        exc: Exception,
    ) -> None:
        now = self._clock()
        error = f"{type(exc).__name__}: {str(exc)}"[:4_000]
        terminal = isinstance(exc, CreatorUnknownOutboxTopicError) or (
            isinstance(exc, CreatorHarnessError) and not exc.retryable
        )
        exhausted = message.attempts >= self._policy.max_attempts
        async with self._uow_factory() as uow:
            if terminal or exhausted:
                recorded = await uow.outbox.mark_dead(
                    message.id,
                    worker_id=self.worker_id,
                    now=now,
                    last_error=error,
                )
            else:
                recorded = await uow.outbox.mark_retry(
                    message.id,
                    worker_id=self.worker_id,
                    now=now,
                    available_at=now + timedelta(seconds=self._retry_delay(message)),
                    last_error=error,
                )
            await uow.commit()
        if not recorded:
            logger.warning(
                "Creator outbox failure result lost lease message_id=%s worker_id=%s",
                message.id,
                self.worker_id,
            )
            return
        logger.error(
            "Creator outbox %s message_id=%s topic=%s attempt=%s error=%s",
            "dead" if terminal or exhausted else "retrying",
            message.id,
            message.topic,
            message.attempts,
            error,
        )

    def _retry_delay(self, message: CreatorOutboxMessage) -> float:
        exponential = self._policy.retry_base_seconds * (
            2 ** max(0, message.attempts - 1)
        )
        bounded = min(self._policy.retry_max_seconds, exponential)
        digest = hashlib.sha256(message.id.encode("utf-8")).digest()[0]
        jitter = 0.85 + (digest / 255.0) * 0.30
        return bounded * jitter

    async def _wait_for_progress(self, stop_event: asyncio.Event) -> None:
        stop_waiter = asyncio.create_task(stop_event.wait())
        waiters: set[asyncio.Task] = {*self._active, stop_waiter}
        try:
            await asyncio.wait(
                waiters,
                timeout=self._policy.poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    def _reap_finished(self) -> None:
        finished = {task for task in self._active if task.done()}
        for task in finished:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Creator worker task failed outside message handling")
        self._active.difference_update(finished)

    async def _drain_active(self) -> None:
        if not self._active:
            return
        done, pending = await asyncio.wait(
            self._active,
            timeout=self._policy.shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Creator worker task failed during shutdown")
        self._active.clear()

    def _write_health(self) -> None:
        path = self._policy.health_file
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._clock().isoformat(), encoding="ascii")
        except OSError:
            logger.exception("Creator worker heartbeat file write failed path=%s", path)


def _required_payload_id(message: CreatorOutboxMessage, key: str) -> str:
    value = message.payload.get(key)
    if not isinstance(value, str) or not value:
        raise CreatorUnknownOutboxTopicError(
            f"Creator outbox message {message.id} is missing {key}"
        )
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)
