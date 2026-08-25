"""Live reconciliation read-back: a RESULT_UNKNOWN op resolves to VERIFIED_COMPLETED
via the Java authoritative read-back adapter (read-only, no replay).

Creates a real schedule, injects a RESULT_UNKNOWN operation with the schedule's
actual expected_postcondition into the live store, then runs the
ReconciliationWorker.reconcile_due scan (what the standalone worker loop calls)
and asserts the operation becomes VERIFIED_COMPLETED and Java is unchanged.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import UTC, datetime

import asyncpg
import httpx

JAVA = "http://127.0.0.1:8080"
API = "http://127.0.0.1:8096"
PHONE = "18818735160"
PW = os.environ["GB_E2E_PW"]
DB = "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
HUMAN = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}
client = httpx.Client(timeout=30)


async def main() -> None:
    r = client.post(f"{JAVA}/api/v1/auth/login",
                    json={"identifierType": "PHONE", "identifier": PHONE, "password": PW, "code": None})
    r.raise_for_status()
    tok = r.json()["token"]["accessToken"]
    hdr = {"Authorization": f"Bearer {tok}"}

    # 1. Create a real schedule (draft + schedule tomorrow 10:00).
    conv = client.post(f"{API}/api/v1/agent/conversations",
                       json={"title": "e2e-reconcile", "surface": "HOME"}, headers=hdr).json()["conversation_id"]
    rm = client.post(f"{API}/api/v1/agent/conversations/{conv}/messages",
                     json={"content": "帮我写一篇《可靠性测试》的帖子，明天上午十点发布", "client_timezone": "Asia/Shanghai"},
                     headers={**hdr, "Idempotency-Key": uuid.uuid4().hex})
    run = rm.json()["run_id"]
    for _ in range(120):
        time.sleep(2)
        st = client.get(f"{API}/api/v1/agent/runs/{run}", headers=hdr).json().get("status")
        if st in TERMINAL or st in HUMAN:
            break
    time.sleep(5)

    # 2. Find the schedule id via Java (most recent SCHEDULED for this user).
    drafts = client.get(f"{JAVA}/api/v1/agent/me/drafts", headers=hdr).json()
    draft_items = drafts if isinstance(drafts, list) else (drafts.get("items") or [])
    newest = draft_items[-1]
    draft_id = str(newest["draftId"])
    out = __import__("subprocess").run(
        ["mysql", "--user=root", "--password=123456", "--port=33306",
         "--host=127.0.0.1", "--batch", "--skip-column-names", "--execute",
         f"SELECT id, run_at FROM zhiguang.scheduled_publications WHERE draft_id={draft_id} AND status='SCHEDULED' ORDER BY created_at DESC LIMIT 1;"],
        capture_output=True, text=True)
    parts = (out.stdout or "").strip().split("\t")
    schedule_id, run_at = parts[0], parts[1]
    run_at_utc = run_at.replace(" ", "T") + "Z" if run_at else ""
    print(f"schedule_id={schedule_id} run_at={run_at_utc}", flush=True)
    schedule_before = client.get(f"{JAVA}/api/v1/agent/publications/schedules/{schedule_id}", headers=hdr).json()
    run_at_java = schedule_before["runAt"]
    # The adapter's _normalize_time renders datetimes as +00:00, so the expected
    # postcondition must use that spelling to match the Java read-back.
    run_at_utc = run_at_java.replace("Z", "+00:00")

    # Load the DB URL from the repo .env so the factory connects to the live
    # Postgres store.
    _env = {}
    for _line in open(r"D:\agent\green-book\.env", encoding="utf-8"):
        _line = _line.strip()
        if "=" in _line and not _line.startswith("#"):
            _k, _, _v = _line.partition("=")
            _env[_k.strip()] = _v.strip().strip('"').strip("'")
    os.environ["GREENBOOK_AGENT_DATABASE_URL"] = _env.get("GREENBOOK_AGENT_DATABASE_URL", DB)
    from greenbook_agent_core.execution.persistence_provider import RuntimePersistenceFactory

    persistence = RuntimePersistenceFactory.from_env()
    print("persistence storage:", getattr(persistence, "storage", "?"), flush=True)

    # 3. Inject a RESULT_UNKNOWN operation via the real OperationLedger path
    #    (correct store encoding) into the live store.
    from greenbook_agent_core.execution.operation_ledger import OperationLedger

    ledger = OperationLedger(persistence.external_operation_store)
    op_id = f"op-live-reconcile-{uuid.uuid4().hex[:8]}"
    op = ledger.begin_operation(
        idempotency_key=op_id,
        conversation_id=conv,
        task_id="live-task-1",
        execution_id="live-exec-1",
        semantic_action="UPDATE_SCHEDULE",
        resource_id=schedule_id,
        resource_type="SCHEDULE",
        expected_postcondition={"arguments": {}, "expected": {"status": "SCHEDULED", "run_at": run_at_utc}},
        claim_owner="worker-A",
    )
    claimed = ledger.claim(op.operation_id, owner="worker-A")
    ledger.mark_side_effect_started(claimed, request_sent=True)
    ledger.mark_result_unknown(claimed)
    print(f"injected RESULT_UNKNOWN op={op_id}", flush=True)

    # 4. Run the reconcile_due scan (the standalone worker loop's call) with the
    #    read-only Java adapter.
    from greenbook_agent_core.execution.reconciliation_adapters import JavaReconciliationAdapter
    from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker

    try:
        java_client = __import__("greenbook_java_client").JavaClient.from_env()
        adapter = JavaReconciliationAdapter(java_client, token_provider=lambda: tok)
        worker = ReconciliationWorker(ledger, adapter=adapter)
        outcomes = await worker.reconcile_due()
        print("reconcile_due outcomes=", outcomes, flush=True)
    finally:
        persistence.close()

    # 5. Verify the operation resolved and Java is unchanged.
    real_op_id = op.operation_id
    conn = await asyncpg.connect(DB)
    row = await conn.fetchrow("SELECT status, verified_status, verified_reason FROM external_operation WHERE operation_id=$1", real_op_id)
    await conn.close()
    print("op final:", row["status"], row["verified_status"], (row["verified_reason"] or "")[:80], flush=True)
    schedule_after = client.get(f"{JAVA}/api/v1/agent/publications/schedules/{schedule_id}", headers=hdr).json()
    print("java run_at unchanged:", schedule_after["runAt"] == run_at_java, flush=True)

    assert row["status"] == "SUCCEEDED"
    assert row["verified_status"] == "VERIFIED_COMPLETED"
    assert schedule_after["runAt"] == run_at_java
    print("LIVE RECONCILE READ-BACK PASS", flush=True)


asyncio.run(main())
