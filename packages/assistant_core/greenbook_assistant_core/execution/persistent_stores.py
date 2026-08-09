"""SQLAlchemy-backed event and checkpoint stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from .checkpoint import ExecutionCheckpoint
from .events import ExecutionEvent, EventType
from .persistence import checkpoints, execution_events, execution_metadata


class PostgresExecutionEventStore:
    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        with self._bind.begin() as conn:
            conn.execute(sa.insert(execution_events).values(
                event_id=event.event_id,
                execution_id=event.execution_id,
                event_type=event.event_type.value,
                step_id=event.step_id,
                payload=event.payload,
                created_at=event.timestamp,
            ))
        return event

    def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        with self._bind.begin() as conn:
            rows = conn.execute(
                sa.select(execution_events)
                .where(execution_events.c.execution_id == execution_id)
                .order_by(execution_events.c.created_at, execution_events.c.event_id)
            ).mappings().all()
        return [ExecutionEvent(
            event_id=row["event_id"],
            execution_id=row["execution_id"],
            event_type=EventType(row["event_type"]),
            step_id=row["step_id"],
            timestamp=row["created_at"],
            payload=row["payload"] or {},
        ) for row in rows]

    def clear(self, execution_id: str) -> None:
        with self._bind.begin() as conn:
            conn.execute(sa.delete(execution_events).where(
                execution_events.c.execution_id == execution_id
            ))


class PostgresCheckpointStore:
    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def save(self, checkpoint: ExecutionCheckpoint) -> ExecutionCheckpoint:
        now = datetime.now(UTC).isoformat()
        with self._bind.begin() as conn:
            conn.execute(sa.insert(checkpoints).values(
                execution_id=checkpoint.execution_id,
                step_id=checkpoint.current_step,
                snapshot={
                    "completed_steps": checkpoint.completed_steps,
                    "current_step": checkpoint.current_step,
                    **checkpoint.snapshot,
                },
                created_at=now,
            ))
        return checkpoint

    def latest(self, execution_id: str) -> ExecutionCheckpoint | None:
        with self._bind.begin() as conn:
            row = conn.execute(
                sa.select(checkpoints)
                .where(checkpoints.c.execution_id == execution_id)
                .order_by(checkpoints.c.checkpoint_id.desc())
            ).mappings().first()
        if row is None:
            return None
        snapshot = dict(row["snapshot"] or {})
        completed = list(snapshot.pop("completed_steps", []))
        current = snapshot.pop("current_step", row["step_id"] or "")
        return ExecutionCheckpoint(
            execution_id=execution_id,
            completed_steps=completed,
            current_step=current,
            snapshot=snapshot,
        )


__all__ = ["PostgresExecutionEventStore", "PostgresCheckpointStore"]
