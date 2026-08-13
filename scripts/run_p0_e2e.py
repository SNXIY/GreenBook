"""Observable, bounded, one-shot P0 E2E harness.

This module is test infrastructure only.  It does not alter either control
plane.  Failed runs retain their manifest, logs, SQLite and Redis namespace
by default so that evidence can be collected after a timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "WAITING_APPROVAL"}
CREATOR_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
DEFAULT_MODEL_TIMEOUT = 60
DEFAULT_SPECIALIST_TIMEOUT = 90
DEFAULT_MAX_MODEL_CALLS = 24
DEFAULT_REVISION_BUDGET = 4
DEFAULT_STALL_TIMEOUT = max(2 * DEFAULT_SPECIALIST_TIMEOUT, 180)
DEFAULT_CREATOR_HARD_TIMEOUT = (
    DEFAULT_MAX_MODEL_CALLS * max(DEFAULT_MODEL_TIMEOUT, DEFAULT_SPECIALIST_TIMEOUT) + 300
)
DEFAULT_GREENBOOK_AGENT_RUN_HARD_TIMEOUT = DEFAULT_CREATOR_HARD_TIMEOUT + 600


SERVER_CODE = r'''
import asyncio, json, os
from pathlib import Path
import uvicorn

if os.environ.get("P0_APP_KIND") == "agent":
    from greenbook_agent_api.main import create_app
    app = create_app()
else:
    from app.main import app

async def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            reload=False, workers=1, lifespan="on",
                            log_level="info")
    server = uvicorn.Server(config)
    if not config.loaded:
        config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    sock = server.servers[0].sockets[0]
    port = int(sock.getsockname()[1])
    Path(os.environ["P0_READY_FILE"]).write_text(
        json.dumps({"port": port}), encoding="utf-8"
    )
    await server.main_loop()
    await server.shutdown()

asyncio.run(main())
'''


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    """Remove credentials/tokens from child output and evidence logs."""
    patterns = (
        (r"(?i)(authorization\s*[\"']?\s*[:=]\s*[\"']?\s*bearer\s+)[^\"'\s,}]+", r"\1<REDACTED>"),
        (r"(?i)(password\s*[:=]\s*)[^,\s}]+", r"\1<REDACTED>"),
        (r"(?i)(refresh[_ -]?token\s*[:=]\s*)[^,\s}]+", r"\1<REDACTED>"),
        (r"(?i)(access[_ -]?token\s*[:=]\s*)[^,\s}]+", r"\1<REDACTED>"),
        (r"(?i)(p0_e2e_(?:email|password)\s*[:=]\s*)[^,\s}]+", r"\1<REDACTED>"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def console_safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_name = str(key).lower().replace("-", "_")
            if key_name in {"authorization", "password", "access_token", "refresh_token", "delegated_token"}:
                result[key] = "<REDACTED>"
            else:
                result[key] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    return value


class HarnessTimeout(RuntimeError):
    def __init__(self, code: str, message: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


class Manifest:
    def __init__(self, root: Path, *, run_id: str, timeouts: dict[str, Any]):
        self.root = root
        self.path = root / "manifest.json"
        self.log_path = root / "harness.log"
        self.data: dict[str, Any] = {
            "harness_run_id": run_id,
            "started_at": utc_now(),
            "status": "STARTING",
            "creator": {"pid": None, "port": None, "instance_id": None,
                         "build_commit": None, "database_path": None,
                         "redis_namespace": None, "log_path": str(root / "creator.log")},
            "agent": {"api_pid": None, "worker_pid": None, "api_port": None,
                          "log_paths": [str(root / "agent-api.log"), str(root / "agent-worker.log")]},
            "business": {"user_id": None, "conversation_id": None, "goal_ids": [],
                         "agent_run_ids": [], "creator_task_ids": [],
                         "creator_run_ids": [], "scheduled_action_ids": []},
            "timeouts": timeouts,
            "last_progress_at": None,
            "last_stage": None,
            "error": None,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self.update("HARNESS_STARTED")

    def update(self, stage: str, **fields: Any) -> None:
        self.data["last_stage"] = stage
        self.data.update(fields)
        self._write()
        self.log(f"MANIFEST stage={stage} fields={json.dumps(fields, ensure_ascii=False, default=str)}")

    def progress(self, **fields: Any) -> None:
        self.data["last_progress_at"] = utc_now()
        self.data.update(fields)
        self._write()

    def log(self, message: str) -> None:
        line = f"{utc_now()} {redact(message)}\n"
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
        print(console_safe(line), end="", flush=True)

    def _write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(sanitize(self.data), ensure_ascii=False, indent=2, default=str)
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


class LogPump:
    def __init__(self, process: subprocess.Popen[str], paths: list[Path], log: Callable[[str], None]):
        self.process = process
        self.paths = paths
        self.log = log
        self.thread = threading.Thread(target=self._pump, name=f"p0-log-{process.pid}", daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        if self.process.stdout is None:
            return
        streams = [path.open("a", encoding="utf-8") for path in self.paths]
        try:
            for raw in iter(self.process.stdout.readline, ""):
                line = redact(raw.rstrip("\r\n"))
                for stream in streams:
                    stream.write(line + "\n")
                    stream.flush()
                self.log(f"CHILD pid={self.process.pid} {line}")
        finally:
            for stream in streams:
                stream.close()

    def join(self, timeout: float = 2.0) -> None:
        self.thread.join(timeout)


@dataclass
class Deadline:
    hard_timeout: float
    started: float = field(default_factory=time.monotonic)

    def check(self, label: str) -> None:
        elapsed = time.monotonic() - self.started
        if elapsed >= self.hard_timeout:
            raise HarnessTimeout("GLOBAL_HARD_TIMEOUT", f"{label} exceeded global hard timeout ({elapsed:.1f}s)")

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


def wait_ready(path: Path, process: subprocess.Popen[str], deadline: Deadline, log: Manifest) -> int:
    started = time.monotonic()
    while time.monotonic() - started < 30:
        deadline.check("service startup")
        if path.exists():
            return int(json.loads(path.read_text(encoding="utf-8"))["port"])
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode} before ready")
        time.sleep(0.2)
    raise HarnessTimeout("SERVICE_START_TIMEOUT", f"server did not publish port: {path}")


def start_server(*, root: Path, python: Path, env: dict[str, str], ready: Path,
                 log_paths: list[Path], deadline: Deadline, manifest: Manifest,
                 role: str) -> tuple[subprocess.Popen[str], int, LogPump]:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["P0_READY_FILE"] = str(ready)
    child_env["PYTHONUNBUFFERED"] = "1"
    manifest.update(f"{role.upper()}_STARTING")
    process = subprocess.Popen(
        [str(python), "-u", "-c", SERVER_CODE], cwd=root, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    pump = LogPump(process, log_paths, manifest.log)
    port = wait_ready(ready, process, deadline, manifest)
    manifest.update(f"{role.upper()}_HEALTHY", **({"creator": {**manifest.data["creator"], "pid": process.pid, "port": port}}
                                                   if role == "creator" else
                                                   {"agent": {**manifest.data["agent"], "api_pid": process.pid, "worker_pid": process.pid, "api_port": port}}))
    return process, port, pump


def wait_health(base_url: str, deadline: Deadline) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < 30:
        deadline.check("health check")
        try:
            response = requests.get(f"{base_url}/actuator/health", timeout=3)
            if response.status_code == 200 and response.json().get("status") == "UP":
                return response.json()
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise HarnessTimeout("SERVICE_HEALTH_TIMEOUT", f"health failed for {base_url}")


def assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def login(deadline: Deadline) -> str:
    deadline.check("Java login")
    identifier = os.environ["P0_E2E_EMAIL"]
    password = os.environ["P0_E2E_PASSWORD"]
    identifier_type = os.environ.get("P0_E2E_IDENTIFIER_TYPE", "PHONE")
    response = requests.post(
        "http://127.0.0.1:8080/api/v1/auth/login",
        json={"identifierType": identifier_type, "identifier": identifier, "password": password, "code": None},
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError(f"Java login failed: status={response.status_code} body={redact(response.text)}")
    payload = response.json()
    token = str(payload["token"]["accessToken"])
    return token


def _progress_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    run = payload.get("run") or {}
    return (payload.get("status"), run.get("status"), payload.get("updated_at"),
            len(payload.get("artifacts") or []), run.get("checkpoint_id"),
            payload.get("current_node"), payload.get("execution_key"))


def _record_business_ids(manifest: Manifest, value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"goal_id", "creator_task_id", "creator_run_id", "scheduled_action_id"}:
                target = {
                    "goal_id": "goal_ids",
                    "creator_task_id": "creator_task_ids",
                    "creator_run_id": "creator_run_ids",
                    "scheduled_action_id": "scheduled_action_ids",
                }[normalized]
                if item and item not in manifest.data["business"][target]:
                    manifest.data["business"][target].append(item)
            _record_business_ids(manifest, item)
    elif isinstance(value, list):
        for item in value:
            _record_business_ids(manifest, item)


def task_snapshot(base_url: str, token: str, task_id: str, *, deadline: Deadline,
                  manifest: Manifest, creator_run_id: str | None = None) -> dict[str, Any]:
    task_started = time.monotonic()
    last_progress = time.monotonic()
    last_signature: tuple[Any, ...] | None = None
    while True:
        deadline.check("Creator task")
        if time.monotonic() - task_started >= DEFAULT_CREATOR_HARD_TIMEOUT:
            raise HarnessTimeout("TASK_BUDGET_EXHAUSTED", f"Creator task {task_id} exceeded {DEFAULT_CREATOR_HARD_TIMEOUT}s")
        try:
            response = requests.get(f"{base_url}/api/v1/creator/tasks/{task_id}",
                                    headers={"Authorization": f"Bearer {token}"}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            signature = _progress_signature(payload)
            if signature != last_signature:
                last_signature = signature
                last_progress = time.monotonic()
                run = payload.get("run") or {}
                manifest.progress(last_progress_monotonic=last_progress,
                                  creator_task_status=payload.get("status"),
                                  creator_run_status=run.get("status"),
                                  creator_artifact_count=len(payload.get("artifacts") or []),
                                  creator_last_progress_at=payload.get("updated_at"))
                manifest.log("CREATOR_PROGRESS " + json.dumps({
                    "task_id": task_id, "status": payload.get("status"),
                    "run_status": run.get("status"), "updated_at": payload.get("updated_at"),
                    "current_node": payload.get("current_node") or run.get("current_node"),
                    "execution_key": payload.get("execution_key") or run.get("execution_key"),
                    "artifact_count": len(payload.get("artifacts") or []),
                }, ensure_ascii=False))
            if payload.get("status") in CREATOR_TERMINAL:
                return payload
        except requests.ReadTimeout:
            manifest.log(f"CREATOR_POLL_READ_TIMEOUT task_id={task_id}")
        if time.monotonic() - last_progress >= DEFAULT_STALL_TIMEOUT:
            raise HarnessTimeout("NODE_STALL_TIMEOUT", f"Creator task {task_id} made no observable progress",
                                  evidence={"task_id": task_id, "last_progress_at": payload.get("updated_at") if 'payload' in locals() else None})
        time.sleep(5)


def submit_creator(base_url: str, token: str, payload: dict[str, Any], *, deadline: Deadline,
                   manifest: Manifest) -> dict[str, Any]:
    deadline.check("Creator submit")
    response = requests.post(
        f"{base_url}/api/v1/creator/tasks",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"p0-harness-{uuid.uuid4()}"},
        json=payload, timeout=15,
    )
    response.raise_for_status()
    accepted = response.json()
    _record_business_ids(manifest, accepted)
    manifest.data["business"]["creator_task_ids"].append(accepted["task_id"])
    manifest.data["business"]["creator_run_ids"].append(accepted["run_id"])
    manifest.update("GREENBOOK_CREATOR_TASK_CREATED")
    snapshot = task_snapshot(base_url, token, accepted["task_id"], deadline=deadline,
                             manifest=manifest, creator_run_id=accepted["run_id"])
    return {"accepted": accepted, "snapshot": snapshot}


def agent_run(base_url: str, token: str, prompt: str, *, conversation: dict[str, Any] | None,
                  deadline: Deadline, manifest: Manifest, label: str) -> dict[str, Any]:
    if label == "E2E2" and os.environ.get("P0_E2E_SKIP_E2E2") == "true":
        manifest.update("E2E2_SKIPPED")
        return {"skipped": True}
    headers = {"Authorization": f"Bearer {token}"}
    if conversation is None:
        deadline.check("Conversation create")
        response = requests.post(f"{base_url}/api/v1/agent/conversations", headers=headers,
                                 json={"title": "P0 final E2E", "surface": "HOME"}, timeout=15)
        response.raise_for_status()
        conversation = response.json()
        manifest.data["business"]["conversation_id"] = conversation["conversation_id"]
        manifest.update("CONVERSATION_CREATED")
    deadline.check(f"{label} message submit")
    accepted = requests.post(
        f"{base_url}/api/v1/agent/conversations/{conversation['conversation_id']}/messages",
        headers={**headers, "Idempotency-Key": f"p0-final-{uuid.uuid4()}"},
        json={"content": prompt, "client_timezone": "Asia/Shanghai"}, timeout=15,
    )
    accepted.raise_for_status()
    accepted_view = accepted.json()
    run_id = accepted_view["run_id"]
    manifest.data["business"]["agent_run_ids"].append(run_id)
    manifest.update(f"{label}_RUN_CREATED")
    run_started = time.monotonic()
    last_progress = time.monotonic()
    last_sig: tuple[Any, ...] | None = None
    while True:
        deadline.check(f"{label} Agent Run")
        if time.monotonic() - run_started >= DEFAULT_GREENBOOK_AGENT_RUN_HARD_TIMEOUT:
            raise HarnessTimeout("GREENBOOK_AGENT_RUN_HARD_TIMEOUT", f"{label} exceeded {DEFAULT_GREENBOOK_AGENT_RUN_HARD_TIMEOUT}s")
        response = requests.get(f"{base_url}/api/v1/agent/runs/{run_id}", headers=headers, timeout=15)
        response.raise_for_status()
        view = response.json()
        steps = view.get("steps") or []
        artifacts = view.get("artifacts") or []
        sig = (view.get("status"), view.get("updated_at"), len(steps), len(artifacts),
               next((s.get("status") for s in reversed(steps) if s.get("status") not in TERMINAL), None))
        if sig != last_sig:
            last_sig = sig
            last_progress = time.monotonic()
            manifest.progress(last_progress_monotonic=last_progress,
                              agent_run_status=view.get("status"), agent_run_updated_at=view.get("updated_at"),
                              agent_step_count=len(steps), agent_artifact_count=len(artifacts))
            manifest.log("GREENBOOK_AGENT_PROGRESS " + json.dumps({"label": label, "run_id": run_id,
                         "status": view.get("status"), "updated_at": view.get("updated_at"),
                         "step_count": len(steps), "artifact_count": len(artifacts)}, ensure_ascii=False))
        if view.get("status") in TERMINAL:
            if view.get("status") != "COMPLETED":
                raise HarnessTimeout(
                    "GREENBOOK_AGENT_RUN_FAILED",
                    f"{label} ended in {view.get('status')}",
                    evidence={"run_id": run_id, "run": view, "artifacts": artifacts},
                )
            artifacts_response = requests.get(
                f"{base_url}/api/v1/agent/runs/{run_id}/artifacts",
                headers=headers,
                timeout=15,
            )
            if artifacts_response.ok:
                artifacts = artifacts_response.json()
            _record_business_ids(manifest, view)
            _record_business_ids(manifest, artifacts)
            return {"conversation": conversation, "accepted": accepted_view, "run": view, "artifacts": artifacts}
        if time.monotonic() - last_progress >= DEFAULT_STALL_TIMEOUT:
            raise HarnessTimeout("NODE_STALL_TIMEOUT", f"{label} made no observable progress",
                                  evidence={"run_id": run_id, "last_progress_at": view.get("updated_at")})
        time.sleep(5)


def cleanup_redis_namespace(redis_url: str, namespace: str, manifest: Manifest) -> dict[str, int]:
    try:
        import redis
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        keys = list(client.scan_iter(match=f"{namespace}:*", count=1000))
        deleted = int(client.delete(*keys)) if keys else 0
        remaining = sum(1 for _ in client.scan_iter(match=f"{namespace}:*", count=1000))
        result = {"keys_before": len(keys), "keys_deleted": deleted, "keys_after": remaining}
    except Exception as exc:
        result = {"keys_before": -1, "keys_deleted": 0, "keys_after": -1, "error": type(exc).__name__}
    manifest.update("REDIS_CLEANUP", redis_cleanup=result)
    return result


def collect_evidence(*, manifest: Manifest, evidence: dict[str, Any]) -> None:
    manifest.update("EVIDENCE_COLLECTED", evidence=evidence)
    path = manifest.root / "evidence.json"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(sanitize(evidence), stream, ensure_ascii=False, indent=2, default=str)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    summary = manifest.root / "evidence-summary.txt"
    summary.write_text(json.dumps(sanitize(evidence), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def stop_process(process: subprocess.Popen[str] | None, pump: LogPump | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
    if pump is not None:
        pump.join()


def main() -> int:
    started_monotonic = time.monotonic()
    started_at = utc_now()
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-output", action="store_true")
    parser.add_argument("--experiment", choices=("A", "B", "C", "D"), default="C")
    parser.add_argument("--global-timeout", type=int, default=None)
    parser.add_argument("--e2e1-only", action="store_true")
    parser.add_argument("--e2e2-only", action="store_true")
    args = parser.parse_args()
    if args.e2e1_only:
        os.environ["P0_E2E_SKIP_E2E2"] = "true"
    creator_hard = DEFAULT_CREATOR_HARD_TIMEOUT
    agent_hard = DEFAULT_GREENBOOK_AGENT_RUN_HARD_TIMEOUT
    global_hard = args.global_timeout or (120 + 30 + 30 + agent_hard * 2 + 120 + 120)
    run_id = uuid.uuid4().hex
    run_root = Path(__file__).resolve().parents[1] / ".p0-e2e-runs" / run_id
    manifest = Manifest(run_root, run_id=run_id, timeouts={
        "stall_timeout": DEFAULT_STALL_TIMEOUT, "creator_task_hard_timeout": creator_hard,
        "agent_run_hard_timeout": agent_hard, "global_hard_timeout": global_hard,
    })
    manifest.data["started_at"] = started_at; manifest.data["harness_started_monotonic"] = started_monotonic; manifest._write()
    deadline = Deadline(global_hard, started=started_monotonic)
    repo = run_root.parents[1]
    creator_root = repo / "creator-agent"; agent_root = repo / "apps" / "agent_api"
    creator_python = creator_root / ".venv/Scripts/python.exe"
    agent_python = repo / ".venv-v2/Scripts/python.exe"
    if not agent_python.exists():
        agent_python = Path(sys.executable)
    temp_path = Path(tempfile.mkdtemp(prefix="greenbook-p0-")); creator_db = temp_path / "creator.sqlite"; checkpoint_db = temp_path / "checkpoints.sqlite"
    creator_process = agent_process = None; creator_pump = agent_pump = None; ports: list[int] = []
    creator_namespace = f"creator:p0:{run_id}"
    creator_env = {"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"], "AI_PROVIDER":"deepseek",
        "GREENBOOK_CREATOR_DATABASE_URL":f"sqlite+aiosqlite:///{creator_db}", "GREENBOOK_CREATOR_CHECKPOINT_BACKEND":"sqlite",
        "GREENBOOK_CREATOR_CHECKPOINT_SQLITE_PATH":str(checkpoint_db), "GREENBOOK_CREATOR_CHECKPOINT_AUTO_SETUP":"true",
        "GREENBOOK_CREATOR_CHECKPOINT_DIAGNOSTICS":"true", "GREENBOOK_CREATOR_API_EXECUTION_MODE":"local",
        "GREENBOOK_CREATOR_API_CREATE_SCHEMA":"true", "GREENBOOK_CREATOR_IDENTITY_MODE":"oidc",
        "GREENBOOK_CREATOR_IDENTITY_ISSUER":"http://127.0.0.1:8080", "GREENBOOK_CREATOR_IDENTITY_AUDIENCE":"creator-agent",
        "GREENBOOK_CREATOR_IDENTITY_JWKS_URL":"http://127.0.0.1:8080/.well-known/jwks.json",
        "GREENBOOK_CREATOR_IDENTITY_ALLOW_INSECURE_HTTP":"true", "GREENBOOK_CREATOR_REDIS_URL":"redis://:mindflow@127.0.0.1:26379/15",
        "GREENBOOK_CREATOR_MAX_WRITER_REVISIONS":"4", "GREENBOOK_CREATOR_MAX_MODEL_CALLS":"24", "GREENBOOK_CREATOR_MAX_SUPERVISOR_TURNS":"24",
        "GREENBOOK_CREATOR_MAX_AGENT_DISPATCHES":"24", "GREENBOOK_CREATOR_MAX_OUTPUT_TOKENS":"40000", "GREENBOOK_CREATOR_RUN_LEASE_SECONDS":"120",
        "GREENBOOK_CREATOR_MODEL_TIMEOUT_SECONDS":"60", "GREENBOOK_CREATOR_SPECIALIST_TIMEOUT_SECONDS":"90",
        "GREENBOOK_CREATOR_BUILD_COMMIT":subprocess.check_output(["git","rev-parse","HEAD"],cwd=creator_root,text=True).strip(),
        "GREENBOOK_CREATOR_INSTANCE_ID":f"p0-e2e-{run_id[:8]}", "GREENBOOK_CREATOR_QUEUE_NAMESPACE":creator_namespace,
        "GREENBOOK_CREATOR_DATABASE_IDENTIFIER":"temporary-sqlite", "GREENBOOK_CREATOR_API_WORKER_ID":f"p0-e2e-dispatcher:{run_id[:8]}"}
    manifest.data["creator"].update({"instance_id":creator_env["GREENBOOK_CREATOR_INSTANCE_ID"],"build_commit":creator_env["GREENBOOK_CREATOR_BUILD_COMMIT"],"database_path":str(creator_db),"redis_namespace":creator_namespace}); manifest._write()
    try:
        creator_process, creator_port, creator_pump = start_server(root=creator_root, python=creator_python, env=creator_env, ready=temp_path/"creator-ready.json", log_paths=[run_root/"creator.log"], deadline=deadline, manifest=manifest, role="creator"); ports.append(creator_port)
        creator_url=f"http://127.0.0.1:{creator_port}"; health=wait_health(creator_url,deadline); manifest.update("GREENBOOK_CREATOR_HEALTHY",creator_url=creator_url,health=health,checkpoint_ns="",revision_budget=4)
        token=login(deadline); manifest.update("JAVA_LOGIN_COMPLETED",email_configured=True,password_configured=True)
        agent_env={"P0_APP_KIND":"agent","PYTHONPATH":os.pathsep.join(str(path) for path in (repo, repo / "packages" / "agent_core", repo / "packages" / "contracts", repo / "packages" / "java_client", repo / "packages" / "creator_client", repo / "packages" / "security", repo / "services" / "greenbook_mcp", repo / "apps" / "agent_api", repo / "apps" / "agent_worker")),"DEEPSEEK_API_KEY":os.environ["DEEPSEEK_API_KEY"],"GREENBOOK_AGENT_DATABASE_URL":os.environ.get("P0_E2E_GREENBOOK_AGENT_DATABASE_URL","postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"),"GREENBOOK_AGENT_REDIS_URL":"redis://:mindflow@127.0.0.1:26379/14","GREENBOOK_CREATOR_BASE_URL":creator_url,"GREENBOOK_JAVA_BASE_URL":"http://127.0.0.1:8080","GREENBOOK_AGENT_IDENTITY_ISSUER":"http://127.0.0.1:8080","GREENBOOK_AGENT_IDENTITY_AUDIENCE":"greenbook-agent-runtime","GREENBOOK_AGENT_IDENTITY_JWKS_URL":"http://127.0.0.1:8080/.well-known/jwks.json","GREENBOOK_AGENT_ALLOW_INSECURE_HTTP":"true","GREENBOOK_AGENT_SERVICE_SHARED_SECRET":os.environ["GREENBOOK_AGENT_SERVICE_SHARED_SECRET"],"GREENBOOK_AGENT_PROCESS_ROLE":"all","GREENBOOK_AGENT_DEV_RELOAD":"false"}
        agent_process, agent_port, agent_pump=start_server(root=agent_root,python=agent_python,env=agent_env,ready=temp_path/"agent-ready.json",log_paths=[run_root/"agent-api.log",run_root/"agent-worker.log"],deadline=deadline,manifest=manifest,role="agent"); ports.append(agent_port); agent_url=f"http://127.0.0.1:{agent_port}"; wait_health(agent_url,deadline); manifest.update("GREENBOOK_AGENT_API_HEALTHY",agent_url=agent_url,creator_base_url=creator_url)
        if args.e2e2_only:
            existing_conversation_id = os.environ["P0_E2E_EXISTING_CONVERSATION_ID"]
            conversation = {"conversation_id": existing_conversation_id}
            e2e2 = agent_run(
                agent_url,
                token,
                "Revise the just-created draft for beginners and add three concrete troubleshooting steps.",
                conversation=conversation,
                deadline=deadline,
                manifest=manifest,
                label="E2E2",
            )
            collect_evidence(manifest=manifest, evidence={"e2e2": e2e2})
            manifest.update("COMPLETED", status="COMPLETED")
            return 0
        conversation=None
        e2e1=agent_run(agent_url,token,"搜索一些社区里关于 Agent 稳定性的公开帖子，参考搜索结果写一篇实用内容，十分钟后发布。",conversation=conversation,deadline=deadline,manifest=manifest,label="E2E1"); conversation=e2e1["conversation"]; manifest.update("E2E1_COMPLETED")
        e2e2=agent_run(agent_url,token,"把刚才那篇 Agent 稳定性的草稿改得更适合初学者，并增加三个具体排查步骤。",conversation=conversation,deadline=deadline,manifest=manifest,label="E2E2"); manifest.update("E2E2_COMPLETED")
        collect_evidence(manifest=manifest,evidence={"e2e1":e2e1,"e2e2":e2e2}); manifest.update("COMPLETED",status="COMPLETED"); return 0
    except Exception as exc:
        evidence={"error_type":type(exc).__name__,"error":redact(str(exc)),"elapsed_seconds":deadline.elapsed,"hard_timeout":global_hard,"manifest":manifest.data.copy()}
        manifest.update("COLLECTING_EVIDENCE",status="COLLECTING_EVIDENCE",error=evidence); collect_evidence(manifest=manifest,evidence=evidence); manifest.update("FAILED",status="FAILED"); return 1
    finally:
        manifest.update("CLEANUP_STARTED")
        stop_process(agent_process,agent_pump); stop_process(creator_process,creator_pump)
        for port in ports:
            try: assert_port_free(port); manifest.log(f"PORT_RELEASED port={port}")
            except OSError as exc: manifest.log(f"PORT_RELEASE_CHECK_FAILED port={port} error={type(exc).__name__}")
        failed=manifest.data.get("status") not in {"COMPLETED"}; keep_failed=os.environ.get("P0_E2E_KEEP_FAILED_ARTIFACTS","true").lower() == "true"
        redis_url=creator_env.get("GREENBOOK_CREATOR_REDIS_URL"); redis_result=cleanup_redis_namespace(redis_url,creator_namespace,manifest) if not failed or not keep_failed else {"preserved":True}
        manifest.data["redis_cleanup"]=redis_result
        if not failed or not keep_failed:
            shutil.rmtree(temp_path,ignore_errors=True)
        else: manifest.data["preserved_temp_path"]=str(temp_path)
        manifest.update("CLEANUP_COMPLETED",status=manifest.data.get("status"),preserved_failed_artifacts=failed and keep_failed)


if __name__ == "__main__":
    raise SystemExit(main())
