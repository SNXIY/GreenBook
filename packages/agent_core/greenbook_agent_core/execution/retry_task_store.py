"""Memory and SQLAlchemy stores for restart-safe retry tasks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol

import sqlalchemy as sa

from .persistence import execution_metadata, retry_tasks
from .retry_task import RetryTask, RetryTaskStatus


class RetryTaskStoreProtocol(Protocol):
    """Storage contract consumed by RetryScheduler and background workers."""

    def create(self, task: RetryTask) -> RetryTask: ...

    def get(self, task_id: str) -> RetryTask | None: ...

    def get_by_key(self, key: tuple[str, str, int]) -> RetryTask | None: ...

    def list_ready(self) -> list[RetryTask]: ...

    def count_ready(self) -> int: ...

    def claim_due(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[RetryTask]: ...

    def complete(self, task_id: str, *, worker_id: str | None = None) -> RetryTask | None: ...

    def release(self, task_id: str, *, worker_id: str) -> RetryTask | None: ...

    def cancel(self, task_id: str) -> RetryTask | None: ...


class RetryTaskStore:
    """Thread-safe in-memory store preserving the durable-store contract."""

    def __init__(
        self,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now_factory or (lambda: datetime.now(UTC))
        self._tasks: dict[str, RetryTask] = {}
        self._lock = RLock()

    def create(self, task: RetryTask) -> RetryTask:
        with self._lock:
            existing = self._tasks.get(task.task_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            self._tasks[task.task_id] = task.model_copy(deep=True)
            return task.model_copy(deep=True)

    def get(self, task_id: str) -> RetryTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task is not None else None

    def get_by_key(self, key: tuple[str, str, int]) -> RetryTask | None:
        with self._lock:
            return next(
                (
                    task.model_copy(deep=True)
                    for task in self._tasks.values()
                    if task.key == key
                ),
                None,
            )

    def list_ready(self) -> list[RetryTask]:
        with self._lock:
            self._reclaim_expired_claims(self._now())
            return [
                task.model_copy(deep=True)
                for task in self._tasks.values()
                if task.status == RetryTaskStatus.READY
            ]

    def count_ready(self) -> int:
        return len(self.list_ready())

    def claim_due(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[RetryTask]:
        with self._lock:
            self._reclaim_expired_claims(now)
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: (task.next_retry_time, task.task_id),
            )
            claimed: list[RetryTask] = []
            for task in tasks:
                if task.status != RetryTaskStatus.READY:
                    continue
                if task.next_retry_time > now:
                    continue
                if task.deadline is not None and now > task.deadline:
                    self._set_status(task, RetryTaskStatus.CANCELLED)
                    continue
                if task.retry_budget <= 0 or task.attempt > task.max_attempts:
                    self._set_status(task, RetryTaskStatus.CANCELLED)
                    continue
                claimed_task = task.model_copy(
                    update={
                        "status": RetryTaskStatus.CLAIMED,
                        "claimed_by": worker_id,
                        "claim_until": now + timedelta(seconds=lease_seconds),
                        "updated_at": now.isoformat(),
                    },
                    deep=True,
                )
                self._tasks[task.task_id] = claimed_task
                claimed.append(claimed_task.model_copy(deep=True))
                if limit is not None and len(claimed) >= limit:
                    break
            return claimed

    def complete(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
    ) -> RetryTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != RetryTaskStatus.CLAIMED:
                return None
            if worker_id is not None and task.claimed_by != worker_id:
                return None
            return self._set_status(task, RetryTaskStatus.COMPLETED)

    def release(self, task_id: str, *, worker_id: str) -> RetryTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != RetryTaskStatus.CLAIMED:
                return None
            if task.claimed_by != worker_id:
                return None
            released = task.model_copy(
                update={
                    "status": RetryTaskStatus.READY,
                    "claimed_by": None,
                    "claim_until": None,
                    "updated_at": self._now().isoformat(),
                },
                deep=True,
            )
            self._tasks[task_id] = released
            return released.model_copy(deep=True)

    def cancel(self, task_id: str) -> RetryTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return self._set_status(task, RetryTaskStatus.CANCELLED)

    def list(self) -> list[RetryTask]:
        with self._lock:
            return [task.model_copy(deep=True) for task in self._tasks.values()]

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()

    def _reclaim_expired_claims(self, now: datetime) -> None:
        for task in list(self._tasks.values()):
            if (
                task.status == RetryTaskStatus.CLAIMED
                and task.claim_until is not None
                and task.claim_until <= now
            ):
                self._tasks[task.task_id] = task.model_copy(
                    update={
                        "status": RetryTaskStatus.READY,
                        "claimed_by": None,
                        "claim_until": None,
                        "updated_at": now.isoformat(),
                    },
                    deep=True,
                )

    def _set_status(self, task: RetryTask, status: RetryTaskStatus) -> RetryTask:
        updated = task.model_copy(
            update={
                "status": status,
                "claimed_by": None,
                "claim_until": None,
                "updated_at": self._now().isoformat(),
            },
            deep=True,
        )
        self._tasks[task.task_id] = updated
        return updated.model_copy(deep=True)


class PostgresRetryTaskStore:
    """SQLAlchemy-backed retry task store with claim leases."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def create(self, task: RetryTask) -> RetryTask:
        existing = self.get_by_key(task.key)
        if existing is not None:
            return existing
        values = _task_values(task)
        try:
            with self._connect() as conn:
                conn.execute(sa.insert(retry_tasks).values(**values))
        except sa.exc.IntegrityError:
            existing = self.get_by_key(task.key)
            if existing is not None:
                return existing
            raise
        return task.model_copy(deep=True)

    def get(self, task_id: str) -> RetryTask | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(retry_tasks).where(retry_tasks.c.task_id == task_id)
            ).mappings().first()
        return _to_task(row) if row is not None else None

    def get_by_key(self, key: tuple[str, str, int]) -> RetryTask | None:
        execution_id, step_id, attempt = key
        with self._connect() as conn:
            row = conn.execute(
                sa.select(retry_tasks).where(
                    retry_tasks.c.execution_id == execution_id,
                    retry_tasks.c.step_id == step_id,
                    retry_tasks.c.attempt == attempt,
                )
            ).mappings().first()
        return _to_task(row) if row is not None else None

    def list_ready(self) -> list[RetryTask]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(retry_tasks)
                .where(retry_tasks.c.status == RetryTaskStatus.READY.value)
                .order_by(retry_tasks.c.next_retry_time, retry_tasks.c.task_id)
            ).mappings().all()
        return [_to_task(row) for row in rows]

    def count_ready(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(retry_tasks)
                    .where(retry_tasks.c.status == RetryTaskStatus.READY.value)
                ).scalar_one()
            )

    def claim_due(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[RetryTask]:
        now_text = now.isoformat()
        claim_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[RetryTask] = []
        with self._connect() as conn:
            conn.execute(
                sa.update(retry_tasks)
                .where(
                    retry_tasks.c.status == RetryTaskStatus.CLAIMED.value,
                    retry_tasks.c.claim_until.is_not(None),
                    retry_tasks.c.claim_until <= now_text,
                )
                .values(
                    status=RetryTaskStatus.READY.value,
                    claimed_by=None,
                    claim_until=None,
                    updated_at=now_text,
                )
            )
            rows = conn.execute(
                sa.select(retry_tasks)
                .where(
                    retry_tasks.c.status == RetryTaskStatus.READY.value,
                    retry_tasks.c.next_retry_time <= now_text,
                )
                .order_by(retry_tasks.c.next_retry_time, retry_tasks.c.task_id)
            ).mappings().all()
            for row in rows:
                if row["deadline"] is not None and row["deadline"] < now_text:
                    conn.execute(
                        sa.update(retry_tasks)
                        .where(retry_tasks.c.task_id == row["task_id"])
                        .values(
                            status=RetryTaskStatus.CANCELLED.value,
                            updated_at=now_text,
                        )
                    )
                    continue
                if row["retry_budget"] <= 0 or row["attempt"] > row["max_attempts"]:
                    conn.execute(
                        sa.update(retry_tasks)
                        .where(retry_tasks.c.task_id == row["task_id"])
                        .values(
                            status=RetryTaskStatus.CANCELLED.value,
                            updated_at=now_text,
                        )
                    )
                    continue
                result = conn.execute(
                    sa.update(retry_tasks)
                    .where(
                        retry_tasks.c.task_id == row["task_id"],
                        retry_tasks.c.status == RetryTaskStatus.READY.value,
                    )
                    .values(
                        status=RetryTaskStatus.CLAIMED.value,
                        claimed_by=worker_id,
                        claim_until=claim_until,
                        updated_at=now_text,
                    )
                )
                if result.rowcount:
                    data = dict(row)
                    data.update(
                        status=RetryTaskStatus.CLAIMED.value,
                        claimed_by=worker_id,
                        claim_until=claim_until,
                        updated_at=now_text,
                    )
                    claimed.append(_to_task(data))
                    if limit is not None and len(claimed) >= limit:
                        break
        return claimed

    def complete(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
    ) -> RetryTask | None:
        return self._finish(task_id, RetryTaskStatus.COMPLETED, worker_id=worker_id)

    def release(self, task_id: str, *, worker_id: str) -> RetryTask | None:
        with self._connect() as conn:
            statement = sa.update(retry_tasks).where(
                retry_tasks.c.task_id == task_id,
                retry_tasks.c.status == RetryTaskStatus.CLAIMED.value,
                retry_tasks.c.claimed_by == worker_id,
            ).values(
                status=RetryTaskStatus.READY.value,
                claimed_by=None,
                claim_until=None,
                updated_at=datetime.now(UTC).isoformat(),
            )
            if conn.execute(statement).rowcount == 0:
                return None
        return self.get(task_id)

    def cancel(self, task_id: str) -> RetryTask | None:
        return self._finish(task_id, RetryTaskStatus.CANCELLED)

    def list(self) -> list[RetryTask]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(retry_tasks).order_by(retry_tasks.c.created_at, retry_tasks.c.task_id)
            ).mappings().all()
        return [_to_task(row) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute(sa.delete(retry_tasks))

    def _finish(
        self,
        task_id: str,
        status: RetryTaskStatus,
        *,
        worker_id: str | None = None,
    ) -> RetryTask | None:
        conditions = [retry_tasks.c.task_id == task_id]
        if worker_id is not None:
            conditions.extend(
                [
                    retry_tasks.c.status == RetryTaskStatus.CLAIMED.value,
                    retry_tasks.c.claimed_by == worker_id,
                ]
            )
        with self._connect() as conn:
            result = conn.execute(
                sa.update(retry_tasks)
                .where(*conditions)
                .values(
                    status=status.value,
                    claimed_by=None,
                    claim_until=None,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            if result.rowcount == 0:
                return None
        return self.get(task_id)


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


def _task_values(task: RetryTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "execution_id": task.execution_id,
        "step_id": task.step_id,
        "attempt": task.attempt,
        "next_retry_time": task.next_retry_time.isoformat(),
        "backoff": task.backoff,
        "reason": task.reason,
        "retry_budget": task.retry_budget,
        "max_attempts": task.max_attempts,
        "deadline": task.deadline.isoformat() if task.deadline is not None else None,
        "operation_id": task.operation_id,
        "status": task.status.value,
        "claimed_by": task.claimed_by,
        "claim_until": task.claim_until.isoformat() if task.claim_until else None,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _to_task(row: Any) -> RetryTask:
    return RetryTask.model_validate(dict(row))


__all__ = [
    "PostgresRetryTaskStore",
    "RetryTaskStore",
    "RetryTaskStoreProtocol",
]
