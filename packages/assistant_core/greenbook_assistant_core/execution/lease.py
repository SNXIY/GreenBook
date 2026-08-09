"""Execution lease used to prevent duplicate workers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

import sqlalchemy as sa

from pydantic import BaseModel

from .persistence import execution_leases, execution_metadata


class ExecutionLease(BaseModel):
    execution_id: str
    worker_id: str
    lease_until: str


class ExecutionLeaseManager:
    def __init__(self) -> None:
        self._leases: dict[str, ExecutionLease] = {}
        self._lock = RLock()

    def acquire(self, execution_id: str, worker_id: str, ttl_seconds: int = 30) -> bool:
        with self._lock:
            current = self._leases.get(execution_id)
            if current and _parse(current.lease_until) > datetime.now(UTC):
                return current.worker_id == worker_id
            self._leases[execution_id] = _lease(execution_id, worker_id, ttl_seconds)
            return True

    def renew(self, execution_id: str, worker_id: str, ttl_seconds: int = 30) -> bool:
        with self._lock:
            current = self._leases.get(execution_id)
            if not current or current.worker_id != worker_id:
                return False
            if _parse(current.lease_until) <= datetime.now(UTC):
                return False
            self._leases[execution_id] = _lease(execution_id, worker_id, ttl_seconds)
            return True

    def release(self, execution_id: str, worker_id: str) -> bool:
        with self._lock:
            current = self._leases.get(execution_id)
            if not current or current.worker_id != worker_id:
                return False
            del self._leases[execution_id]
            return True

    def get(self, execution_id: str) -> ExecutionLease | None:
        with self._lock:
            lease = self._leases.get(execution_id)
            return lease.model_copy(deep=True) if lease else None


class PostgresExecutionLeaseManager:
    """Database-backed lease manager using row-level locking."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            execution_metadata.create_all(bind)

    def acquire(self, execution_id: str, worker_id: str, ttl_seconds: int = 30) -> bool:
        with self._bind.begin() as conn:
            row = conn.execute(
                sa.select(execution_leases)
                .where(execution_leases.c.execution_id == execution_id)
                .with_for_update()
            ).mappings().first()
            now = datetime.now(UTC)
            if row and _parse(row["lease_until"]) > now and row["worker_id"] != worker_id:
                return False
            values = {
                "execution_id": execution_id,
                "worker_id": worker_id,
                "lease_until": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            }
            if row:
                conn.execute(sa.update(execution_leases)
                             .where(execution_leases.c.execution_id == execution_id)
                             .values(**values))
            else:
                conn.execute(sa.insert(execution_leases).values(**values))
            return True

    def renew(self, execution_id: str, worker_id: str, ttl_seconds: int = 30) -> bool:
        with self._bind.begin() as conn:
            row = conn.execute(sa.select(execution_leases).where(
                execution_leases.c.execution_id == execution_id
            ).with_for_update()).mappings().first()
            if not row or row["worker_id"] != worker_id or _parse(row["lease_until"]) <= datetime.now(UTC):
                return False
            conn.execute(sa.update(execution_leases)
                         .where(execution_leases.c.execution_id == execution_id)
                         .values(lease_until=(datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()))
            return True

    def release(self, execution_id: str, worker_id: str) -> bool:
        with self._bind.begin() as conn:
            result = conn.execute(sa.delete(execution_leases).where(
                execution_leases.c.execution_id == execution_id,
                execution_leases.c.worker_id == worker_id,
            ))
            return result.rowcount > 0


def _lease(execution_id: str, worker_id: str, ttl_seconds: int) -> ExecutionLease:
    return ExecutionLease(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_until=(datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


__all__ = ["ExecutionLease", "ExecutionLeaseManager", "PostgresExecutionLeaseManager"]
