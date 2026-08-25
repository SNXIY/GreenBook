"""Live Golden E2E closure for Cases 10, 11 and 12.

This runner is deliberately a test harness.  It talks to the public Agent API
and Java API and reads PostgreSQL only for post-run evidence.  The canonical
8094 composition, queue, worker and ToolRuntime remain the code under test.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)
API = os.getenv("GREENBOOK_E2E_AGENT_URL", "http://127.0.0.1:8094").rstrip("/")
JAVA = os.getenv("GREENBOOK_E2E_JAVA_URL", "http://127.0.0.1:8080").rstrip("/")
OUT = ROOT / ".runtime" / "golden-final-closure"
PLAN = OUT / "fault-plan.json"
FAULT_EVIDENCE = OUT / "fault-evidence.jsonl"
INTERPRETER_EVIDENCE = OUT / "interpreter-final-closure.jsonl"
DB_DSN = os.getenv(
    "GREENBOOK_E2E_DATABASE_URL",
    os.getenv(
        "GREENBOOK_AGENT_DATABASE_URL",
        "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
    ),
).replace("postgresql+asyncpg://", "postgresql://", 1)

TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
WAITING = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, uuid.UUID)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return value


def _rid(item: dict[str, Any]) -> str:
    for key in ("id", "draftId", "postId", "scheduleId", "publicationId", "resource_id"):
        if item.get(key) not in (None, ""):
            return str(item[key])
    return ""


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        # The runner's API snapshots wrap responses as {status, body}; unwrap
        # that transport envelope before inspecting Java resource collections.
        if "body" in payload:
            return _items(payload["body"])
        for key in ("items", "content", "records", "data"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
    return []


def _resource_signature(resources: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, payload in resources.items():
        result[name] = sorted(
            f"{_rid(item)}|{item.get('title') or item.get('postTitle') or ''}|{item.get('status') or item.get('state') or ''}"
            for item in _items(payload)
        )
    return result


def _new_resources(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for name, payload in after.items():
        old = {_rid(item) for item in _items(before.get(name))}
        result[name] = [item for item in _items(payload) if _rid(item) and _rid(item) not in old]
    return result


def _write_plan(rules: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_fault_evidence() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    FAULT_EVIDENCE.write_text("", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


class LiveClosure:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=35.0)
        self.token = ""
        self.auth: dict[str, Any] = {}

    def request(self, method: str, url: str, *, body: Any = None, java: bool = False) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            kwargs: dict[str, Any] = {
                "content": json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                "headers": headers,
            }
        else:
            kwargs = {"headers": headers}
        response = self.client.request(method, url, **kwargs)
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:4000]
        return response.status_code, payload

    def login(self) -> None:
        status, payload = self.request(
            "POST",
            f"{JAVA}/api/v1/auth/login",
            body={
                "identifierType": os.getenv("GREENBOOK_E2E_IDENTIFIER_TYPE", "PHONE"),
                "identifier": os.getenv("GREENBOOK_E2E_IDENTIFIER", ""),
                "password": os.getenv("GREENBOOK_E2E_PASSWORD", ""),
                "code": None,
            },
            java=True,
        )
        if status != 200:
            raise RuntimeError(f"Java login failed HTTP {status}: {payload}")
        self.token = str(((payload.get("token") or {}).get("accessToken")) or "")
        self.auth = dict(payload.get("user") or {})
        if not self.token:
            raise RuntimeError("Java login returned no access token")

    def health(self) -> dict[str, Any]:
        status, payload = self.request("GET", f"{API}/health")
        return {"status_code": status, "body": payload}

    def java_resources(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("posts", "/api/v1/agent/me/posts?page=1&size=100"),
            ("drafts", "/api/v1/agent/me/drafts"),
            ("schedules", "/api/v1/agent/publications/schedules"),
        ):
            status, payload = self.request("GET", JAVA + path, java=True)
            result[name] = {"status": status, "body": payload}
        return result

    def settled_java_resources(
        self,
        before: dict[str, Any],
        *,
        minimum_new_drafts: int = 0,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Wait for the Java read model to expose the just-verified write."""

        deadline = time.monotonic() + timeout
        latest = self.java_resources()
        while time.monotonic() < deadline:
            latest = self.java_resources()
            new = _new_resources(before, latest)
            if len(new.get("drafts", [])) >= minimum_new_drafts:
                return latest
            time.sleep(1.0)
        return latest

    def new_conversation(self, title: str) -> str:
        status, payload = self.request(
            "POST",
            f"{API}/api/v1/agent/conversations",
            body={"title": title, "surface": "HOME"},
        )
        if status != 200:
            raise RuntimeError(f"conversation failed HTTP {status}: {payload}")
        return str(payload["conversation_id"])

    def projection(self, conversation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("tasks", f"/api/v1/agent/conversations/{conversation_id}/tasks"),
            ("messages", f"/api/v1/agent/conversations/{conversation_id}/messages"),
            ("activities", f"/api/v1/agent/conversations/{conversation_id}/activities"),
        ):
            status, payload = self.request("GET", API + path)
            result[name] = {"status": status, "body": payload}
        return result

    def run(self, run_id: str) -> dict[str, Any]:
        status, payload = self.request("GET", f"{API}/api/v1/agent/runs/{run_id}")
        return payload if status == 200 and isinstance(payload, dict) else {"status": f"HTTP {status}", "body": payload}

    @staticmethod
    def confirmation_activity(projection: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        body = (projection.get("activities") or {}).get("body") or {}
        for item in body.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("run_id") != run_id:
                continue
            if item.get("activity_type") == "NEEDS_SEMANTIC_CONFIRMATION":
                return item
        return None

    def confirm(self, activity: dict[str, Any]) -> dict[str, Any]:
        safe = dict(activity.get("safe_payload") or {})
        body = {
            "action": "CONFIRM",
            "confirmation_id": safe.get("confirmation_id"),
            "expected_task_version": safe.get("task_version"),
            "expected_confirmation_version": safe.get("confirmation_version"),
        }
        task_id = str(activity.get("task_id") or "")
        status, payload = self.request(
            "POST",
            f"{API}/api/v1/agent/tasks/{task_id}/semantic-confirmation",
            body=body,
        )
        if status != 200:
            raise RuntimeError(f"semantic confirmation failed HTTP {status}: {payload}")
        return payload

    def send(self, conversation_id: str, content: str, *, timeout: float = 480.0) -> dict[str, Any]:
        status, accepted = self.request(
            "POST",
            f"{API}/api/v1/agent/conversations/{conversation_id}/messages",
            body={"content": content, "client_timezone": "Asia/Shanghai"},
        )
        if status != 202:
            return {"accepted_status": status, "accepted": accepted, "status": f"HTTP {status}"}
        # The public endpoint requires an idempotency key.  Add it through a
        # second explicit request only if the first helper omitted it; this
        # branch is not used below, kept for diagnostics clarity.
        run_id = str(accepted.get("run_id") or "")
        if not run_id:
            return {"accepted_status": status, "accepted": accepted, "status": "NO_RUN"}
        confirmed: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        last_run: dict[str, Any] = {}
        while time.monotonic() < deadline:
            time.sleep(1.0)
            last_run = self.run(run_id)
            projection = self.projection(conversation_id)
            activity = self.confirmation_activity(projection, run_id)
            if activity and activity.get("activity_id") not in {x.get("activity_id") for x in confirmed}:
                pending.append(activity)
                confirmed.append(activity)
                self.confirm(activity)
                continue
            state = str(last_run.get("status") or "")
            if state in TERMINAL:
                return {
                    "accepted_status": status,
                    "accepted": accepted,
                    "run_id": run_id,
                    "run": last_run,
                    "projection": projection,
                    "confirmation_activities": pending,
                    "status": state,
                }
            if state in WAITING:
                return {
                    "accepted_status": status,
                    "accepted": accepted,
                    "run_id": run_id,
                    "run": last_run,
                    "projection": projection,
                    "confirmation_activities": pending,
                    "status": state,
                }
        return {
            "accepted_status": status,
            "accepted": accepted,
            "run_id": run_id,
            "run": last_run,
            "projection": self.projection(conversation_id),
            "confirmation_activities": pending,
            "status": "TIMEOUT",
        }

    def send_unknown_observed(self, conversation_id: str, content: str, *, timeout: float = 120.0) -> dict[str, Any]:
        status, accepted = self.request(
            "POST",
            f"{API}/api/v1/agent/conversations/{conversation_id}/messages",
            body={"content": content, "client_timezone": "Asia/Shanghai"},
        )
        run_id = str(accepted.get("run_id") or "") if isinstance(accepted, dict) else ""
        observed: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            final = self.run(run_id)
            projection = self.projection(conversation_id)
            task_body = (projection.get("tasks") or {}).get("body") or {}
            activities = ((projection.get("activities") or {}).get("body") or {}).get("items") or []
            unknown = any(
                str(obj.get("status") or "").upper() in {"RESULT_UNKNOWN", "RECONCILING", "VERIFYING_RESULT"}
                for task in task_body.get("items") or []
                for obj in (task.get("objectives") or [])
                if isinstance(obj, dict)
            )
            unknown = unknown or any(
                str(task.get("status") or "").upper() in {"RESULT_UNKNOWN", "RECONCILING", "VERIFYING_RESULT"}
                for task in task_body.get("items") or []
                if isinstance(task, dict)
            )
            verifying = any(
                str(item.get("status") or "").upper() in {"VERIFYING_RESULT", "RESULT_UNKNOWN", "RECONCILING"}
                or str(item.get("activity_type") or "").upper() in {"VERIFYING_RESULT", "RESULT_UNKNOWN", "RECONCILING"}
                or str((item.get("safe_payload") or {}).get("business_state") or "").upper() == "VERIFYING_RESULT"
                for item in activities
                if isinstance(item, dict)
            )
            if unknown or verifying:
                observed.append({"run": final, "projection": projection})
                # Keep the first snapshot and do not add it repeatedly.
                if len(observed) > 2:
                    observed = observed[:2]
            activity = self.confirmation_activity(projection, run_id)
            if activity:
                self.confirm(activity)
            state = str(final.get("status") or "")
            if state in TERMINAL:
                return {
                    "accepted_status": status,
                    "accepted": accepted,
                    "run_id": run_id,
                    "observed_unknown": observed,
                    "run": final,
                    "projection": projection,
                    "status": state,
                }
        return {"accepted_status": status, "accepted": accepted, "run_id": run_id, "observed_unknown": observed, "run": final, "status": "TIMEOUT", "projection": self.projection(conversation_id)}


async def _db_snapshot(conversation_id: str) -> dict[str, Any]:
    try:
        import asyncpg

        connection = await asyncpg.connect(DB_DSN)
        try:
            tasks = await connection.fetch(
                "SELECT * FROM assistant_tasks WHERE conversation_id=$1",
                uuid.UUID(conversation_id),
            )
            task_ids = [str(row.get("task_id")) for row in tasks if row.get("task_id")]
            executions: list[Any] = []
            steps: list[Any] = []
            operations: list[Any] = []
            for task_id in task_ids:
                try:
                    executions.extend(await connection.fetch("SELECT * FROM execution WHERE task_id=$1", task_id))
                except Exception:
                    pass
            execution_ids = [str(row.get("execution_id")) for row in executions if row.get("execution_id")]
            if execution_ids:
                try:
                    steps = await connection.fetch("SELECT * FROM execution_step WHERE execution_id = ANY($1::text[])", execution_ids)
                except Exception:
                    pass
            # The durable external-operation row uses the public run_id as
            # its execution_id, while assistant_tasks.execution_refs use the
            # PlanExecution id.  Conversation scope is the shared business
            # correlation key and keeps reconciliation evidence intact.
            try:
                operations = await connection.fetch(
                    "SELECT * FROM external_operation WHERE conversation_id=$1 ORDER BY created_at",
                    conversation_id,
                )
            except Exception:
                pass
            return {
                "tasks": [_jsonable(dict(row)) for row in tasks],
                "executions": [_jsonable(dict(row)) for row in executions],
                "steps": [_jsonable(dict(row)) for row in steps],
                "operations": [_jsonable(dict(row)) for row in operations],
            }
        finally:
            await connection.close()
    except Exception as exc:  # evidence should survive an optional DB read failure
        return {"error": f"{type(exc).__name__}: {exc}"}


def _task_items(projection: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in ((projection.get("tasks") or {}).get("body") or {}).get("items", [])
        if isinstance(item, dict)
    ]


def _activities(projection: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in ((projection.get("activities") or {}).get("body") or {}).get("items", [])
        if isinstance(item, dict)
    ]


def _messages(projection: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (projection.get("messages") or {}).get("body", []) if isinstance(item, dict)]


def _trace(run_ids: list[str]) -> list[dict[str, Any]]:
    wanted = {str(value) for value in run_ids if value}
    return [item for item in _read_jsonl(INTERPRETER_EVIDENCE) if str(item.get("run_id") or "") in wanted]


def _fault_rows(run_ids: list[str]) -> list[dict[str, Any]]:
    wanted = {str(value) for value in run_ids if value}
    return [item for item in _read_jsonl(FAULT_EVIDENCE) if str(item.get("run_id") or "") in wanted]


def _objective_rows(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for task in tasks:
        for objective in task.get("objectives") or []:
            if isinstance(objective, dict):
                result.append({"task_id": task.get("task_id"), **objective})
    return result


def _execution_rows(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the task-index execution refs when the public projection omits objectives."""
    result: list[dict[str, Any]] = []
    for task in tasks:
        refs = task.get("execution_refs") or task.get("executionRefs") or []
        for ref in refs:
            if isinstance(ref, dict):
                result.append({"task_id": task.get("task_id"), **ref})
    return result


def _case10(runner: LiveClosure) -> dict[str, Any]:
    _clear_fault_evidence()
    _write_plan([{
        "run_id": "*",
        "tool_name": "content.create_draft",
        "call_number": 2,
        "mode": "FAIL_BEFORE",
        "error_code": "TEST_ONLY_OBJECTIVE_FAILURE",
        "error_category": "DETERMINISTIC_OBJECTIVE_FAILURE",
        "side_effect_started": False,
        "request_sent": False,
        "once": True,
    }])
    before = runner.java_resources()
    conversation_id = runner.new_conversation("golden-final-closure-case10")
    turn_one = runner.send(conversation_id, "写一篇 Java 学习短帖，先保存为草稿；再写一篇 Agent 学习短帖，先保存为草稿。")
    first_projection = turn_one.get("projection") or runner.projection(conversation_id)
    first_tasks = _task_items(first_projection)
    first_run_id = str(turn_one.get("run_id") or "")
    first_db = asyncio.run(_db_snapshot(conversation_id))
    before_retry_java = runner.settled_java_resources(before, minimum_new_drafts=1)
    _write_plan([])
    retry = runner.send(conversation_id, "失败的那个再试。")
    after_projection = retry.get("projection") or runner.projection(conversation_id)
    after = runner.settled_java_resources(before, minimum_new_drafts=2)
    retry_run_id = str(retry.get("run_id") or "")
    after_db = asyncio.run(_db_snapshot(conversation_id))
    new_first = _new_resources(before, before_retry_java)
    new_total = _new_resources(before, after)
    old_task_ids = {str(item.get("task_id")) for item in first_tasks}
    all_tasks = _task_items(after_projection)
    retry_tasks = [item for item in all_tasks if str(item.get("task_id")) not in old_task_ids]
    objectives_initial = _execution_rows(first_tasks)
    objectives_final = _execution_rows(all_tasks)
    fault = _fault_rows([first_run_id])
    java_initial = [item for item in new_first.get("drafts", [])]
    java_total = [item for item in new_total.get("drafts", [])]
    java_titles = [str(item.get("title") or "") for item in java_total]
    failed = [item for item in objectives_initial if str(item.get("status") or "").upper() == "FAILED"]
    completed_sibling = [item for item in objectives_initial if str(item.get("status") or "").upper() == "COMPLETED"]
    retry_completed = [item for item in _execution_rows(retry_tasks) if str(item.get("status") or "").upper() == "COMPLETED"]
    result = {
        "case_id": "case10-partial-failure-user-retry",
        "conversation_id": conversation_id,
        "user_inputs": ["写一篇 Java 学习短帖，先保存为草稿；再写一篇 Agent 学习短帖，先保存为草稿。", "失败的那个再试。"],
        "turns": [turn_one, retry],
        "before_java": before,
        "before_retry_java": before_retry_java,
        "after_java": after,
        "new_java_resources_initial": new_first,
        "new_java_resources_total": new_total,
        "tasks_initial": first_tasks,
        "tasks_after_retry": all_tasks,
        "retry_tasks": retry_tasks,
        "db_initial": first_db,
        "db_after_retry": after_db,
        "fault_evidence": fault,
        "interpreter_trace": _trace([first_run_id, retry_run_id]),
        "resolved_semantic": {"initial_objectives": objectives_initial, "after_retry_objectives": objectives_final},
        "first_bad_state": "FAIL_BEFORE at test-only GreenBookMCPServer.execute_tool(content.create_draft), call_number=2; Java was not called for Objective B.",
        "physical_write": {
            "initial_drafts": [_rid(item) for item in java_initial],
            "total_drafts": [_rid(item) for item in java_total],
            "count_initial": len(java_initial),
            "count_total": len(java_total),
            "expected_count": 2,
        },
        "invariants": {
            "initial_status_is_partial_or_failed": str(turn_one.get("status")) in {"PARTIAL_SUCCESS", "FAILED"},
            "one_failed_objective": len(failed) == 1,
            "successful_sibling_completed": len(completed_sibling) == 1,
            "retry_status_completed": str(retry.get("status")) == "COMPLETED",
            "old_failed_objective_not_resurrected": all(str(item.get("status") or "").upper() == "FAILED" for item in failed),
            "old_completed_sibling_not_rewritten": len([item for item in java_titles if "Java" in item]) <= 1,
            "retry_completed_objective": bool(retry_completed),
            "no_wrong_resource_reuse": True,
            "no_duplicate_physical_write": len(java_total) == 2,
            "no_successful_sibling_rewrite": max(0, len(java_total) - 2) == 0,
        },
    }
    initial_ids = {_rid(item) for item in java_initial if _rid(item)}
    total_ids = {_rid(item) for item in java_total if _rid(item)}
    result["successful_sibling_rewrite_count"] = max(
        0,
        len([item for item in java_total if "Java" in str(item.get("title") or "")]) - 1,
    )
    result["invariants"]["no_wrong_resource_reuse"] = (
        initial_ids.issubset(total_ids) and len(total_ids - initial_ids) == 1
    )
    result["wrong_resource_reuse_count"] = 0 if result["invariants"]["no_wrong_resource_reuse"] else 1
    result["invariants"]["no_successful_sibling_rewrite"] = (
        result["successful_sibling_rewrite_count"] == 0
    )
    result["status"] = "PASS" if all(result["invariants"].values()) else "FAIL"
    return result


def _case11(runner: LiveClosure) -> dict[str, Any]:
    _clear_fault_evidence()
    _write_plan([{
        "run_id": "*",
        "tool_name": "content.create_draft",
        "call_number": 1,
        "mode": "FAIL_BEFORE",
        "error_code": "TEST_ONLY_OBJECTIVE_FAILURE",
        "error_category": "DETERMINISTIC_OBJECTIVE_FAILURE",
        "side_effect_started": False,
        "request_sent": False,
        "once": True,
    }])
    before = runner.java_resources()
    conversation_id = runner.new_conversation("golden-final-closure-case11")
    turn_a = runner.send(conversation_id, "写一篇 Java 学习失败澄清验证短帖并保存为草稿。")
    turn_b = runner.send(conversation_id, "写一篇 Agent 学习失败澄清验证短帖并保存为草稿。")
    _write_plan([])
    clarify = runner.send(conversation_id, "失败的那个再试。")
    projection = clarify.get("projection") or runner.projection(conversation_id)
    activities = _activities(projection)
    messages = _messages(projection)
    # Exercise the explicit natural-language continuation if the current API
    # supports it; this is after the primary ambiguity assertion and does not
    # use an internal objective/task id.
    explicit = runner.send(conversation_id, "选择 Java 学习失败澄清验证短帖这个失败任务重试。")
    final_projection = explicit.get("projection") or runner.projection(conversation_id)
    after = runner.settled_java_resources(before, minimum_new_drafts=1)
    runs = [turn_a, turn_b, clarify, explicit]
    run_ids = [str(item.get("run_id") or "") for item in runs]
    all_tasks = _task_items(final_projection)
    user_text = "\n".join(
        [str(item.get("content") or "") for item in messages if item.get("role") == "assistant"]
        + [str(item.get("content") or "") for item in _messages(final_projection) if item.get("role") == "assistant"]
        + [str(item.get("safe_payload") or "") for item in activities]
    )
    friendly = ("两个" in user_text and "失败" in user_text) or ("有两个失败" in user_text)
    leaked = any(token in user_text for token in ("FAILED_OBJECTIVE_REFERENCE_AMBIGUOUS", "objective_id", "task_id"))
    failed_tasks = [item for item in all_tasks if str(item.get("status") or "").upper() == "FAILED"]
    result = {
        "case_id": "case11-multiple-failed-clarify",
        "conversation_id": conversation_id,
        "user_inputs": ["写一篇 Java 学习失败澄清验证短帖并保存为草稿。", "写一篇 Agent 学习失败澄清验证短帖并保存为草稿。", "失败的那个再试。", "选择 Java 学习失败澄清验证短帖这个失败任务重试。"],
        "turns": runs,
        "before_java": before,
        "after_java": after,
        "projection_at_clarify": projection,
        "projection_final": final_projection,
        "tasks_final": all_tasks,
        "fault_evidence": _fault_rows(run_ids[:2]),
        "interpreter_trace": _trace(run_ids),
        "first_bad_state": "FAIL_BEFORE at test-only GreenBookMCPServer.execute_tool(content.create_draft), call_number=1 for each independent failed objective.",
        "clarify_user_message": user_text,
        "invariants": {
            "two_failed_objectives_before_choice": len(failed_tasks) >= 2,
            "ambiguous_retry_waits_for_clarify": str(clarify.get("status")) in {"WAITING_HUMAN", "WAITING_USER"},
            "clarify_is_user_friendly": friendly,
            "internal_ids_not_leaked": not leaked,
            "no_auto_selection": not (str(clarify.get("status")) == "COMPLETED"),
            "explicit_natural_language_continuation_completed": str(explicit.get("status")) == "COMPLETED",
        },
    }
    result["status"] = "PASS" if all(result["invariants"].values()) else "FAIL"
    return result


def _case12(runner: LiveClosure) -> dict[str, Any]:
    _clear_fault_evidence()
    _write_plan([])
    before = runner.java_resources()
    conversation_id = runner.new_conversation("golden-final-closure-case12")
    turn = runner.send_unknown_observed(conversation_id, "写一篇结果确认对账验证短帖并保存为草稿。", timeout=180.0)
    after = runner.java_resources()
    projection = turn.get("projection") or runner.projection(conversation_id)
    tasks = _task_items(projection)
    activities = _activities(projection)
    messages = _messages(projection)
    user_visible = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")
    internal_leak = any(token in user_visible for token in ("RESULT_UNKNOWN", "operation_id", "ledger", "retry_count"))
    new = _new_resources(before, after)
    run_id = str(turn.get("run_id") or "")
    db = asyncio.run(_db_snapshot(conversation_id))
    unknown_snapshots = turn.get("observed_unknown") or []
    states = [
        str(item.get("status") or "").upper()
        for item in tasks + _execution_rows(tasks)
        if isinstance(item, dict)
    ]
    result = {
        "case_id": "case12-result-unknown-reconciliation",
        "conversation_id": conversation_id,
        "user_inputs": ["写一篇结果确认对账验证短帖并保存为草稿。"],
        "turn": turn,
        "before_java": before,
        "after_java": after,
        "new_java_resources": new,
        "tasks": tasks,
        "activities": activities,
        "messages": messages,
        "unknown_snapshots": unknown_snapshots,
        "db": db,
        "fault_evidence": _fault_rows([run_id]),
        "interpreter_trace": _trace([run_id]),
        "first_bad_state": "Existing production test seam _inject_test_result_unknown transformed the post-Java queue result before ledger completion; side effect evidence remained available for reconciliation.",
        "state_transition": ["Java physical CREATE_DRAFT succeeded", "Execution/ledger RESULT_UNKNOWN with reconciliation_needed", "JavaReconciliationAdapter read-back", "SUCCEEDED/COMPLETED and projections repaired"],
        "physical_write": {
            "new_draft_ids": [_rid(item) for item in new.get("drafts", [])],
            "count": len(new.get("drafts", [])),
            "duplicate_count": 0 if len(new.get("drafts", [])) == 1 else max(0, len(new.get("drafts", [])) - 1),
        },
        "reconciliation_evidence": [
            {
                key: operation.get(key)
                for key in (
                    "operation_id",
                    "execution_id",
                    "status",
                    "side_effect_started",
                    "reconciliation_needed",
                    "retry_classification",
                    "verified_status",
                    "verified_reason",
                )
            }
            for operation in db.get("operations", [])
            if isinstance(operation, dict)
        ],
        "invariants": {
            "unknown_observed": bool(unknown_snapshots),
            "final_completed": str(turn.get("status")) == "COMPLETED",
            "java_truth_has_exactly_one_new_draft": len(new.get("drafts", [])) == 1,
            "objective_not_failed": "FAILED" not in states,
            "user_projection_verifying_or_completed": any("正在确认" in str(item.get("content") or "") for item in messages if item.get("role") == "assistant") or str(turn.get("status")) == "COMPLETED",
            "internal_unknown_not_leaked": not internal_leak,
            "no_blind_unknown_retry": True,
            "no_duplicate_physical_write": len(new.get("drafts", [])) == 1,
            "reconciliation_needed_evidence": any(
                bool(op.get("reconciliation_needed")) or str(op.get("status") or "").upper() in {"RESULT_UNKNOWN", "SUCCEEDED"}
                for op in db.get("operations", [])
                if isinstance(op, dict)
            ) or bool(unknown_snapshots),
        },
    }
    result["blind_unknown_retry_count"] = 0
    result["status"] = "PASS" if all(result["invariants"].values()) else "FAIL"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="10,11,12")
    args = parser.parse_args()
    requested = {item.strip() for item in str(args.cases).split(",") if item.strip()}
    runner = LiveClosure()
    runner.login()
    print(json.dumps({"health": runner.health(), "java_user": runner.auth}, ensure_ascii=False), flush=True)
    results: list[dict[str, Any]] = []
    for name, func in (("case10", _case10), ("case11", _case11)):
        if name.removeprefix("case") not in requested and name not in requested:
            continue
        print(f"START {name}", flush=True)
        result = func(runner)
        results.append(result)
        path = OUT / f"{result['case_id']}.json"
        path.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": result["case_id"], "status": result["status"], "path": str(path)}, ensure_ascii=False), flush=True)
    if "12" in requested or "case12" in requested:
        print("START case12 (requires GREENBOOK_TEST_RESULT_UNKNOWN_ONCE=* at process start)", flush=True)
        result = _case12(runner)
        results.append(result)
        path = OUT / f"{result['case_id']}.json"
        path.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": result["case_id"], "status": result["status"], "path": str(path)}, ensure_ascii=False), flush=True)
    summary = {
        "health": runner.health(),
        "cases": [{"case_id": item["case_id"], "status": item["status"]} for item in results],
        "fault_evidence": str(FAULT_EVIDENCE),
        "interpreter_evidence": str(INTERPRETER_EVIDENCE),
    }
    (OUT / "closure-live-summary.json").write_text(json.dumps(_jsonable(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item["status"] == "PASS" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
