"""Process-neutral consumer for the Runtime Execution Queue."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .execution_queue import (
    ExecutionQueueMessage,
    ExecutionQueueProtocol,
)

logger = logging.getLogger(__name__)

ExecutionHandler = Callable[
    [ExecutionQueueMessage],
    Awaitable[Any] | Any,
]


class ExecutionHandlerDeferredError(RuntimeError):
    """Signal that a queue message is valid but cannot run yet.

    Unlike an execution failure, a deferred delivery is released back to the
    queue.  The primary use is an API-managed local worker waiting for a fresh,
    already-validated user credential after an API restart.  The credential is
    deliberately never persisted in the queue payload.
    """


class ExecutionQueueWorker:
    """Claim queue messages and delegate execution to an injected handler.

    This class owns queue delivery semantics only. The handler is the boundary
    where a process-specific Runtime execution service can load the dispatch
    envelope and invoke ``ExecutionWorker``. The queue worker never calls
    Planner, MCP, Java, Creator, or a tool directly.
    """

    def __init__(
        self,
        *,
        queue: ExecutionQueueProtocol,
        execution_handler: ExecutionHandler,
        worker_id: str = "execution-worker",
        lease_seconds: int = 60,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 10,
        lease_manager: Any | None = None,
    ) -> None:
        self._queue = queue
        self._handler = execution_handler
        self._worker_id = worker_id
        self._lease_seconds = max(1, lease_seconds)
        self._poll_interval = max(0.0, poll_interval_seconds)
        self._batch_size = max(1, batch_size)
        # Queue claims prevent duplicate delivery while the message lease is
        # valid.  The Runtime lease is an additional execution-level guard
        # shared by API/worker processes and is deliberately optional for
        # older embedders and unit tests.
        self._lease_manager = lease_manager
        self._stop = asyncio.Event()
        self._claimed: set[str] = set()
        self._execution_leases: dict[str, str] = {}

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ExecutionQueueMessage]:
        """Claim one batch and ack/fail each delegated message."""

        claimed = self._queue.claim(
            now or datetime.now(UTC),
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            limit=self._batch_size,
        )
        if claimed:
            logger.info(
                "Execution Queue claimed count=%s worker_id=%s execution_ids=%s",
                len(claimed),
                self._worker_id,
                [message.execution_id for message in claimed],
            )
        handled: list[ExecutionQueueMessage] = []
        for message in claimed:
            self._claimed.add(message.message_id)
            if self.stopped:
                self._release(message)
                continue
            if not self._acquire_execution_lease(message):
                # Another process already owns this execution.  Release the
                # queue claim so the message can be claimed again after the
                # owning lease expires or the process exits.
                self._release(message)
                continue
            try:
                result = self._handler(message)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self._release(message)
                raise
            except ExecutionHandlerDeferredError:
                logger.debug(
                    "Execution queue delivery deferred execution_id=%s message_id=%s",
                    message.execution_id,
                    message.message_id,
                )
                self._release(message)
                continue
            except Exception as exc:
                logger.exception(
                    "Execution queue handler failed execution_id=%s message_id=%s",
                    message.execution_id,
                    message.message_id,
                )
                self._queue.fail(
                    message.message_id,
                    worker_id=self._worker_id,
                    error=str(exc) or type(exc).__name__,
                )
                self._release_execution_lease(message.message_id, message.execution_id)
                self._claimed.discard(message.message_id)
                continue

            acked = self._queue.ack(
                message.message_id,
                worker_id=self._worker_id,
            )
            self._release_execution_lease(message.message_id, message.execution_id)
            self._claimed.discard(message.message_id)
            if acked is not None:
                handled.append(acked)
        return handled

    async def run(self) -> None:
        """Poll until shutdown or task cancellation."""

        logger.info(
            "Execution Queue polling started worker_id=%s poll_interval_seconds=%s",
            self._worker_id,
            self._poll_interval,
        )
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
        self._stop.set()

    async def shutdown(self) -> None:
        """Release claims held by this process before it exits."""

        self.request_shutdown()
        for message_id in list(self._claimed):
            message = self._queue.get(message_id)
            if message is not None:
                self._queue.release(message_id, worker_id=self._worker_id)
                execution_id = message.execution_id
            else:
                execution_id = self._execution_leases.get(message_id)
            self._release_execution_lease(message_id, execution_id)
            self._claimed.discard(message_id)

    def _release(self, message: ExecutionQueueMessage) -> None:
        self._queue.release(message.message_id, worker_id=self._worker_id)
        self._release_execution_lease(message.message_id, message.execution_id)
        self._claimed.discard(message.message_id)

    def _acquire_execution_lease(self, message: ExecutionQueueMessage) -> bool:
        if self._lease_manager is None:
            return True
        try:
            acquired = bool(
                self._lease_manager.acquire(
                    message.execution_id,
                    self._worker_id,
                    ttl_seconds=self._lease_seconds,
                )
            )
        except Exception:
            logger.exception(
                "Execution lease acquisition failed execution_id=%s",
                message.execution_id,
            )
            return False
        if acquired:
            self._execution_leases[message.message_id] = message.execution_id
        return acquired

    def _release_execution_lease(
        self,
        message_id: str,
        execution_id: str | None,
    ) -> None:
        tracked_execution_id = self._execution_leases.pop(message_id, None)
        execution_id = execution_id or tracked_execution_id
        if self._lease_manager is None or not execution_id:
            return
        try:
            self._lease_manager.release(execution_id, self._worker_id)
        except Exception:
            logger.exception(
                "Execution lease release failed execution_id=%s",
                execution_id,
            )


__all__ = [
    "ExecutionHandler",
    "ExecutionHandlerDeferredError",
    "ExecutionQueueWorker",
]
