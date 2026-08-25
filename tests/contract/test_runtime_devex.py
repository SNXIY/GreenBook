from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")


def _run_script(script: Path, env_file: Path) -> subprocess.CompletedProcess[str]:
    shell = _powershell()
    assert shell is not None
    return subprocess.run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-EnvFile",
            str(env_file),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_env(
    path: Path,
    *,
    dispatch: str = "queue",
    consumer: str = "true",
    worker_token: str = "test-worker-token",
) -> None:
    path.write_text(
        "\n".join(
            [
                "GREENBOOK_AGENT_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:25432/db",
                f"GREENBOOK_AGENT_EXECUTION_DISPATCH={dispatch}",
                f"GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER={consumer}",
                f"GREENBOOK_AGENT_WORKER_ACCESS_TOKEN={worker_token}",
                "GREENBOOK_JAVA_BASE_URL=http://127.0.0.1:8080",
            ]
        ),
        encoding="utf-8",
    )


def test_local_launcher_uses_canonical_agent_api_and_worker() -> None:
    launcher = (SCRIPTS / "start-greenbook.ps1").read_text(encoding="utf-8")
    for script in (
        "scripts\\start-be.ps1",
        "scripts\\start-agent.ps1",
        "scripts\\start-agent-worker.ps1",
        "scripts\\start-fe.ps1",
    ):
        assert script in launcher
    assert '"-ApiOnly"' in launcher
    assert "Start-Process" in launcher
    assert "check-runtime-env.ps1" in launcher

    assistant = (SCRIPTS / "start-agent.ps1").read_text(encoding="utf-8")
    assert 'DefaultValue "all"' in assistant
    assert "GREENBOOK_AGENT_IN_PROCESS_WORKER" in assistant
    assert "greenbook_agent_worker.main" not in assistant
    assert "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN is required" not in assistant
    assert 'DefaultValue "8094"' in assistant


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is required for script contract tests")
def test_runtime_env_check_accepts_durable_queue_profile(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file)
    result = _run_script(SCRIPTS / "check-runtime-env.ps1", env_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Environment check: READY" in result.stdout


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is required for script contract tests")
def test_runtime_env_check_accepts_direct_dispatch_without_worker_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, dispatch="direct", consumer="false", worker_token="")
    result = _run_script(SCRIPTS / "check-runtime-env.ps1", env_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Worker token: NOT REQUIRED" in result.stdout


def test_runtime_status_distinguishes_worker_and_queue() -> None:
    status = (SCRIPTS / "check-runtime-status.ps1").read_text(encoding="utf-8")
    assert "Worker:" in status
    assert "Queue:" in status
    assert "NOT REQUIRED" in status
    assert "$dispatchReady" in status


def test_frontend_agent_proxy_uses_canonical_runtime_target() -> None:
    frontend_launcher = (SCRIPTS / "start-fe.ps1").read_text(encoding="utf-8")
    vite_config = (ROOT / "zhiguang-fe" / "vite.config.ts").read_text(encoding="utf-8")

    assert "VITE_GREENBOOK_AGENT_PROXY_TARGET" in frontend_launcher
    assert 'DefaultValue "8094"' in frontend_launcher
    assert "VITE_GREENBOOK_AGENT_PROXY_TARGET" in vite_config
    assert '"http://127.0.0.1:8094"' in vite_config
    assert "8095" not in frontend_launcher + vite_config
