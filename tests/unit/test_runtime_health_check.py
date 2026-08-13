"""Configuration-only tests for the Runtime health check script."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime-health-check"


def test_runtime_health_check_reports_configured_components_without_network(tmp_path) -> None:
    env = os.environ.copy()
    env.update({
        "GREENBOOK_AGENT_RUNTIME_STORAGE": "postgres",
        "GREENBOOK_AGENT_DATABASE_URL": "postgresql+asyncpg://user:pass@db.example:5432/runtime",
        "GREENBOOK_AGENT_WORKER_HEALTH_FILE": str(tmp_path / "worker-health.json"),
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-only", "--env-file", str(tmp_path / "missing.env")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "PostgreSQL: READY" in result.stdout
    assert "Agent API: READY" in result.stdout
    assert "Agent Worker: READY" in result.stdout
    assert "Creator: READY" in result.stdout
    assert "Java Backend: READY" in result.stdout
    assert "Overall: READY" in result.stdout


def test_runtime_health_check_rejects_memory_production_profile(tmp_path) -> None:
    env = os.environ.copy()
    env.update({
        "GREENBOOK_AGENT_RUNTIME_STORAGE": "memory",
        "GREENBOOK_AGENT_WORKER_HEALTH_FILE": str(tmp_path / "worker-health.json"),
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-only", "--env-file", str(tmp_path / "missing.env")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PostgreSQL: NOT_READY" in result.stdout
    assert "Overall: NOT_READY" in result.stdout


def test_runtime_health_check_matches_database_url_auto_selection(tmp_path) -> None:
    env = os.environ.copy()
    for name in ("GREENBOOK_AGENT_RUNTIME_DATABASE_URL", "GREENBOOK_AGENT_DB_URL", "GREENBOOK_DB_URL"):
        env.pop(name, None)
    env.update({
        "GREENBOOK_AGENT_RUNTIME_STORAGE": "",
        "GREENBOOK_AGENT_DATABASE_URL": "postgresql://user:pass@db.example/runtime",
        "GREENBOOK_AGENT_WORKER_HEALTH_FILE": str(tmp_path / "worker-health.json"),
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-only", "--env-file", str(tmp_path / "missing.env")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "PostgreSQL: READY" in result.stdout
