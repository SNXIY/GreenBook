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

from pydantic import BaseModel, ConfigDict, Field

from .retry_decision import RetryDecision

if TYPE_CHECKING:
    from .models import StepExecution
    from .retry_manager import RetryManager


class RetryTask(BaseModel):
    """One delayed retry request, keyed by execution, step, and attempt."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    step_id: str
    attempt: int = Field(ge=1)
    next_retry_time: datetime
    backoff: float = Field(default=0.0, ge=0.0)
    reason: str
    retry_budget: int = Field(default=1, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    deadline: datetime | None = None
    operation_id: str | None = None

    @property
    def key(self) -> tuple[str, str, int]:
        """Stable idempotency key for one logical step attempt."""

        return (self.execution_id, self.step_id, self.attempt)


class RetryScheduler:
    """In-memory delayed retry queue with duplicate-attempt protection."""

    def __init__(
        self,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now_factory or (lambda: datetime.now(UTC))
        self._pending: dict[tuple[str, str, int], RetryTask] = {}
        # Keep consumed keys as well as pending keys.  A duplicate request
        # arriving after dispatch must not create a second retry task.
        self._known: dict[tuple[str, str, int], RetryTask] = {}

    def schedule(self, task: RetryTask) -> RetryTask | None:
        """Enqueue a task, or return the original task for a duplicate.

        A task outside its attempt budget or deadline is rejected before it
        can enter the queue.  Returning the existing task makes repeated API
        calls idempotent without executing the retry twice.
        """

        existing = self._known.get(task.key)
        if existing is not None:
            return existing
        if task.retry_budget <= 0:
            return None
        if task.attempt > task.max_attempts:
            return None
        if task.deadline is not None and task.next_retry_time > task.deadline:
            return None
        self._known[task.key] = task
        self._pending[task.key] = task
        return task

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

    def due(self, now: datetime | None = None) -> list[RetryTask]:
        """Claim and return due tasks in deterministic time/key order."""

        current = now or self._now()
        for key, task in list(self._pending.items()):
            if task.deadline is not None and current > task.deadline:
                self._pending.pop(key, None)
        due_tasks = sorted(
            (
                task
                for task in self._pending.values()
                if task.next_retry_time <= current
            ),
            key=lambda task: (task.next_retry_time, task.key),
        )
        for task in due_tasks:
            self._pending.pop(task.key, None)
        return due_tasks

    def dispatch_due(
        self,
        *,
        retry_manager: RetryManager,
        now: datetime | None = None,
    ) -> list[tuple[RetryTask, StepExecution]]:
        """Dispatch due tasks through RetryManager's common safety gate.

        This only prepares steps for a future Worker pass.  It does not call a
        tool and does not introduce a new Execution state.
        """

        dispatched: list[tuple[RetryTask, StepExecution]] = []
        for task in self.due(now):
            step = retry_manager.retry_step(
                task.execution_id,
                task.step_id,
                source="retry_scheduler",
                user_requested_retry=False,
            )
            dispatched.append((task, step))
        return dispatched

    def pending(self) -> list[RetryTask]:
        """Return pending tasks without claiming them."""

        return sorted(
            self._pending.values(),
            key=lambda task: (task.next_retry_time, task.key),
        )

    def count(self) -> int:
        return len(self._pending)

    def cancel(self, execution_id: str, step_id: str, attempt: int) -> bool:
        """Remove a pending task; its key remains consumed for idempotency."""

        return (
            self._pending.pop((execution_id, step_id, attempt), None) is not None
        )


__all__ = ["RetryScheduler", "RetryTask"]
