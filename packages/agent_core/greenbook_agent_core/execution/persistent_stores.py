"""SQLAlchemy-backed event and checkpoint stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from greenbook_agent_core.observability.context import TraceContext

from .checkpoint import ExecutionCheckpoint
from .events import EventType, ExecutionEvent
from .operation_tracking import ExternalOperationRecord, OperationStatus
from .persistence import (
    checkpoints,
    execution_events,
    execution_metadata,
    external_operations,
)


class PostgresExecutionEventStore:
    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        with self._bind.begin() as conn:
            payload = dict(event.payload)
            if event.trace_context is not None:
                payload.setdefault(
                    "trace_context",
                    event.trace_context.model_dump(mode="json"),
                )
            conn.execute(sa.insert(execution_events).values(
                event_id=event.event_id,
                execution_id=event.execution_id,
                event_type=event.event_type.value,
                step_id=event.step_id,
                payload=payload,
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
        events: list[ExecutionEvent] = []
        for row in rows:
            payload = row["payload"] or {}
            raw_context = payload.get("trace_context") if isinstance(payload, dict) else None
            trace_context = None
            if raw_context is not None:
                try:
                    trace_context = TraceContext.model_validate(raw_context)
                except (TypeError, ValueError):
                    trace_context = None
            events.append(ExecutionEvent(
                event_id=row["event_id"],
                execution_id=row["execution_id"],
                event_type=EventType(row["event_type"]),
                step_id=row["step_id"],
                timestamp=row["created_at"],
                payload=payload,
                trace_context=trace_context,
            ))
        return events

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


class PostgresExternalOperationStore:
    """SQLAlchemy-backed store for Runtime external operation records.

    The adapter accepts the same synchronous SQLAlchemy bind as the existing
    execution/event/checkpoint adapters. SQLite remains useful for contract
    tests; PostgreSQL is the intended production backend.
    """

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def create(self, record: ExternalOperationRecord) -> ExternalOperationRecord:
        """Create or idempotently preserve one logical operation record."""

        return self.save(record)

    def save(self, record: ExternalOperationRecord) -> ExternalOperationRecord:
        values = _operation_values(record)
        with self._connect() as conn:
            exists = conn.execute(
                sa.select(external_operations.c.operation_id).where(
                    external_operations.c.operation_id == record.operation_id
                )
            ).first()
            if exists:
                conn.execute(
                    sa.update(external_operations)
                    .where(
                        external_operations.c.operation_id == record.operation_id
                    )
                    .values(**values)
                )
            else:
                conn.execute(sa.insert(external_operations).values(**values))
        return record.model_copy(deep=True)

    def get(self, operation_id: str) -> ExternalOperationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(external_operations).where(
                    external_operations.c.operation_id == operation_id
                )
            ).mappings().first()
        return _to_operation(row) if row is not None else None

    def update_status(
        self,
        operation_id: str,
        status: OperationStatus,
    ) -> ExternalOperationRecord | None:
        with self._connect() as conn:
            result = conn.execute(
                sa.update(external_operations)
                .where(external_operations.c.operation_id == operation_id)
                .values(
                    status=status.value,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            if result.rowcount == 0:
                return None
        return self.get(operation_id)

    def find(
        self,
        *,
        execution_id: str | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ExternalOperationRecord | None:
        conditions: list[Any] = []
        if execution_id is not None:
            conditions.append(external_operations.c.execution_id == execution_id)
        identities: list[Any] = []
        if external_operation_id is not None:
            identities.append(
                external_operations.c.external_operation_id == external_operation_id
            )
        if receipt_id is not None:
            identities.append(external_operations.c.receipt_id == receipt_id)
        if not identities and execution_id is None:
            return None
        if identities:
            conditions.append(sa.or_(*identities))
        with self._connect() as conn:
            row = conn.execute(
                sa.select(external_operations)
                .where(sa.and_(*conditions))
                .order_by(external_operations.c.updated_at.desc())
            ).mappings().first()
        return _to_operation(row) if row is not None else None

    def find_by_execution_id(self, execution_id: str) -> list[ExternalOperationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(external_operations)
                .where(external_operations.c.execution_id == execution_id)
                .order_by(external_operations.c.created_at, external_operations.c.operation_id)
            ).mappings().all()
        return [_to_operation(row) for row in rows]

    def find_by_external_operation_id(
        self,
        external_operation_id: str,
    ) -> ExternalOperationRecord | None:
        return self.find(external_operation_id=external_operation_id)

    def find_by_receipt_id(self, receipt_id: str) -> ExternalOperationRecord | None:
        return self.find(receipt_id=receipt_id)

    def list(self) -> list[ExternalOperationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(external_operations)
                .order_by(external_operations.c.created_at, external_operations.c.operation_id)
            ).mappings().all()
        return [_to_operation(row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(external_operations)).scalar_one())

    def clear(self, execution_id: str | None = None) -> None:
        with self._connect() as conn:
            statement = sa.delete(external_operations)
            if execution_id is not None:
                statement = statement.where(
                    external_operations.c.execution_id == execution_id
                )
            conn.execute(statement)


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


def _operation_values(record: ExternalOperationRecord) -> dict[str, Any]:
    return {
        "operation_id": record.operation_id,
        "execution_id": record.execution_id,
        "step_id": record.step_id,
        "tool_name": record.tool_name,
        "status": record.status.value,
        "external_operation_id": record.external_operation_id,
        "receipt_id": record.receipt_id,
        "idempotency_key": record.idempotency_key,
        "runtime_idempotency_key": record.runtime_idempotency_key,
        "external_idempotency_key": record.external_idempotency_key,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "evidence": (
            record.evidence.model_dump(mode="json")
            if record.evidence is not None
            else None
        ),
    }


def _to_operation(row: Any) -> ExternalOperationRecord:
    data = dict(row)
    if data.get("evidence") is not None:
        data["evidence"] = dict(data["evidence"])
    return ExternalOperationRecord.model_validate(data)


__all__ = [
    "PostgresExecutionEventStore",
    "PostgresCheckpointStore",
    "PostgresExternalOperationStore",
]
