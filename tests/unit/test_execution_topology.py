from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from greenbook_agent_core.execution.topology import (
    ExecutionTopologyError,
    standalone_worker_active,
    validate_single_consumer,
)


def _write_health(path, *, updated_at: datetime, queue_consumer: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "READY",
                "updated_at": updated_at.isoformat(),
                "queue_consumer": queue_consumer,
            }
        ),
        encoding="utf-8",
    )


def test_recent_standalone_worker_health_is_active(tmp_path) -> None:
    now = datetime.now(UTC)
    health = tmp_path / "worker.json"
    _write_health(health, updated_at=now - timedelta(seconds=2))

    assert standalone_worker_active(health, now=now, max_age_seconds=30) is True


def test_stale_or_non_consumer_health_is_ignored(tmp_path) -> None:
    now = datetime.now(UTC)
    health = tmp_path / "worker.json"
    _write_health(health, updated_at=now - timedelta(seconds=60))
    assert standalone_worker_active(health, now=now, max_age_seconds=30) is False

    _write_health(health, updated_at=now, queue_consumer=False)
    assert standalone_worker_active(health, now=now, max_age_seconds=30) is False


def test_in_process_and_standalone_consumers_are_rejected(tmp_path) -> None:
    now = datetime.now(UTC)
    health = tmp_path / "worker.json"
    _write_health(health, updated_at=now)

    with pytest.raises(ExecutionTopologyError, match="Both in-process"):
        validate_single_consumer(
            dispatch_mode="queue",
            in_process_worker=True,
            health_file=health,
            max_age_seconds=30,
        )


def test_direct_dispatch_does_not_require_a_queue_consumer(tmp_path) -> None:
    health = tmp_path / "worker.json"
    _write_health(health, updated_at=datetime.now(UTC))
    validate_single_consumer(
        dispatch_mode="direct",
        in_process_worker=True,
        health_file=health,
    )
