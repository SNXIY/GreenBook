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
    ) -> None:
        self._queue = queue
        self._handler = execution_handler
        self._worker_id = worker_id
        self._lease_seconds = max(1, lease_seconds)
        self._poll_interval = max(0.0, poll_interval_seconds)
        self._batch_size = max(1, batch_size)
        self._stop = asyncio.Event()
        self._claimed: set[str] = set()

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
        handled: list[ExecutionQueueMessage] = []
        for message in claimed:
            self._claimed.add(message.message_id)
            if self.stopped:
                self._release(message)
                continue
            try:
                result = self._handler(message)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self._release(message)
                raise
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
                self._claimed.discard(message.message_id)
                continue

            acked = self._queue.ack(
                message.message_id,
                worker_id=self._worker_id,
            )
            self._claimed.discard(message.message_id)
            if acked is not None:
                handled.append(acked)
        return handled

    async def run(self) -> None:
        """Poll until shutdown or task cancellation."""

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
        self._stop.set()

    async def shutdown(self) -> None:
        """Release claims held by this process before it exits."""

        self.request_shutdown()
        for message_id in list(self._claimed):
            message = self._queue.get(message_id)
            if message is not None:
                self._queue.release(message_id, worker_id=self._worker_id)
            self._claimed.discard(message_id)

    def _release(self, message: ExecutionQueueMessage) -> None:
        self._queue.release(message.message_id, worker_id=self._worker_id)
        self._claimed.discard(message.message_id)


__all__ = ["ExecutionHandler", "ExecutionQueueWorker"]
