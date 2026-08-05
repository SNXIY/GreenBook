import json
import subprocess
import sys
import time

import run_p0_e2e as harness


def test_timeout_budgets_are_derived_from_creator_configuration() -> None:
    assert harness.DEFAULT_CREATOR_HARD_TIMEOUT == 2460
    assert harness.DEFAULT_ASSISTANT_RUN_HARD_TIMEOUT == 3060
    assert harness.DEFAULT_STALL_TIMEOUT == 180


def test_deadline_uses_monotonic_clock(monkeypatch) -> None:
    clock = iter((106.0,))
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(clock))
    deadline = harness.Deadline(5, started=100.0)

    try:
        deadline.check("test")
    except harness.HarnessTimeout as exc:
        assert exc.code == "GLOBAL_HARD_TIMEOUT"
    else:
        raise AssertionError("monotonic deadline did not expire")


def test_manifest_update_is_atomic_and_valid_json(tmp_path) -> None:
    manifest = harness.Manifest(tmp_path, run_id="run-1", timeouts={"global_hard_timeout": 10})
    manifest.update("CREATOR_STARTING", creator={"pid": 123})

    data = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert data["harness_run_id"] == "run-1"
    assert data["last_stage"] == "CREATOR_STARTING"
    assert not (tmp_path / "manifest.tmp").exists()


def test_child_log_is_available_before_process_exit(tmp_path, capsys) -> None:
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "print('CHILD_READY', flush=True); time.sleep(0.2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    manifest = harness.Manifest(tmp_path, run_id="run-2", timeouts={})
    pump = harness.LogPump(process, [tmp_path / "child.log"], manifest.log)
    time.sleep(0.05)
    assert "CHILD_READY" in (tmp_path / "child.log").read_text(encoding="utf-8")
    process.wait(timeout=5)
    pump.join()
    assert "CHILD_READY" in capsys.readouterr().out


def test_redaction_does_not_emit_credentials() -> None:
    value = harness.redact(
        "password=secret Authorization: Bearer jwt-value access_token=abc refresh_token=xyz"
    )
    assert "secret" not in value
    assert "jwt-value" not in value
    assert "abc" not in value
    assert "xyz" not in value


def test_manifest_sanitizes_nested_evidence(tmp_path) -> None:
    manifest = harness.Manifest(tmp_path, run_id="run-3", timeouts={})
    manifest.update("EVIDENCE_COLLECTED", evidence={"headers": {"Authorization": "Bearer secret"}})

    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert "Bearer secret" not in text
    assert "<REDACTED>" in text
