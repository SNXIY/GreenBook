"""One-shot P0 E2E harness.

Starts Creator and Assistant with programmatic uvicorn servers on OS-assigned
ports, runs health/OpenAPI/probes, and always tears down child processes.
This is test infrastructure only; it does not change either control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests


SERVER_CODE = r'''
import asyncio, json, os
from pathlib import Path
import uvicorn

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


def wait_ready(path: Path, process: subprocess.Popen[str]) -> int:
    deadline = time.time() + 30
    while time.time() < deadline:
        if path.exists():
            return int(json.loads(path.read_text(encoding="utf-8"))["port"])
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(
                f"server exited with {process.returncode}:\n{output}"
            )
        time.sleep(0.2)
    raise TimeoutError(f"server did not publish port: {path}")


def start_server(
    *, root: Path, python: Path, env: dict[str, str], ready: Path
) -> tuple[subprocess.Popen[str], int]:
    child_env = os.environ.copy()
    child_env.update(env)
    child_env["P0_READY_FILE"] = str(ready)
    process = subprocess.Popen(
        [str(python), "-c", SERVER_CODE],
        cwd=root,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, wait_ready(ready, process)


def wait_health(base_url: str) -> dict[str, Any]:
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/actuator/health", timeout=3)
            if response.status_code == 200:
                payload = response.json()
                if payload.get("status") == "UP":
                    return payload
            last = f"{response.status_code}: {response.text}"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"health failed for {base_url}: {last}")


def assert_port_free(port: int) -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def login() -> str:
    email = os.environ["P0_E2E_EMAIL"]
    password = os.environ["P0_E2E_PASSWORD"]
    response = requests.post(
        "http://127.0.0.1:8080/api/v1/auth/login",
        json={
            "identifierType": "EMAIL",
            "identifier": email,
            "password": password,
            "code": None,
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["token"]["accessToken"])


def task_snapshot(base_url: str, token: str, task_id: str) -> dict[str, Any]:
    deadline = time.time() + float(os.environ.get("P0_E2E_TASK_TIMEOUT", "300"))
    last_status = None
    while time.time() < deadline:
        try:
            response = requests.get(
                f"{base_url}/api/v1/creator/tasks/{task_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != last_status:
                last_status = payload.get("status")
                try:
                    dispatcher_response = requests.get(
                        f"{base_url}/api/v1/creator/status",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    dispatcher_diagnostic = {
                        "http_status": dispatcher_response.status_code,
                        "body": dispatcher_response.json(),
                    }
                except requests.RequestException as exc:
                    dispatcher_diagnostic = {"error": repr(exc)}
                print(json.dumps({
                    "task_diagnostic": {
                        "task_id": task_id,
                        "status": payload.get("status"),
                        "updated_at": payload.get("updated_at"),
                        "attempt": (payload.get("run") or {}).get("execution_attempts"),
                        "artifact_count": len(payload.get("artifacts") or []),
                        "last_error": payload.get("error_message"),
                    },
                    "dispatcher_diagnostic": dispatcher_diagnostic,
                }, ensure_ascii=False), flush=True)
            if payload.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                return payload
        except requests.ReadTimeout:
            pass
        time.sleep(1)
    raise TimeoutError(f"Creator task did not finish: {task_id}")


def submit_creator(
    base_url: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = requests.post(
        f"{base_url}/api/v1/creator/tasks",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"p0-harness-{uuid.uuid4()}",
        },
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    accepted = response.json()
    snapshot = task_snapshot(base_url, token, accepted["task_id"])
    print(json.dumps({"creator_task_snapshot": snapshot}, ensure_ascii=False, indent=2), flush=True)
    return {"accepted": accepted, "snapshot": snapshot}


def artifact_from_isolated_db(db_path: Path, task_id: str, artifact_id: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, kind, producer, revision, content_json, parent_ids_json, metadata_json "
            "FROM creator_artifacts WHERE task_id = ? AND id = ?",
            (task_id, artifact_id),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"isolated Creator artifact not found: {task_id}/{artifact_id}")
    return {
        "artifact_id": row[0],
        "kind": row[1],
        "producer": row[2],
        "revision": row[3],
        "content": json.loads(row[4]),
        "parent_ids": json.loads(row[5] or "[]"),
        "metadata": json.loads(row[6] or "{}"),
    }


def creator_probe(base_url: str, token: str, db_path: Path) -> dict[str, Any]:
    create_results: list[dict[str, Any]] = []
    draft_details: list[dict[str, Any]] = []
    count = 1 if os.environ.get("P0_E2E_SINGLE") == "1" else 3
    for index in range(count):
        create = submit_creator(
            base_url,
            token,
            {
                "kind": "CREATE_CONTENT",
                "goal": f"Create a concise practical post about neighborhood resilience, case {index + 1}.",
                "constraints": {
                    "interaction_mode": "AUTO",
                    "format": "POST",
                    "target_length": 1200,
                    "tone": "PRACTICAL",
                },
                "source_scope": {
                    "include_creator_profile": False,
                    "include_creator_history": False,
                    "include_community_posts": False,
                },
            },
        )
        create_results.append(create)
        if create["snapshot"].get("status") != "COMPLETED":
            raise RuntimeError(json.dumps(create, ensure_ascii=False))
        draft = next(item for item in create["snapshot"].get("artifacts", []) if item["kind"] == "DRAFT")
        draft_details.append(artifact_from_isolated_db(
            db_path, create["accepted"]["task_id"], draft["artifact_id"]
        ))

    if os.environ.get("P0_E2E_SINGLE") == "1":
        return {
            "create_x1": create_results,
            "create_draft_details": draft_details,
            "improve_x0": [],
        }

    improve_results: list[dict[str, Any]] = []
    for index, (create, detail) in enumerate(zip(create_results, draft_details, strict=True)):
        content = detail.get("content") or {}
        document = content.get("document") or content
        improve = submit_creator(
            base_url,
            token,
            {
                "kind": "IMPROVE_DRAFT",
                "goal": f"Improve the draft with one additional concrete action, case {index + 1}.",
                "constraints": {
                    "interaction_mode": "AUTO",
                    "format": "POST",
                    "target_length": 1200,
                    "tone": "PRACTICAL",
                    "draft": {
                        "title": str(document.get("title") or "Neighborhood resilience"),
                        "body_markdown": str(
                            document.get("body_markdown")
                            or document.get("content_markdown")
                            or document.get("body")
                            or ""
                        ),
                    },
                },
                "source_scope": {
                    "include_creator_profile": False,
                    "include_creator_history": False,
                    "include_community_posts": False,
                },
            },
        )
        improve_results.append(improve)
        if improve["snapshot"].get("status") != "COMPLETED":
            raise RuntimeError(json.dumps(improve, ensure_ascii=False))
    return {
        "create_x3": create_results,
        "create_draft_details": draft_details,
        "improve_x3": improve_results,
    }


def assistant_run(base_url: str, token: str, prompt: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    conversation = requests.post(
        f"{base_url}/api/v1/assistant/conversations",
        headers=headers,
        json={"title": "P0 harness", "surface": "HOME"},
        timeout=15,
    )
    conversation.raise_for_status()
    conversation_view = conversation.json()
    key = f"p0-harness-{uuid.uuid4()}"
    accepted = requests.post(
        f"{base_url}/api/v1/assistant/conversations/{conversation_view['conversation_id']}/messages",
        headers={**headers, "Idempotency-Key": key},
        json={"content": prompt, "client_timezone": "Asia/Shanghai"},
        timeout=15,
    )
    accepted.raise_for_status()
    accepted_view = accepted.json()
    deadline = time.time() + 900
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = requests.get(
            f"{base_url}/api/v1/assistant/runs/{accepted_view['run_id']}",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        last = response.json()
        if last.get("status") in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return {
                "conversation": conversation_view,
                "accepted": accepted_view,
                "run": last,
                "artifacts": requests.get(
                    f"{base_url}/api/v1/assistant/runs/{accepted_view['run_id']}/artifacts",
                    headers=headers,
                    timeout=15,
                ).json(),
            }
        time.sleep(2)
    raise TimeoutError(f"GreenBook run did not finish: {accepted_view['run_id']} last={last}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-output", action="store_true")
    parser.add_argument("--experiment", choices=("A", "B", "C", "D"), default="C")
    parser.add_argument("--single", action="store_true")
    parser.add_argument("--task-timeout", type=int, default=300)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    creator_root = repo / "creator-agent"
    assistant_root = repo / "community-assistant-agent"
    creator_python = creator_root / ".venv/Scripts/python.exe"
    assistant_python = assistant_root / ".venv/Scripts/python.exe"
    creator_build = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=creator_root, text=True
    ).strip()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["P0_E2E_TASK_TIMEOUT"] = str(args.task_timeout)
    os.environ["P0_E2E_SINGLE"] = "1" if args.single else "0"
    temp_path = Path(tempfile.mkdtemp(prefix="greenbook-p0-"))
    creator_db = temp_path / "creator.sqlite"
    checkpoint_db = temp_path / "checkpoints.sqlite"
    if args.experiment == "A":
        checkpoint_db = creator_db
    creator_env = {
        "DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"],
        "AI_PROVIDER": "deepseek",
        "CREATOR_DATABASE_URL": f"sqlite+aiosqlite:///{creator_db}",
        "CREATOR_CHECKPOINT_BACKEND": "sqlite",
        "CREATOR_CHECKPOINT_SQLITE_PATH": str(checkpoint_db),
        "CREATOR_CHECKPOINT_AUTO_SETUP": "true",
        "CREATOR_CHECKPOINT_DIAGNOSTICS": "true",
        "CREATOR_API_EXECUTION_MODE": "local",
        "CREATOR_API_CREATE_SCHEMA": "true",
        "CREATOR_IDENTITY_MODE": "oidc",
        "CREATOR_IDENTITY_ISSUER": "http://127.0.0.1:8080",
        "CREATOR_IDENTITY_AUDIENCE": "creator-agent",
        "CREATOR_IDENTITY_JWKS_URL": "http://127.0.0.1:8080/.well-known/jwks.json",
        "CREATOR_IDENTITY_ALLOW_INSECURE_HTTP": "true",
        "REDIS_URL": "redis://:mindflow@127.0.0.1:26379/15",
        "CREATOR_MAX_WRITER_REVISIONS": "4",
        "CREATOR_MAX_MODEL_CALLS": "24",
        "CREATOR_MAX_SUPERVISOR_TURNS": "24",
        "CREATOR_MAX_AGENT_DISPATCHES": "24",
        "CREATOR_MAX_OUTPUT_TOKENS": "40000",
        "CREATOR_RUN_LEASE_SECONDS": "120",
        "CREATOR_MODEL_TIMEOUT_SECONDS": "60",
        "CREATOR_SPECIALIST_TIMEOUT_SECONDS": "90",
        "CREATOR_BUILD_COMMIT": creator_build,
        "CREATOR_INSTANCE_ID": f"p0-e2e-{uuid.uuid4().hex[:8]}",
        "CREATOR_QUEUE_NAMESPACE": f"creator:p0:{uuid.uuid4().hex[:8]}",
        "CREATOR_DATABASE_IDENTIFIER": "temporary-sqlite",
        "CREATOR_API_WORKER_ID": "p0-e2e-dispatcher",
    }
    if args.experiment == "B":
        creator_env["CREATOR_CHECKPOINT_BACKEND"] = "memory"
    if args.experiment == "D":
        creator_env["CREATOR_DIAGNOSTICS_DISABLE_EVENT_PERSISTENCE"] = "true"
    creator_ready = temp_path / "creator-ready.json"
    assistant_ready = temp_path / "assistant-ready.json"
    creator_process: subprocess.Popen[str] | None = None
    assistant_process: subprocess.Popen[str] | None = None
    try:
        creator_process, creator_port = start_server(
                root=creator_root,
                python=creator_python,
                env=creator_env,
                ready=creator_ready,
        )
        creator_url = f"http://127.0.0.1:{creator_port}"
        health = wait_health(creator_url)
        openapi = requests.get(f"{creator_url}/openapi.json", timeout=10).json()
        token = login()
        print(json.dumps({
                "creator": {
                    "url": creator_url,
                    "instance_id": creator_env["CREATOR_INSTANCE_ID"],
            "build_commit": creator_env["CREATOR_BUILD_COMMIT"],
                    "dispatcher": "local-durable-dispatcher",
                    "experiment": args.experiment,
                    "checkpoint_backend": creator_env["CREATOR_CHECKPOINT_BACKEND"],
                    "database_path": str(creator_db),
                    "checkpoint_path": str(checkpoint_db),
                    "revision_budget": 4,
                    "health": health,
                    "task_paths": [p for p in openapi["paths"] if "/creator/tasks" in p],
                }
            }, ensure_ascii=False, indent=2))
        print(json.dumps(creator_probe(creator_url, token, creator_db), ensure_ascii=False, indent=2))
        if args.single:
            print(json.dumps({"experiment": args.experiment, "creator_only": True}, indent=2), flush=True)
            return 0
        assistant_env = {
            "DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"],
            "ASSISTANT_DATABASE_URL": os.environ.get(
                "P0_E2E_ASSISTANT_DATABASE_URL",
                "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
            ),
            "ASSISTANT_REDIS_URL": "redis://:mindflow@127.0.0.1:26379/14",
            "ASSISTANT_CREATOR_BASE_URL": creator_url,
            "ASSISTANT_JAVA_BASE_URL": "http://127.0.0.1:8080",
            "ASSISTANT_IDENTITY_ISSUER": "http://127.0.0.1:8080",
            "ASSISTANT_IDENTITY_AUDIENCE": "community-assistant-agent",
            "ASSISTANT_IDENTITY_JWKS_URL": "http://127.0.0.1:8080/.well-known/jwks.json",
            "ASSISTANT_ALLOW_INSECURE_HTTP": "true",
            "ASSISTANT_SERVICE_SHARED_SECRET": os.environ["ASSISTANT_SERVICE_SHARED_SECRET"],
            "ASSISTANT_PROCESS_ROLE": "all",
            "ASSISTANT_DEV_RELOAD": "false",
        }
        assistant_process, assistant_port = start_server(
            root=assistant_root,
            python=assistant_python,
            env=assistant_env,
            ready=assistant_ready,
        )
        assistant_url = f"http://127.0.0.1:{assistant_port}"
        assistant_health = wait_health(assistant_url)
        print(json.dumps({
            "assistant": {
                "url": assistant_url,
                "creator_base_url": creator_url,
                "health": assistant_health,
            }
        }, ensure_ascii=False, indent=2), flush=True)
        e2e_one = assistant_run(
            assistant_url,
            token,
            "Search public community posts about neighborhood resilience, use the results to write a concise practical post, and schedule it two hours from now. Do not publish immediately.",
        )
        print(json.dumps({"e2e_public_search_create_schedule": e2e_one}, ensure_ascii=False, indent=2), flush=True)
        e2e_two = assistant_run(
            assistant_url,
            token,
            "Find the existing draft created in this conversation and revise it by adding one concrete practical action for readers.",
        )
        print(json.dumps({"e2e_resolve_existing_modify": e2e_two}, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        for process in (assistant_process, creator_process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None and process.stdout is not None:
                output = process.stdout.read()
                if output:
                    print(f"[child {process.pid} output]\n{output}", flush=True)
        for port in locals().get("creator_port"), locals().get("assistant_port"):
            if port is not None:
                try:
                    assert_port_free(port)
                    print(f"port_released={port}", flush=True)
                except OSError as exc:
                    print(f"port_release_check_failed={port}: {exc}", flush=True)
        if not args.keep_output:
            for _ in range(20):
                try:
                    shutil.rmtree(temp_path)
                    break
                except PermissionError:
                    time.sleep(0.5)
            else:
                raise RuntimeError(f"harness cleanup failed: {temp_path}")


if __name__ == "__main__":
    raise SystemExit(main())
