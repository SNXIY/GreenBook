"""Process-neutral consumer for the Runtime Execution Queue."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.goal.ready_work import (
    WorkAccess,
    access_mode,
    resource_conflict,
    resource_keys,
)

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
    Planner, MCP, Java, or a tool directly.
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
        max_concurrency: int = 1,
        lease_manager: Any | None = None,
        resource_access_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._queue = queue
        self._handler = execution_handler
        self._worker_id = worker_id
        self._lease_seconds = max(1, lease_seconds)
        self._poll_interval = max(0.0, poll_interval_seconds)
        self._batch_size = max(1, batch_size)
        self._max_concurrency = max(1, max_concurrency)
        # Queue claims prevent duplicate delivery while the message lease is
        # valid.  The Runtime lease is an additional execution-level guard
        # shared by API/worker processes and is deliberately optional for
        # older embedders and unit tests.
        self._lease_manager = lease_manager
        self._resource_access_provider = resource_access_provider
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
            limit=min(self._batch_size, self._max_concurrency),
        )
        if claimed:
            logger.info(
                "Execution Queue claimed count=%s worker_id=%s execution_ids=%s",
                len(claimed),
                self._worker_id,
                [message.execution_id for message in claimed],
            )
        active_work: list[Any] = []
        provider_failed = False
        if self._resource_access_provider is not None:
            try:
                active_work = self._resource_access_provider() or []
                if inspect.isawaitable(active_work):
                    active_work = await active_work
                active_work = list(active_work or [])
            except Exception:
                # A conflict check that cannot read durable state must fail
                # closed; it releases the queue claim for a later retry.
                logger.exception("Execution resource conflict lookup failed")
                provider_failed = True

        processable: list[ExecutionQueueMessage] = []
        for message in claimed:
            if provider_failed or self._message_conflicts(
                message,
                processable,
                active_work,
            ):
                # Defer with exponential backoff instead of releasing with an
                # unchanged availability: a message that always conflicts must
                # not spin a claim/release busy-loop or starve other work.
                # Once the attempt cap is exceeded the message is failed so it
                # cannot loop forever.
                logger.info(
                    "Execution deferred by resource conflict execution_id=%s attempt=%s",
                    message.execution_id,
                    message.attempt,
                )
                self._release_deferred(message)
                continue
            processable.append(message)

        results = await asyncio.gather(
            *(self._handle_message(message) for message in processable),
            return_exceptions=True,
        )
        handled: list[ExecutionQueueMessage] = []
        for result in results:
            if isinstance(result, ExecutionQueueMessage):
                handled.append(result)
            elif isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error(
                    "Execution queue delivery task failed",
                    exc_info=(type(result), result, result.__traceback__),
                )
        return handled

    @staticmethod
    def _message_conflicts(
        message: ExecutionQueueMessage,
        processable: list[ExecutionQueueMessage],
        active_work: list[Any],
    ) -> bool:
        current = _message_access(message)
        if not current.resource_keys:
            return False
        for other in processable:
            if resource_conflict(current, _message_access(other)):
                return True
        for other in active_work:
            execution_id = str(getattr(other, "execution_id", "") or "")
            if execution_id == message.execution_id:
                continue
            status = str(getattr(other, "status", "") or "").upper()
            if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            if resource_conflict(current, _execution_access(other)):
                return True
        return False

    async def _handle_message(
        self,
        message: ExecutionQueueMessage,
    ) -> ExecutionQueueMessage | None:
        """Handle one claimed message; independent messages overlap here."""

        self._claimed.add(message.message_id)
        if self.stopped:
            self._release(message)
            return None
        if not self._acquire_execution_lease(message):
            # Another process already owns this execution.  Release the queue
            # claim so the message can be claimed again after the owning lease
            # expires or the process exits.
            self._release(message)
            return None
        heartbeat: asyncio.Task[None] | None = None
        try:
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(message),
                name=f"lease-heartbeat:{message.execution_id}",
            )
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
            return None
        except Exception as exc:
            logger.exception(
                "Execution queue handler failed execution_id=%s message_id=%s",
                message.execution_id,
                message.message_id,
            )
            if _is_transient_handler_error(exc):
                # A transient delivery failure (network / timeout / outage)
                # must be retried with backoff, not permanently failed: a
                # permanent FAIL would drop work that a healthy dependency
                # could complete moments later (design goal 0813 — the queue
                # recovers, it does not silently lose user work).
                self._release_deferred(message)
                return None
            self._queue.fail(
                message.message_id,
                worker_id=self._worker_id,
                error=str(exc) or type(exc).__name__,
            )
            self._release_execution_lease(message.message_id, message.execution_id)
            self._claimed.discard(message.message_id)
            return None
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

        acked = self._queue.ack(
            message.message_id,
            worker_id=self._worker_id,
        )
        self._release_execution_lease(message.message_id, message.execution_id)
        self._claimed.discard(message.message_id)
        return acked

    async def _lease_heartbeat(self, message: ExecutionQueueMessage) -> None:
        """Renew the execution lease while the handler runs.

        Long-running tools (Java publish or draft flows can take minutes) must
        not let the lease expire mid-execution: an expired lease lets a second
        worker claim the same execution and run it concurrently (double side
        effects).  The heartbeat renews at half the lease TTL and stops when
        the handler finishes or the process shuts down.
        """
        if self._lease_manager is None:
            return
        interval = max(0.1, self._lease_seconds / 2)
        while True:
            await asyncio.sleep(interval)
            if self.stopped:
                return
            try:
                renewed = self._lease_manager.renew(
                    message.execution_id,
                    self._worker_id,
                    ttl_seconds=self._lease_seconds,
                )
                if not renewed:
                    # The lease was lost (another worker claimed it or it was
                    # explicitly released); stop renewing.  The handler keeps
                    # running to finish its current side effect, but no longer
                    # holds ownership.
                    logger.warning(
                        "Execution lease lost execution_id=%s worker_id=%s",
                        message.execution_id,
                        self._worker_id,
                    )
                    return
            except Exception:
                logger.exception(
                    "Execution lease renewal failed execution_id=%s",
                    message.execution_id,
                )
                return

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

    def _release_deferred(self, message: ExecutionQueueMessage) -> None:
        """Release with exponential backoff; fail after the deferral cap.

        The backoff gives the conflicting work a chance to settle instead of
        the two messages spinning forever; the cap turns an always-conflicting
        message into a visible failure (dead-letter) rather than a silent
        infinite loop."""
        release_deferred = getattr(self._queue, "release_deferred", None)
        max_deferrals = getattr(self, "_max_deferrals", 20)
        next_attempt = message.attempt + 1
        if callable(release_deferred) and next_attempt < max_deferrals:
            delay = min(30.0, (2 ** max(0, message.attempt - 1)) * 0.5)
            release_deferred(
                message.message_id,
                worker_id=self._worker_id,
                delay_seconds=delay,
            )
        else:
            self._queue.fail(
                message.message_id,
                worker_id=self._worker_id,
                error="Execution deferred too many times (resource conflict)",
            )
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


def _is_transient_handler_error(exc: BaseException) -> bool:
    """Return True when the handler failure is transient and should be retried.

    A transient failure is one that a healthy dependency could complete moments
    later: network errors, timeouts, and anything explicitly marked retryable
    (e.g. an upstream outage on the MCP/Java boundary).  Permanent
    failures (validation, logic, unknown tool) are returned to the caller as
    ``False`` so the queue worker fails the message instead of spinning.
    """
    for cause in _exception_chain(exc):
        retryable = getattr(cause, "retryable", None)
        if retryable is True:
            return True
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError)):
            return True
        module = type(cause).__module__
        if module == "httpx" or module.startswith("httpx."):
            # ConnectError / ReadTimeout / RemoteProtocolError etc. all
            # indicate the upstream was unreachable, not that the work is bad.
            return True
    return False


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Flatten the exception and its ``__cause__``/``__context__`` chain."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _message_access(message: ExecutionQueueMessage) -> WorkAccess:
    payload = dict(message.payload or {})
    return WorkAccess(
        task_id=str(payload.get("task_id") or ""),
        goal_id=str(payload.get("goal_id") or ""),
        resource_keys=resource_keys(payload),
        access_mode=access_mode(payload),
        status="QUEUED",
    )


def _execution_access(execution: Any) -> WorkAccess:
    return WorkAccess(
        task_id=str(getattr(execution, "task_id", "") or ""),
        goal_id="",
        resource_keys=resource_keys(execution),
        access_mode=access_mode(execution),
        status=str(getattr(execution, "status", "") or ""),
    )
