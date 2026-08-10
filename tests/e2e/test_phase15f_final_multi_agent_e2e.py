"""Authenticated Phase15-F E2E gate.

This test deliberately skips as BLOCKED_BY_ENV when a dedicated USER account
or access token is not configured. It never replaces Java/Creator with mocks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.e2e
def test_phase15f_final_multi_agent_e2e_uses_real_services() -> None:
    configured = (
        os.getenv("GREENBOOK_E2E_ACCESS_TOKEN")
        or (os.getenv("GREENBOOK_E2E_IDENTIFIER") and os.getenv("GREENBOOK_E2E_PASSWORD"))
    )
    if not configured:
        pytest.skip("BLOCKED_BY_ENV: dedicated real USER credentials are not configured")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run-agent-evaluation.py"), "--output", str(ROOT / ".runtime" / "phase15f-test.json")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=4_800,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Status: COMPLETED" in result.stdout
    assert "BLOCKED_BY_ENV" not in result.stdout
