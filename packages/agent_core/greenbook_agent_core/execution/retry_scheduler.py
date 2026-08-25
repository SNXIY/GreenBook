"""Small in-process scheduling primitives for evidence-authorized retries.

The scheduler owns timing and de-duplication only.  It does not classify a
failure, bypass the retry decision gate, or execute a tool.  A caller must
provide an already-approved ``RetryDecision`` and may later dispatch due tasks
through the existing ``RetryManager``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .retry_decision import RetryDecision
from .retry_task import RetryTask, RetryTaskStatus
from .retry_task_store import RetryTaskStore, RetryTaskStoreProtocol

if TYPE_CHECKING:
    from .retry_manager import RetryManager


class RetryScheduler:
    """Delayed retry facade backed by memory or a durable task store."""

    def __init__(
        self,
        *,
        now_factory: Callable[[], datetime] | None = None,
        task_store: RetryTaskStoreProtocol | None = None,
        worker_id: str = "retry-scheduler",
        lease_seconds: int = 60,
    ) -> None:
        self._now = now_factory or (lambda: datetime.now(UTC))
        self._task_store = task_store or RetryTaskStore(now_factory=self._now)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    @property
    def task_store(self) -> RetryTaskStoreProtocol:
        return self._task_store

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def schedule(self, task: RetryTask) -> RetryTask | None:
        """Enqueue a task, or return the original task for a duplicate.

        A task outside its attempt budget or deadline is rejected before it
        can enter the queue.  Returning the existing task makes repeated API
        calls idempotent without executing the retry twice.
        """

        existing = self._task_store.get_by_key(task.key)
        if existing is not None:
            return existing
        if task.retry_budget <= 0:
            return None
        if task.attempt > task.max_attempts:
            return None
        if task.deadline is not None and task.next_retry_time > task.deadline:
            return None
        return self._task_store.create(task)

    def schedule_decision(
        self,
        *,
        execution_id: str,
        step_id: str,
        decision: RetryDecision,
        reason: str | None = None,
        deadline: datetime | None = None,
    ) -> RetryTask | None:
        """Materialize a task only from an allowed common retry decision."""

        if (
            not decision.allowed
            or decision.requires_reconciliation
            or decision.requires_user_confirmation
        ):
            return None
        if decision.retry_budget <= 0 or decision.attempt > decision.max_attempts:
            return None

        next_retry_time = decision.retry_after or (
            self._now() + timedelta(seconds=decision.backoff)
        )
        task = RetryTask(
            execution_id=execution_id,
            step_id=step_id,
            attempt=decision.attempt,
            next_retry_time=next_retry_time,
            backoff=decision.backoff,
            reason=reason or decision.reason,
            retry_budget=decision.retry_budget,
            max_attempts=decision.max_attempts,
            deadline=deadline,
            operation_id=decision.operation_id,
        )
        return self.schedule(task)

    def schedule_step(
        self,
        *,
        retry_manager: RetryManager,
        execution_id: str,
        step_id: str,
        deadline: datetime | None = None,
    ) -> tuple[RetryTask | None, RetryDecision]:
        """Ask the common gate for a step decision, then schedule it."""

        decision = retry_manager.decision_for_step(
            execution_id,
            step_id,
            source="retry_scheduler",
            user_requested_retry=False,
        )
        task = self.schedule_decision(
            execution_id=execution_id,
            step_id=step_id,
            decision=decision,
            deadline=deadline,
        )
        return task, decision

    def due(
        self,
        now: datetime | None = None,
        *,
        worker_id: str | None = None,
        limit: int | None = None,
    ) -> list[RetryTask]:
        """Claim and return due tasks in deterministic time/key order."""

        return self._task_store.claim_due(
            now or self._now(),
            worker_id=worker_id or self._worker_id,
            lease_seconds=self._lease_seconds,
            limit=limit,
        )

    def pending(self) -> list[RetryTask]:
        """Return pending tasks without claiming them."""

        return sorted(self._task_store.list_ready(), key=lambda task: (task.next_retry_time, task.key))

    def count(self) -> int:
        return self._task_store.count_ready()

    def cancel(self, execution_id: str, step_id: str, attempt: int) -> bool:
        """Cancel a pending task while retaining its idempotency record."""

        task = self._task_store.get_by_key((execution_id, step_id, attempt))
        return task is not None and self._task_store.cancel(task.task_id) is not None


__all__ = ["RetryScheduler", "RetryTask", "RetryTaskStatus"]
