"""Monotonic execution-state projection for Task execution refs.

Invariant: for a single ``execution_id``, a terminal status (COMPLETED /
FAILED / CANCELLED) is a latch.  A late non-terminal (QUEUED / RUNNING /
PENDING / SUBMITTED) update must never regress it, and a later terminal
update is idempotent.  Non-terminal progression (QUEUED -> RUNNING -> ...)
keeps its existing rules.

This is the single place that computes the effective status of one execution
ref; every write boundary (TaskManager.bind_execution, TaskProvider terminal
projection) routes through it so the read model can never observe an
execution that was terminal and then became pending again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from .models import TaskExecutionRef

TERMINAL_EXECUTION_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


def is_terminal_execution_status(status: str | None) -> bool:
    return str(status or "").upper() in TERMINAL_EXECUTION_STATUSES


def merge_execution_status(current: str | None, incoming: str) -> str:
    """Return the effective status for one execution ref, monotonic in terminality.

    - terminal -> non-terminal : blocked (keep the terminal status)
    - terminal -> terminal     : idempotent (keep the first terminal status)
    - non-terminal -> any      : the incoming status (normal progression)
    """
    current = str(current or "").upper()
    if current in TERMINAL_EXECUTION_STATUSES:
        return current
    return str(incoming or "").upper()


def project_execution_ref(
    refs: Sequence[TaskExecutionRef],
    *,
    execution_id: str,
    task_id: str,
    goal_id: str | None = None,
    status: str = "",
    now: datetime | None = None,
) -> list[TaskExecutionRef]:
    """Upsert one execution ref under the monotonic guard.

    Returns a new list for the caller to persist.  An existing ref that is
    already terminal is never downgraded, and terminal -> terminal stays
    idempotent (no timestamp churn).  Duplicate refs for the same
    ``execution_id`` are merged into a single ref.
    """
    now = now or datetime.now(UTC)
    refs = list(refs)
    existing = next((r for r in refs if r.execution_id == execution_id), None)
    if existing is None:
        refs.append(
            TaskExecutionRef(
                execution_id=execution_id,
                task_id=task_id,
                goal_id=goal_id,
                status=str(status or "").upper(),
            )
        )
        return refs
    effective = merge_execution_status(existing.status, status)
    if str(existing.status).upper() != effective:
        existing.status = effective
        existing.updated_at = now.isoformat()
    if goal_id and not existing.goal_id:
        existing.goal_id = goal_id
    return refs


def existing_execution_status(
    refs: Sequence[TaskExecutionRef],
    execution_id: str,
) -> str:
    """Return the current persisted status of an execution ref, or ""."""
    for ref in refs:
        if ref.execution_id == execution_id:
            return str(ref.status or "").upper()
    return ""


__all__ = [
    "TERMINAL_EXECUTION_STATUSES",
    "existing_execution_status",
    "is_terminal_execution_status",
    "merge_execution_status",
    "project_execution_ref",
]
