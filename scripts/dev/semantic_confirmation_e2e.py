"""One focused Semantic Confirmation E2E.

The input is kept in this UTF-8 source file and every JSON request is encoded
explicitly as UTF-8 bytes.  This harness observes the existing API/runtime;
it does not create a second semantic or execution path.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import load_dotenv


MESSAGE = (
    "写两篇帖子：\n\n"
    "第一篇标题是《Java 后端实习面试最容易被问到的 10 个问题》，完成后立即发布。\n\n"
    "第二篇标题是《2026 年 Agent 开发需要掌握哪些核心技术》，五分钟后发布。"
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "PARTIAL_SUCCESS"}
WAITING = {"WAITING_USER", "WAITING_HUMAN", "WAITING_SEMANTIC_CONFIRMATION", "WAITING_APPROVAL"}


class FocusedE2E:
    def __init__(self, output: Path, timeout: int) -> None:
        load_dotenv(".env")
        self.output = output
        self.timeout = timeout
        self.agent = f"http://127.0.0.1:{os.getenv('GREENBOOK_AGENT_API_PORT', '8094')}"
        self.java = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        self.client = httpx.Client(timeout=30.0)
        self.java_headers: dict[str, str] = {}
        self.approved_execution_ids: list[str] = []

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: Any = None) -> tuple[int, Any]:
        request_headers = dict(headers or {})
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["content"] = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        if request_headers:
            kwargs["headers"] = request_headers
        response = self.client.request(method, url, **kwargs)
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:4000]
        return response.status_code, payload

    def login(self) -> None:
        status, payload = self.request(
            "POST",
            f"{self.java}/api/v1/auth/login",
            body={
                "identifierType": os.getenv("GREENBOOK_E2E_IDENTIFIER_TYPE", "EMAIL"),
                "identifier": os.getenv("GREENBOOK_E2E_IDENTIFIER", ""),
                "password": os.getenv("GREENBOOK_E2E_PASSWORD", ""),
                "code": None,
            },
        )
        if status != 200:
            raise AssertionError(f"Java login failed: HTTP {status}")
        token = str(((payload.get("token") or {}).get("accessToken")) or "")
        if not token:
            raise AssertionError("Java login returned no access token")
        self.java_headers = {"Authorization": f"Bearer {token}"}

    def java_resources(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("posts", "/api/v1/agent/me/posts?page=1&size=100"),
            ("drafts", "/api/v1/agent/me/drafts"),
            ("schedules", "/api/v1/agent/publications/schedules"),
        ):
            status, payload = self.request("GET", self.java + path, headers=self.java_headers)
            result[name] = {"status": status, "body": payload}
        return result

    def create_conversation(self) -> str:
        status, payload = self.request(
            "POST",
            f"{self.agent}/api/v1/agent/conversations",
            headers={"Authorization": self.java_headers["Authorization"]},
            body={"title": "focused semantic confirmation UTF8", "surface": "HOME"},
        )
        if status != 200:
            raise AssertionError(f"conversation create failed: HTTP {status}")
        return str(payload["conversation_id"])

    def agent_get(self, path: str) -> tuple[int, Any]:
        return self.request(
            "GET",
            self.agent + path,
            headers={"Authorization": self.java_headers["Authorization"]},
        )

    def send_message(self, conversation_id: str) -> dict[str, Any]:
        status, payload = self.request(
            "POST",
            f"{self.agent}/api/v1/agent/conversations/{conversation_id}/messages",
            headers={
                "Authorization": self.java_headers["Authorization"],
                "Idempotency-Key": uuid.uuid4().hex,
            },
            body={"content": MESSAGE, "client_timezone": "Asia/Shanghai"},
        )
        if status != 202:
            raise AssertionError(f"message submit failed: HTTP {status} {payload}")
        return payload

    def wait_for(self, predicate: Callable[[], Any], label: str) -> Any:
        deadline = time.monotonic() + self.timeout
        last: Any = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(1.0)
        raise AssertionError(f"timed out waiting for {label}: {last}")

    def projection(self, conversation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("tasks", f"/api/v1/agent/conversations/{conversation_id}/tasks"),
            ("messages", f"/api/v1/agent/conversations/{conversation_id}/messages"),
            ("activities", f"/api/v1/agent/conversations/{conversation_id}/activities"),
        ):
            status, payload = self.agent_get(path)
            result[name] = {"status": status, "body": payload}
        return result

    @staticmethod
    def semantic_trace(run_id: str) -> dict[str, Any]:
        path = Path(
            os.getenv(
                "GREENBOOK_DEBUG_INTERPRETER_FILE",
                ".runtime/focused-e2e/interpreter-structured.jsonl",
            )
        )
        if not path.exists():
            return {"records": 0, "counts": {}}
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = [record for record in records if record.get("run_id") == run_id]
        counts: dict[str, int] = {}
        for record in records:
            payload = record.get("payload") or {}
            count = payload.get("item_count")
            if count is None and isinstance(payload.get("items"), list):
                count = len(payload["items"])
            if count is None:
                count = payload.get("objective_count")
            if count is not None:
                counts[str(record.get("stage") or "")] = int(count)
        return {"records": len(records), "counts": counts}

    @staticmethod
    def activity(projection: dict[str, Any]) -> dict[str, Any] | None:
        body = ((projection.get("activities") or {}).get("body") or {})
        for item in body.get("items") or []:
            if item.get("activity_type") == "NEEDS_SEMANTIC_CONFIRMATION":
                return item
        return None

    def wait_pending(self, conversation_id: str, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        def read() -> tuple[dict[str, Any], dict[str, Any]] | None:
            run_status, run = self.agent_get(f"/api/v1/agent/runs/{run_id}")
            if run_status != 200 or not isinstance(run, dict):
                return None
            projection = self.projection(conversation_id)
            activity = self.activity(projection)
            if activity:
                return run, projection
            if str(run.get("status") or "") in TERMINAL:
                raise AssertionError(
                    f"run became terminal without semantic confirmation: {run.get('status')}"
                )
            return None

        return self.wait_for(read, "semantic confirmation pending")

    def wait_settled(self, conversation_id: str, run_id: str) -> dict[str, Any]:
        approved: set[str] = set()

        def approve_activity_approvals() -> bool:
            """Continue the existing Risk Approval path for immediate publish."""

            projection = self.projection(conversation_id)
            activities = ((projection.get("activities") or {}).get("body") or {}).get("items") or []
            if not any(
                item.get("run_id") == run_id
                and item.get("activity_type") == "NEEDS_APPROVAL"
                and item.get("status") == "WAITING_APPROVAL"
                for item in activities
            ):
                return False
            task_items = ((projection.get("tasks") or {}).get("body") or {}).get("items") or []
            waiting_execution_ids = {
                str(ref.get("execution_id") or ref.get("executionId") or "")
                for task in task_items
                for ref in (task.get("execution_refs") or [])
                if isinstance(ref, dict) and str(ref.get("status") or "") == "WAITING_HUMAN"
            }
            changed = False
            for execution_id in sorted(waiting_execution_ids):
                if not execution_id or execution_id in approved:
                    continue
                approve_status, _ = self.request(
                    "POST",
                    f"{self.agent}/api/v1/agent/executions/{execution_id}/approve",
                    headers={"Authorization": self.java_headers["Authorization"]},
                    body={"decision": "APPROVE"},
                )
                if approve_status not in {200, 409}:
                    raise AssertionError(
                        f"execution approval failed: HTTP {approve_status}"
                    )
                approved.add(execution_id)
                self.approved_execution_ids.append(execution_id)
                changed = True
            return changed

        def read() -> dict[str, Any] | None:
            status, run = self.agent_get(f"/api/v1/agent/runs/{run_id}")
            if status != 200 or not isinstance(run, dict):
                return None
            approval_id = str(run.get("approval_id") or "")
            if str(run.get("status") or "") == "WAITING_APPROVAL" and approval_id and approval_id not in approved:
                approve_status, approval_result = self.request(
                    "POST",
                    f"{self.agent}/api/v1/agent/runs/{run_id}/approvals/{approval_id}",
                    headers={"Authorization": self.java_headers["Authorization"]},
                    body={"decision": "APPROVE"},
                )
                if approve_status not in {200, 409}:
                    raise AssertionError(f"approval failed: HTTP {approve_status}")
                approved.add(approval_id)
                return None
            if approve_activity_approvals():
                return None
            if str(run.get("status") or "") in TERMINAL:
                return run
            return None

        return self.wait_for(read, "confirmed Task continuation")

    def confirm(self, task_id: str, safe_payload: dict[str, Any]) -> tuple[Any, Any]:
        body = {
            "action": "CONFIRM",
            "confirmation_id": safe_payload["confirmation_id"],
            "expected_task_version": safe_payload["task_version"],
            "expected_confirmation_version": safe_payload["confirmation_version"],
        }
        headers = {"Authorization": self.java_headers["Authorization"]}
        first = self.request(
            "POST", f"{self.agent}/api/v1/agent/tasks/{task_id}/semantic-confirmation",
            headers=headers, body=body,
        )
        duplicate = self.request(
            "POST", f"{self.agent}/api/v1/agent/tasks/{task_id}/semantic-confirmation",
            headers=headers, body=body,
        )
        if first[0] != 200 or duplicate[0] != 200:
            raise AssertionError(f"confirm failed: {first[0]} / {duplicate[0]}")
        if duplicate[1].get("idempotent") is not True:
            raise AssertionError("duplicate confirm was not idempotent")
        return first, duplicate


def _items(resource: dict[str, Any]) -> list[dict[str, Any]]:
    body = resource.get("body")
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("items", "records", "content", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _resource_id(item: dict[str, Any]) -> str:
    for key in ("id", "postId", "draftId", "scheduleId", "publicationId"):
        if item.get(key) not in (None, ""):
            return str(item[key])
    return ""


def _resource_signature(resources: dict[str, Any]) -> dict[str, list[str]]:
    return {
        name: sorted(
            f"{_resource_id(item)}|{item.get('title') or item.get('postTitle') or item.get('draftTitle') or item.get('name') or ''}|{item.get('status') or item.get('publishStatus') or item.get('state') or ''}"
            for item in _items(value)
        )
        for name, value in resources.items()
    }


def _field(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _find_title(resources: dict[str, Any], title: str) -> dict[str, Any] | None:
    for name, resource in resources.items():
        for item in _items(resource):
            if _field(item, "title", "postTitle", "draftTitle", "name") == title:
                return {"collection": name, "item": item}
    return None


def _find_new_title(
    before: dict[str, Any],
    after: dict[str, Any],
    title: str,
    preferred_collections: tuple[str, ...],
) -> dict[str, Any] | None:
    collection_order = [
        *preferred_collections,
        *(name for name in after if name not in preferred_collections),
    ]
    for name in collection_order:
        before_ids = {_resource_id(item) for item in _items(before.get(name, {}))}
        for item in _items(after.get(name, {})):
            if (
                _field(item, "title", "postTitle", "draftTitle", "name") == title
                and _resource_id(item)
                and _resource_id(item) not in before_ids
            ):
                return {"collection": name, "item": item}
    return None


def _find_new_schedule_for_title(
    before: dict[str, Any],
    after: dict[str, Any],
    title: str,
) -> dict[str, Any] | None:
    """Join the new schedule to its new draft; schedules have no title field."""

    draft = _find_new_title(before, after, title, ("drafts",))
    if draft is None:
        return None
    draft_id = _resource_id(draft["item"])
    before_schedule_ids = {
        _resource_id(item) for item in _items(before.get("schedules", {}))
    }
    for schedule in _items(after.get("schedules", {})):
        if (
            str(_field(schedule, "draftId", "draft_id") or "") == draft_id
            and _resource_id(schedule)
            and _resource_id(schedule) not in before_schedule_ids
        ):
            return {"collection": "schedules", "item": schedule, "draft": draft["item"]}
    return None


def _instant(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".runtime/focused-e2e/semantic-confirmation.json")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    runner = FocusedE2E(output, args.timeout)
    result: dict[str, Any] = {"message": MESSAGE, "message_codepoints": [ord(char) for char in MESSAGE]}
    try:
        runner.login()
        before_java = runner.java_resources()
        conversation_id = runner.create_conversation()
        accepted = runner.send_message(conversation_id)
        run_id = str(accepted.get("run_id") or "")
        if not run_id:
            raise AssertionError("accepted response contained no run_id")
        pending_run, pending_projection = runner.wait_pending(conversation_id, run_id)
        pending_activity = runner.activity(pending_projection)
        assert pending_activity is not None
        safe_payload = dict(pending_activity.get("safe_payload") or {})
        task_id = str(pending_activity.get("task_id") or "")
        if len(safe_payload.get("objectives") or []) != 2:
            raise AssertionError("confirmation card did not contain two objectives")
        if not task_id:
            raise AssertionError("confirmation activity did not bind a Task")
        semantic_trace_before = runner.semantic_trace(run_id)
        expected_trace_counts = {
            "raw": 2,
            "schema_parse": 2,
            "normalized": 2,
            "segmentation": 2,
            "semantic_derivation": 2,
            "turn_command": 2,
            "resolved_semantic_state": 2,
            "objective_attach": 2,
        }
        if any(
            semantic_trace_before["counts"].get(stage) != count
            for stage, count in expected_trace_counts.items()
        ):
            raise AssertionError(
                f"semantic stage count mismatch: {semantic_trace_before}"
            )
        pending_task_index = ((pending_projection.get("tasks") or {}).get("body") or {}).get("items") or []
        pending_messages = ((pending_projection.get("messages") or {}).get("body") or [])
        persisted_user_messages = [
            item.get("content") for item in pending_messages
            if item.get("role") == "user"
        ]
        if MESSAGE not in persisted_user_messages:
            raise AssertionError("persisted API message did not round-trip exact UTF-8 input")
        if pending_run.get("execution_id") or (pending_run.get("partial_results") or {}).get("execution_ids"):
            raise AssertionError("pending confirmation already exposed an execution")
        pending_execution_refs = [
            ref for task in pending_task_index for ref in (task.get("execution_refs") or [])
        ]
        if pending_execution_refs:
            raise AssertionError("pending confirmation already had durable execution refs")
        before_confirmation_java = runner.java_resources()
        if _resource_signature(before_java) != _resource_signature(before_confirmation_java):
            raise AssertionError("Java resources changed before confirmation")

        first_confirm, duplicate_confirm = runner.confirm(task_id, safe_payload)
        final_run = runner.wait_settled(conversation_id, run_id)
        final_projection = runner.projection(conversation_id)
        semantic_trace_after = runner.semantic_trace(run_id)
        if semantic_trace_after["counts"].get("raw") != semantic_trace_before["counts"].get("raw"):
            raise AssertionError("Confirm re-entered CommandInterpreter")
        after_java = runner.java_resources()
        final_task_index = ((final_projection.get("tasks") or {}).get("body") or {}).get("items") or []
        final_execution_refs = [
            ref for task in final_task_index for ref in (task.get("execution_refs") or [])
        ]
        execution_ids = [
            str(ref.get("execution_id") or ref.get("executionId") or "")
            for ref in final_execution_refs if isinstance(ref, dict)
        ]
        execution_ids = [value for value in execution_ids if value]
        if len(execution_ids) != len(set(execution_ids)):
            raise AssertionError("duplicate physical execution identity detected")
        raw_texts = [
            item.get("content") for item in ((final_projection.get("messages") or {}).get("body") or [])
            if item.get("role") == "user"
        ]
        if raw_texts.count(MESSAGE) != 1:
            raise AssertionError("confirmation created a second Interpreter/user message")

        result.update({
            "conversation_id": conversation_id,
            "run_id": run_id,
            "accepted": accepted,
            "pending_run": pending_run,
            "pending_activity": pending_activity,
            "semantic_trace_before": semantic_trace_before,
            "semantic_trace_after": semantic_trace_after,
            "pending_task_index": pending_task_index,
            "before_java": before_java,
            "before_confirmation_java": before_confirmation_java,
            "first_confirm": first_confirm[1],
            "duplicate_confirm": duplicate_confirm[1],
            "approved_execution_ids": runner.approved_execution_ids,
            "final_run": final_run,
            "final_task_index": final_task_index,
            "after_java": after_java,
            "persisted_user_messages": raw_texts,
            "before_confirmation_zero_write": True,
            "duplicate_execution_identity": False,
            "java_signature_before": _resource_signature(before_java),
            "java_signature_before_confirm": _resource_signature(before_confirmation_java),
            "java_signature_after": _resource_signature(after_java),
            "java_truth": {
                "published_a": _find_new_title(
                    before_confirmation_java,
                    after_java,
                    "Java 后端实习面试最容易被问到的 10 个问题",
                    ("posts",),
                ),
                "scheduled_b": _find_new_schedule_for_title(
                    before_confirmation_java,
                    after_java,
                    "2026 年 Agent 开发需要掌握哪些核心技术",
                ),
            },
        })
        if not result["java_truth"]["published_a"] or not result["java_truth"]["scheduled_b"]:
            raise AssertionError("final Java truth did not contain both requested titles")
        published = result["java_truth"]["published_a"]["item"]
        scheduled = result["java_truth"]["scheduled_b"]["item"]
        if str(_field(published, "status", "publishStatus", "state") or "").lower() not in {
            "published",
            "publish",
        }:
            raise AssertionError(f"Objective A is not published: {published}")
        if str(_field(scheduled, "status", "publishStatus", "state") or "").lower() not in {
            "scheduled",
            "schedule",
        }:
            raise AssertionError(f"Objective B is not scheduled: {scheduled}")
        expected_run_at = (safe_payload.get("objectives") or [])[1].get("run_at")
        actual_run_at = _field(scheduled, "runAt", "run_at", "scheduledAt", "scheduled_at")
        if _instant(expected_run_at) is None or _instant(actual_run_at) is None:
            raise AssertionError("Objective B has no canonical schedule time")
        if abs(_instant(expected_run_at) - _instant(actual_run_at)) > 1.0:
            raise AssertionError(
                f"Objective B schedule binding changed: {expected_run_at} != {actual_run_at}"
            )
        result["status"] = "PASS"
    except Exception as exc:  # keep all evidence available for diagnosis
        result["status"] = "FAIL"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": result.get("status"),
            "output": str(output),
            "error": result.get("error"),
        }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
