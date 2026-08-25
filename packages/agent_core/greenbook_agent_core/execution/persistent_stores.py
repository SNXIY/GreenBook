"""SQLAlchemy-backed event and checkpoint stores."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from greenbook_agent_core.observability.context import TraceContext

from .checkpoint import ExecutionCheckpoint
from .events import EventType, ExecutionEvent
from .evidence import ExecutionEvidence
from .operation_tracking import ExternalOperationRecord, OperationStatus
from .persistence import (
    checkpoints,
    execution_events,
    execution_metadata,
    external_operations,
)


def _parse_operation_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
            _migrate_external_operations(bind)

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

    def claim(
        self,
        operation_id: str,
        *,
        expected_status: OperationStatus,
        new_status: OperationStatus,
        owner: str,
        lease_expires_at: str = "",
    ) -> ExternalOperationRecord | None:
        """Atomically claim an operation only if it is still in the expected
        state, bumping the version as a fencing token (row-level CAS).
        """
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            current = conn.execute(
                sa.select(external_operations)
                .where(external_operations.c.operation_id == operation_id)
            ).mappings().first()
            if current is None or current["status"] != expected_status.value:
                return None
            attempt = int(current["attempt"] or 0) + 1
            version = int(current["claim_version"] or 0) + 1
            conn.execute(
                sa.update(external_operations)
                .where(external_operations.c.operation_id == operation_id)
                .values(
                    status=new_status.value,
                    attempt=attempt,
                    claim_owner=owner,
                    claim_version=version,
                    fencing_token=f"{version}:{owner}",
                    lease_expires_at=lease_expires_at or str(current.get("lease_expires_at") or ""),
                    updated_at=now,
                )
            )
        return self.get(operation_id)

    def reclaim_expired_lease(
        self,
        operation_id: str,
        *,
        owner: str,
        now: str,
    ) -> ExternalOperationRecord | None:
        """Reclaim a RUNNING operation whose lease expired, only when no side
        effect has started (side_effect_started false).  Advances the fencing
        token so the stale worker is rejected.  A side effect that may have
        reached Java is never reclaimed as a fresh write.
        """
        with self._connect() as conn:
            current = conn.execute(
                sa.select(external_operations)
                .where(external_operations.c.operation_id == operation_id)
            ).mappings().first()
            if current is None:
                return None
            if str(current["status"] or "") != OperationStatus.RUNNING.value:
                return None
            if bool(current.get("side_effect_started")):
                return None
            expires = _parse_operation_ts(current.get("lease_expires_at"))
            if expires is None or expires > _parse_operation_ts(now):
                return None
            attempt = int(current["attempt"] or 0) + 1
            version = int(current["claim_version"] or 0) + 1
            conn.execute(
                sa.update(external_operations)
                .where(external_operations.c.operation_id == operation_id)
                .values(
                    attempt=attempt,
                    claim_owner=owner,
                    claim_version=version,
                    fencing_token=f"{version}:{owner}",
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        return self.get(operation_id)

    def complete_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        status: OperationStatus,
        evidence: ExecutionEvidence | None = None,
        external_operation_id: str | None = None,
        receipt_id: str | None = None,
        verified_status: str = "",
        verified_reason: str = "",
    ) -> ExternalOperationRecord | None:
        """Fenced terminal write: reject a stale worker whose version is older."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            current = conn.execute(
                sa.select(external_operations)
                .where(external_operations.c.operation_id == operation_id)
            ).mappings().first()
            if current is None or int(current["claim_version"] or 0) != expected_version:
                return None
            version = int(current["claim_version"] or 0) + 1
            values: dict[str, Any] = {
                "status": status.value,
                "claim_version": version,
                "fencing_token": f"{version}:{current['claim_owner'] or ''}",
                "updated_at": now,
                "reconciliation_needed": False,
                "verified_status": verified_status or str(current.get("verified_status") or ""),
                "verified_reason": verified_reason or str(current.get("verified_reason") or ""),
            }
            if evidence is not None:
                values["evidence"] = evidence.model_dump(mode="json")
            if external_operation_id is not None:
                values["external_operation_id"] = external_operation_id
            if receipt_id is not None:
                values["receipt_id"] = receipt_id
            conn.execute(
                sa.update(external_operations)
                .where(external_operations.c.operation_id == operation_id)
                .values(**values)
            )
        return self.get(operation_id)

    def save_if_version(
        self,
        operation_id: str,
        *,
        expected_version: int,
        updates: dict[str, Any],
    ) -> ExternalOperationRecord | None:
        """Fenced partial update: a stale worker (older version) is rejected."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                sa.update(external_operations)
                .where(
                    external_operations.c.operation_id == operation_id,
                    external_operations.c.claim_version == expected_version,
                )
                .values(**updates, updated_at=now)
            )
            if result.rowcount == 0:
                return None
        return self.get(operation_id)

    def find_reconciliation_needed(
        self,
        *,
        now: str = "",
        limit: int = 50,
    ) -> list[ExternalOperationRecord]:
        with self._connect() as conn:
            conditions = [external_operations.c.reconciliation_needed.is_(True)]
            if now:
                conditions.append(
                    sa.or_(
                        external_operations.c.next_reconcile_at == "",
                        external_operations.c.next_reconcile_at.is_(None),
                        external_operations.c.next_reconcile_at <= now,
                    )
                )
            rows = conn.execute(
                sa.select(external_operations)
                .where(sa.and_(*conditions))
                .order_by(external_operations.c.created_at)
                .limit(max(1, int(limit)))
            ).mappings().all()
        return [_to_operation(row) for row in rows]


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


_EXTERNAL_OPERATION_COLUMNS = (
    "trace_id VARCHAR(128) DEFAULT ''",
    "conversation_id VARCHAR(128) DEFAULT ''",
    "semantic_action VARCHAR(128) DEFAULT ''",
    "resource_type VARCHAR(64) DEFAULT ''",
    "resource_id VARCHAR(256) DEFAULT ''",
    "expected_postcondition JSONB DEFAULT '{}'::jsonb",
    "attempt INTEGER DEFAULT 0",
    "claim_owner VARCHAR(256) DEFAULT ''",
    "claim_version INTEGER DEFAULT 0",
    "fencing_token VARCHAR(256) DEFAULT ''",
    "lease_expires_at VARCHAR(64) DEFAULT ''",
    "side_effect_started BOOLEAN DEFAULT FALSE",
    "reconciliation_needed BOOLEAN DEFAULT FALSE",
    "retry_classification VARCHAR(32) DEFAULT ''",
    "reconcile_attempts INTEGER DEFAULT 0",
    "verified_status VARCHAR(32) DEFAULT ''",
    "verified_reason VARCHAR(512) DEFAULT ''",
    "next_reconcile_at VARCHAR(64) DEFAULT ''",
)


def _migrate_external_operations(bind: Any) -> None:
    """Additively migrate the durable operation table to the current schema.

    ``create_all`` only creates missing tables; a pre-existing ``external_operation``
    table needs additive columns so OperationLedger inserts do not fail on a
    running deployment.  SQLite is skipped (its ALTER syntax differs; tests use a
    fresh schema via ``create_all``).
    """
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "") or "")
    if dialect not in {"postgresql", "pg"}:
        return
    with bind.begin() as conn:
        for column in _EXTERNAL_OPERATION_COLUMNS:
            conn.execute(
                sa.text(f"ALTER TABLE external_operation ADD COLUMN IF NOT EXISTS {column}")
            )


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
        "trace_id": record.trace_id,
        "conversation_id": record.conversation_id,
        "semantic_action": record.semantic_action,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "expected_postcondition": dict(record.expected_postcondition or {}),
        "attempt": record.attempt,
        "claim_owner": record.claim_owner,
        "claim_version": record.claim_version,
        "fencing_token": record.fencing_token,
        "lease_expires_at": record.lease_expires_at,
        "side_effect_started": bool(record.side_effect_started),
        "reconciliation_needed": bool(record.reconciliation_needed),
        "retry_classification": record.retry_classification,
        "reconcile_attempts": record.reconcile_attempts,
        "verified_status": record.verified_status,
        "verified_reason": record.verified_reason,
        "next_reconcile_at": record.next_reconcile_at,
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
