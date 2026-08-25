"""Execution consumer topology guards.

The durable queue must have exactly one execution consumer per runtime
profile.  This module is deliberately small and side-effect free so startup
scripts and API lifespan tests can share the same decision.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ExecutionTopologyError(RuntimeError):
    """Raised when API and standalone queue consumers are both configured."""


def standalone_worker_active(
    health_file: str | Path | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 30.0,
) -> bool:
    """Return whether a recent standalone worker health record is present."""

    if not health_file:
        return False
    path = Path(health_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not bool(payload.get("queue_consumer")):
            return False
        if str(payload.get("status") or "").upper() not in {"STARTING", "READY"}:
            return False
        updated = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return (current - updated).total_seconds() <= max(0.0, max_age_seconds)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def validate_single_consumer(
    *,
    dispatch_mode: str,
    in_process_worker: bool,
    health_file: str | Path | None,
    max_age_seconds: float = 30.0,
) -> None:
    """Reject an API in-process worker when a standalone worker is active."""

    if str(dispatch_mode).strip().lower() != "queue" or not in_process_worker:
        return
    if standalone_worker_active(health_file, max_age_seconds=max_age_seconds):
        raise ExecutionTopologyError(
            "Both in-process and standalone execution queue consumers are active. "
            "Choose GREENBOOK_AGENT_IN_PROCESS_WORKER=true for development or "
            "run the standalone worker with it set to false."
        )


__all__ = [
    "ExecutionTopologyError",
    "standalone_worker_active",
    "validate_single_consumer",
]
