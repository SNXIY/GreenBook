"""Business-neutral durable queue for dispatching Runtime executions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

import sqlalchemy as sa
from pydantic import BaseModel, Field

from .persistence import execution_metadata, execution_queue_messages


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ExecutionQueueStatus(StrEnum):
    READY = "READY"
    CLAIMED = "CLAIMED"
    ACKED = "ACKED"
    FAILED = "FAILED"


class ExecutionQueueMessage(BaseModel):
    """Serializable dispatch envelope owned by the queue boundary.

    ``payload`` is deliberately opaque to the queue. It may contain a
    dispatch snapshot, but the queue never interprets it or invokes business
    code. Callers must not put bearer tokens or other secrets in it.
    """

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    created_at: str = Field(default_factory=lambda: _iso(_now()))
    available_at: str = Field(default_factory=lambda: _iso(_now()))
    attempt: int = 0
    trace_id: str = ""
    status: ExecutionQueueStatus = ExecutionQueueStatus.READY
    claimed_by: str | None = None
    claim_until: str | None = None
    last_error: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: _iso(_now()))

    @property
    def key(self) -> str:
        """One logical queue item per execution id."""

        return self.execution_id


class ExecutionQueueProtocol(Protocol):
    """Queue contract consumed by API dispatch and worker processes."""

    def enqueue(
        self,
        execution_id: str | ExecutionQueueMessage,
        *,
        trace_id: str = "",
        payload: dict[str, Any] | None = None,
        requeue: bool = False,
    ) -> ExecutionQueueMessage: ...

    def get(self, message_id: str) -> ExecutionQueueMessage | None: ...

    def get_by_execution_id(self, execution_id: str) -> ExecutionQueueMessage | None: ...

    def claim(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[ExecutionQueueMessage]: ...

    def ack(self, message_id: str, *, worker_id: str) -> ExecutionQueueMessage | None: ...

    def fail(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> ExecutionQueueMessage | None: ...

    def release(
        self,
        message_id: str,
        *,
        worker_id: str,
    ) -> ExecutionQueueMessage | None: ...

    def release_deferred(
        self,
        message_id: str,
        *,
        worker_id: str,
        delay_seconds: float = 1.0,
    ) -> ExecutionQueueMessage | None: ...


class ExecutionQueue:
    """Thread-safe in-memory queue implementing the durable queue contract."""

    def __init__(self, *, now_factory: Callable[[], datetime] | None = None) -> None:
        self._now = now_factory or _now
        self._messages: dict[str, ExecutionQueueMessage] = {}
        self._lock = RLock()

    def enqueue(
        self,
        execution_id: str | ExecutionQueueMessage,
        *,
        trace_id: str = "",
        payload: dict[str, Any] | None = None,
        requeue: bool = False,
    ) -> ExecutionQueueMessage:
        message = _coerce_message(
            execution_id,
            trace_id=trace_id,
            payload=payload,
            now=self._now(),
        )
        with self._lock:
            existing = self._messages.get(message.key)
            if existing is not None:
                if requeue and existing.status in {
                    ExecutionQueueStatus.ACKED,
                    ExecutionQueueStatus.FAILED,
                    ExecutionQueueStatus.CLAIMED,
                }:
                    updated = existing.model_copy(
                        update={
                            "status": ExecutionQueueStatus.READY,
                            "claimed_by": None,
                            "claim_until": None,
                            "available_at": _iso(self._now()),
                            "last_error": "",
                            "payload": dict(payload or existing.payload),
                            "updated_at": _iso(self._now()),
                        },
                        deep=True,
                    )
                    self._messages[message.key] = updated
                    return updated.model_copy(deep=True)
                return existing.model_copy(deep=True)
            self._messages[message.key] = message
            return message.model_copy(deep=True)

    def get(self, message_id: str) -> ExecutionQueueMessage | None:
        with self._lock:
            message = next(
                (item for item in self._messages.values() if item.message_id == message_id),
                None,
            )
            return message.model_copy(deep=True) if message is not None else None

    def get_by_execution_id(self, execution_id: str) -> ExecutionQueueMessage | None:
        with self._lock:
            message = self._messages.get(execution_id)
            return message.model_copy(deep=True) if message is not None else None

    def claim(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[ExecutionQueueMessage]:
        with self._lock:
            current = now.astimezone(UTC)
            self._reclaim_expired(current)
            ready = sorted(
                self._messages.values(),
                key=lambda item: (item.available_at, item.created_at, item.message_id),
            )
            claimed: list[ExecutionQueueMessage] = []
            for message in ready:
                if message.status != ExecutionQueueStatus.READY:
                    continue
                if _parse(message.available_at) > current:
                    continue
                claimed_message = message.model_copy(
                    update={
                        "status": ExecutionQueueStatus.CLAIMED,
                        "claimed_by": worker_id,
                        "claim_until": _iso(current + timedelta(seconds=lease_seconds)),
                        "attempt": message.attempt + 1,
                        "updated_at": _iso(current),
                    },
                    deep=True,
                )
                self._messages[message.key] = claimed_message
                claimed.append(claimed_message.model_copy(deep=True))
                if limit is not None and len(claimed) >= limit:
                    break
            return claimed

    def ack(self, message_id: str, *, worker_id: str) -> ExecutionQueueMessage | None:
        return self._finish(message_id, ExecutionQueueStatus.ACKED, worker_id=worker_id)

    def fail(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> ExecutionQueueMessage | None:
        with self._lock:
            message = self._claimed_by(message_id, worker_id)
            if message is None:
                return None
            failed = message.model_copy(
                update={
                    "status": ExecutionQueueStatus.FAILED,
                    "claimed_by": None,
                    "claim_until": None,
                    "last_error": str(error),
                    "updated_at": _iso(self._now()),
                },
                deep=True,
            )
            self._messages[message.key] = failed
            return failed.model_copy(deep=True)

    def release(self, message_id: str, *, worker_id: str) -> ExecutionQueueMessage | None:
        with self._lock:
            message = self._claimed_by(message_id, worker_id)
            if message is None:
                return None
            released = message.model_copy(
                update={
                    "status": ExecutionQueueStatus.READY,
                    "claimed_by": None,
                    "claim_until": None,
                    "updated_at": _iso(self._now()),
                },
                deep=True,
            )
            self._messages[message.key] = released
            return released.model_copy(deep=True)

    def release_deferred(
        self,
        message_id: str,
        *,
        worker_id: str,
        delay_seconds: float = 1.0,
    ) -> ExecutionQueueMessage | None:
        """Release a claim but delay its next availability.

        Used when a message cannot run yet (resource conflict, missing
        credential): pushing ``available_at`` forward prevents the claim /
        release busy-loop and gives competing messages a chance (design goal
        0813 — no starvation)."""
        with self._lock:
            message = self._claimed_by(message_id, worker_id)
            if message is None:
                return None
            delayed = message.model_copy(
                update={
                    "status": ExecutionQueueStatus.READY,
                    "claimed_by": None,
                    "claim_until": None,
                    "available_at": _iso(
                        self._now() + timedelta(seconds=max(0.0, delay_seconds))
                    ),
                    "attempt": message.attempt + 1,
                    "updated_at": _iso(self._now()),
                },
                deep=True,
            )
            self._messages[message.key] = delayed
            return delayed.model_copy(deep=True)

    def list(self) -> list[ExecutionQueueMessage]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._messages.values()]

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()

    def _finish(
        self,
        message_id: str,
        status: ExecutionQueueStatus,
        *,
        worker_id: str,
    ) -> ExecutionQueueMessage | None:
        with self._lock:
            message = self._claimed_by(message_id, worker_id)
            if message is None:
                return None
            finished = message.model_copy(
                update={
                    "status": status,
                    "claimed_by": None,
                    "claim_until": None,
                    "updated_at": _iso(self._now()),
                },
                deep=True,
            )
            self._messages[message.key] = finished
            return finished.model_copy(deep=True)

    def _claimed_by(self, message_id: str, worker_id: str) -> ExecutionQueueMessage | None:
        message = self.get(message_id)
        if (
            message is None
            or message.status != ExecutionQueueStatus.CLAIMED
            or message.claimed_by != worker_id
        ):
            return None
        return message

    def _reclaim_expired(self, now: datetime) -> None:
        for message in list(self._messages.values()):
            if (
                message.status == ExecutionQueueStatus.CLAIMED
                and message.claim_until is not None
                and _parse(message.claim_until) <= now
            ):
                self._messages[message.key] = message.model_copy(
                    update={
                        "status": ExecutionQueueStatus.READY,
                        "claimed_by": None,
                        "claim_until": None,
                        "updated_at": _iso(now),
                    },
                    deep=True,
                )


class PostgresExecutionQueue:
    """SQLAlchemy-backed queue using the Runtime persistence database."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def enqueue(
        self,
        execution_id: str | ExecutionQueueMessage,
        *,
        trace_id: str = "",
        payload: dict[str, Any] | None = None,
        requeue: bool = False,
    ) -> ExecutionQueueMessage:
        message = _coerce_message(
            execution_id,
            trace_id=trace_id,
            payload=payload,
            now=_now(),
        )
        existing = self.get_by_execution_id(message.execution_id)
        if existing is not None:
            if requeue and existing.status in {
                ExecutionQueueStatus.ACKED,
                ExecutionQueueStatus.FAILED,
                ExecutionQueueStatus.CLAIMED,
            }:
                now = _now()
                with self._connect() as conn:
                    conn.execute(
                        sa.update(execution_queue_messages)
                        .where(
                            execution_queue_messages.c.message_id == existing.message_id,
                            execution_queue_messages.c.status.in_(
                                [
                                    ExecutionQueueStatus.ACKED.value,
                                    ExecutionQueueStatus.FAILED.value,
                                    ExecutionQueueStatus.CLAIMED.value,
                                ]
                            ),
                        )
                        .values(
                            status=ExecutionQueueStatus.READY.value,
                            claimed_by=None,
                            claim_until=None,
                            available_at=_iso(now),
                            last_error="",
                            payload=dict(payload or existing.payload),
                            updated_at=_iso(now),
                        )
                    )
                refreshed = self.get(existing.message_id)
                if refreshed is not None:
                    return refreshed
            return existing
        try:
            with self._connect() as conn:
                conn.execute(sa.insert(execution_queue_messages).values(**_message_values(message)))
        except sa.exc.IntegrityError:
            existing = self.get_by_execution_id(message.execution_id)
            if existing is not None:
                return existing
            raise
        return message.model_copy(deep=True)

    def get(self, message_id: str) -> ExecutionQueueMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(execution_queue_messages).where(
                    execution_queue_messages.c.message_id == message_id
                )
            ).mappings().first()
        return _to_message(row) if row is not None else None

    def get_by_execution_id(self, execution_id: str) -> ExecutionQueueMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(execution_queue_messages).where(
                    execution_queue_messages.c.execution_id == execution_id
                )
            ).mappings().first()
        return _to_message(row) if row is not None else None

    def claim(
        self,
        now: datetime,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int | None = None,
    ) -> list[ExecutionQueueMessage]:
        current = now.astimezone(UTC)
        now_text = _iso(current)
        claim_until = _iso(current + timedelta(seconds=lease_seconds))
        claimed: list[ExecutionQueueMessage] = []
        with self._connect() as conn:
            conn.execute(
                sa.update(execution_queue_messages)
                .where(
                    execution_queue_messages.c.status == ExecutionQueueStatus.CLAIMED.value,
                    execution_queue_messages.c.claim_until.is_not(None),
                    execution_queue_messages.c.claim_until <= now_text,
                )
                .values(
                    status=ExecutionQueueStatus.READY.value,
                    claimed_by=None,
                    claim_until=None,
                    updated_at=now_text,
                )
            )
            rows = conn.execute(
                sa.select(execution_queue_messages)
                .where(
                    execution_queue_messages.c.status == ExecutionQueueStatus.READY.value,
                    execution_queue_messages.c.available_at <= now_text,
                )
                .order_by(
                    execution_queue_messages.c.available_at,
                    execution_queue_messages.c.created_at,
                    execution_queue_messages.c.message_id,
                )
            ).mappings().all()
            for row in rows:
                result = conn.execute(
                    sa.update(execution_queue_messages)
                    .where(
                        execution_queue_messages.c.message_id == row["message_id"],
                        execution_queue_messages.c.status == ExecutionQueueStatus.READY.value,
                    )
                    .values(
                        status=ExecutionQueueStatus.CLAIMED.value,
                        claimed_by=worker_id,
                        claim_until=claim_until,
                        attempt=int(row["attempt"]) + 1,
                        updated_at=now_text,
                    )
                )
                if not result.rowcount:
                    continue
                data = dict(row)
                data.update(
                    status=ExecutionQueueStatus.CLAIMED.value,
                    claimed_by=worker_id,
                    claim_until=claim_until,
                    attempt=int(row["attempt"]) + 1,
                    updated_at=now_text,
                )
                claimed.append(_to_message(data))
                if limit is not None and len(claimed) >= limit:
                    break
        return claimed

    def ack(self, message_id: str, *, worker_id: str) -> ExecutionQueueMessage | None:
        return self._finish(message_id, ExecutionQueueStatus.ACKED, worker_id=worker_id)

    def fail(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> ExecutionQueueMessage | None:
        return self._finish(
            message_id,
            ExecutionQueueStatus.FAILED,
            worker_id=worker_id,
            error=str(error),
        )

    def release(self, message_id: str, *, worker_id: str) -> ExecutionQueueMessage | None:
        return self._finish(message_id, ExecutionQueueStatus.READY, worker_id=worker_id)

    def release_deferred(
        self,
        message_id: str,
        *,
        worker_id: str,
        delay_seconds: float = 1.0,
    ) -> ExecutionQueueMessage | None:
        """Release a claim and delay the next availability (backoff)."""
        values: dict[str, Any] = {
            "status": ExecutionQueueStatus.READY.value,
            "claimed_by": None,
            "claim_until": None,
            "available_at": _iso(
                _now() + timedelta(seconds=max(0.0, delay_seconds))
            ),
            "attempt": execution_queue_messages.c.attempt + 1,
            "updated_at": _iso(_now()),
        }
        with self._connect() as conn:
            result = conn.execute(
                sa.update(execution_queue_messages)
                .where(
                    execution_queue_messages.c.message_id == message_id,
                    execution_queue_messages.c.status == ExecutionQueueStatus.CLAIMED.value,
                    execution_queue_messages.c.claimed_by == worker_id,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return None
        return self.get(message_id)

    def list(self) -> list[ExecutionQueueMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(execution_queue_messages).order_by(
                    execution_queue_messages.c.created_at,
                    execution_queue_messages.c.message_id,
                )
            ).mappings().all()
        return [_to_message(row) for row in rows]

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute(sa.delete(execution_queue_messages))

    def _finish(
        self,
        message_id: str,
        status: ExecutionQueueStatus,
        *,
        worker_id: str,
        error: str = "",
    ) -> ExecutionQueueMessage | None:
        values: dict[str, Any] = {
            "status": status.value,
            "claimed_by": None,
            "claim_until": None,
            "updated_at": _iso(_now()),
        }
        if error:
            values["last_error"] = error
        with self._connect() as conn:
            result = conn.execute(
                sa.update(execution_queue_messages)
                .where(
                    execution_queue_messages.c.message_id == message_id,
                    execution_queue_messages.c.status == ExecutionQueueStatus.CLAIMED.value,
                    execution_queue_messages.c.claimed_by == worker_id,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return None
        return self.get(message_id)


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


def _coerce_message(
    value: str | ExecutionQueueMessage,
    *,
    trace_id: str,
    payload: dict[str, Any] | None,
    now: datetime,
) -> ExecutionQueueMessage:
    if isinstance(value, ExecutionQueueMessage):
        return value.model_copy(deep=True)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("execution_id must be a non-empty string")
    return ExecutionQueueMessage(
        execution_id=value,
        trace_id=trace_id,
        payload=dict(payload or {}),
        created_at=_iso(now),
        available_at=_iso(now),
        updated_at=_iso(now),
    )


def _message_values(message: ExecutionQueueMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "execution_id": message.execution_id,
        "created_at": message.created_at,
        "available_at": message.available_at,
        "attempt": message.attempt,
        "trace_id": message.trace_id,
        "status": message.status.value,
        "claimed_by": message.claimed_by,
        "claim_until": message.claim_until,
        "last_error": message.last_error,
        "payload": message.payload,
        "updated_at": message.updated_at,
    }


def _to_message(row: Any) -> ExecutionQueueMessage:
    data = dict(row)
    data["status"] = ExecutionQueueStatus(data["status"])
    return ExecutionQueueMessage.model_validate(data)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def recover_unqueued_executions(repository: Any, queue: ExecutionQueueProtocol) -> int:
    """Re-publish durable executions whose queue write was interrupted.

    The dispatch envelope is a sanitized snapshot in the existing first-step
    checkpoint. Only non-terminal executions with no queue row are eligible;
    waiting human/approval states remain under their existing control path.
    """

    recovered = 0
    for persisted_execution in repository.list_all():
        execution_status = str(
            getattr(
                getattr(persisted_execution, "status", None),
                "value",
                getattr(persisted_execution, "status", ""),
            )
            or ""
        ).upper()
        if execution_status not in {"PENDING", "RUNNING"}:
            continue
        execution_id = str(getattr(persisted_execution, "execution_id", "") or "")
        if not execution_id:
            continue
        if queue.get_by_execution_id(execution_id) is not None:
            continue
        dispatch_payload = None
        for step in getattr(persisted_execution, "steps", ()) or ():
            candidate = dict(getattr(step, "checkpoint_data", {}) or {}).get(
                "dispatch_payload"
            )
            if isinstance(candidate, dict):
                dispatch_payload = candidate
                break
        if not dispatch_payload:
            continue
        queue.enqueue(
            execution_id,
            trace_id=str(dispatch_payload.get("trace_id") or execution_id),
            payload=dispatch_payload,
        )
        recovered += 1
    return recovered


__all__ = [
    "ExecutionQueue",
    "ExecutionQueueMessage",
    "ExecutionQueueProtocol",
    "ExecutionQueueStatus",
    "PostgresExecutionQueue",
    "recover_unqueued_executions",
]
