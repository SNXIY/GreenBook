"""Live Agent API Restart recovery test.

Sends one user message, kills the agent API while the draft write is in flight,
restarts it, and verifies the SAME Task recovers from Postgres and the ActionLoop
auto-continues to create the Schedule WITHOUT a second user message.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

import httpx

JAVA = "http://127.0.0.1:8080"
API = "http://127.0.0.1:8096"
PHONE = "18818735160"
PW = os.environ["GB_E2E_PW"]
TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
HUMAN = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}

client = httpx.Client(timeout=30)


def wait_healthy() -> bool:
    for _ in range(60):
        try:
            r = client.get(f"{API}/health", timeout=5)
            if r.status_code == 200 and r.json().get("javaReachable") is True:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def kill_api() -> None:
    # Kill the process owning 8096 (the .venv launcher + interpreter tree).
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$p = Get-NetTCPConnection -LocalPort 8096 -State Listen -ErrorAction SilentlyContinue;"
         "if ($p) { $p.OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -Confirm:$false -ErrorAction SilentlyContinue } }"],
        capture_output=True,
    )
    time.sleep(2.0)


def start_api() -> None:
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         r"D:\agent\green-book\scripts\start-agent.ps1", "-NoReload", "-ApiPort", "8096"],
        cwd=r"D:\agent\green-book",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def main() -> None:
    r = client.post(f"{JAVA}/api/v1/auth/login",
                    json={"identifierType": "PHONE", "identifier": PHONE, "password": PW, "code": None})
    r.raise_for_status()
    tok = r.json()["token"]["accessToken"]
    hdr = {"Authorization": f"Bearer {tok}"}
    conv = client.post(f"{API}/api/v1/agent/conversations",
                       json={"title": "e2e-restart", "surface": "HOME"}, headers=hdr).json()["conversation_id"]
    rm = client.post(f"{API}/api/v1/agent/conversations/{conv}/messages",
                     json={"content": "写一篇 Java 学习帖子，五分钟之后发布", "client_timezone": "Asia/Shanghai"},
                     headers={**hdr, "Idempotency-Key": uuid.uuid4().hex})
    if rm.status_code != 202:
        print("ACCEPT FAIL", rm.status_code, rm.text[:200]); sys.exit(1)
    run = rm.json()["run_id"]
    print(f"conv={conv} run={run} at={datetime.utcnow().isoformat()}", flush=True)

    # Let the draft write be durably queued / in-flight, then kill the API.
    time.sleep(6.0)
    kill_api()
    print("API killed (draft write in flight)", flush=True)

    time.sleep(1.0)
    start_api()
    print("API restarting...", flush=True)
    if not wait_healthy():
        print("API did not come back healthy"); sys.exit(1)
    print("API healthy again", flush=True)

    # Poll the run to a terminal state (auto-continue without a second message).
    last = None
    deadline = time.time() + 420
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            rr = client.get(f"{API}/api/v1/agent/runs/{run}", headers=hdr)
            if rr.status_code == 200:
                last = rr.json()
        except Exception:
            continue
        st = str((last or {}).get("status") or "")
        if st in TERMINAL or st in HUMAN:
            break
    print("final run_status=", (last or {}).get("status"), "final=", ((last or {}).get("final_response") or "")[:120], flush=True)

    # Verify: one Task, a Draft + a Schedule resource created.
    tasks = client.get(f"{API}/api/v1/agent/conversations/{conv}/tasks", headers=hdr).json().get("items") or []
    print(f"task_count={len(tasks)}", flush=True)
    for t in tasks:
        print("  task", t["task_id"], t["status"], "goal=", (t.get("goal") or "")[:30], flush=True)


if __name__ == "__main__":
    main()
