"""Live GreenBook Product E2E collector.

This is a test harness only.  It calls the existing Frontend, Agent API and
Java Agent Facade; it does not create a second runtime or infer business truth
from Agent final text.  Every result is written under ``.runtime/e2e`` with a
unique test tag so Java resources can be reconciled after the run.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "PARTIAL_SUCCESS"}
HUMAN = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}
EXTERNAL_WAIT = {"WAITING_EXTERNAL"}


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    seed_id: str
    turns: tuple[str, ...]
    add_title_marker: bool = True


CASE_SPECS: tuple[CaseSpec, ...] = (
    CaseSpec(
        "case01-simple-read",
        "search-create-schedule",
        ("搜几篇最近比较热门的 Java 面试帖子",),
        False,
    ),
    CaseSpec(
        "case02-simple-draft",
        "search-create-schedule",
        ("写一篇 Java 后端学习帖子，保存草稿",),
    ),
    CaseSpec(
        "case03-publish-now",
        "multi-publication-binding",
        ("写一篇 Java 后端学习帖子并立即发布",),
    ),
    CaseSpec(
        "case04-schedule",
        "unresolved-future",
        ("写一篇 Agent 开发学习帖子，五分钟后发布",),
    ),
    CaseSpec(
        "case05-multi-objective",
        "multi-publication-binding",
        (
            "写两篇帖子，一篇《Java 后端如何学习》立即发布，一篇《Agent 开发如何学习》五分钟后发布",
        ),
    ),
    CaseSpec(
        "case06-three-objective",
        "multi-publication-binding",
        (
            "写三篇帖子：Java 后端如何学习立即发布；Agent 开发如何学习五分钟后发布；Redis 学习保存草稿",
        ),
    ),
    CaseSpec(
        "case07-cross-turn-update",
        "cross-turn-schedule-update",
        (
            "写一篇 Java 学习帖子，五分钟后发布",
            "Java 那篇改成明天下午四点发布",
        ),
    ),
    CaseSpec(
        "case08-ambiguous-target",
        "ambiguous-delete",
        ("Java 那篇删掉",),
    ),
    CaseSpec(
        "case09-delete-hitl",
        "ambiguous-delete",
        ("删除这篇帖子",),
    ),
    CaseSpec(
        "case10-mid-run-change",
        "cross-turn-schedule-update",
        (
            "写两篇帖子，一篇 Java 学习五分钟后发布，一篇 Agent 学习五分钟后发布",
            "两篇都改到明天下午三点发布",
        ),
    ),
    CaseSpec(
        "case11-partial-failure",
        "multi-publication-binding",
        (
            "写两篇帖子，一篇 Java 学习立即发布，一篇 Agent 学习五分钟后发布",
        ),
    ),
    CaseSpec(
        "case12-result-unknown",
        "search-create-schedule",
        ("写一篇 Redis 学习帖子，五分钟后发布",),
    ),
    CaseSpec(
        "case13-no-progress",
        "unresolved-future",
        ("把这件事继续处理",),
    ),
    CaseSpec(
        "case14-search-create",
        "search-create-schedule",
        (
            "搜索最近关于 Agent 学习的帖子，参考这些内容写一篇《Agent 开发学习路线》，保存草稿",
        ),
    ),
    CaseSpec(
        "case15-search-create-schedule",
        "search-create-schedule",
        (
            "搜索最近关于 Agent 学习的帖子，参考这些内容写一篇《Agent 开发学习路线》，五分钟后发布",
        ),
    ),
)


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


class LiveE2E:
    def __init__(self, output_dir: Path, timeout_seconds: int) -> None:
        load_dotenv(".env")
        self.java = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        self.agent = f"http://127.0.0.1:{os.getenv('GREENBOOK_AGENT_API_PORT', '8094')}"
        self.frontend = "http://127.0.0.1:5173"
        self.timeout_seconds = timeout_seconds
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=30.0)
        self.token = ""
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        json_body = kwargs.pop("json", None)
        if json_body is not None:
            # Keep the harness independent of the host console/code page.  A
            # Python file launched from PowerShell should still put the exact
            # Unicode code points on the HTTP wire.
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
            kwargs["content"] = body
            kwargs["headers"] = headers
        response = self.client.request(method, url, **kwargs)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:2000]
        return response.status_code, body

    def login(self) -> dict[str, Any]:
        status, body = self.request(
            "POST",
            f"{self.java}/api/v1/auth/login",
            json={
                "identifierType": os.getenv("GREENBOOK_E2E_IDENTIFIER_TYPE", "EMAIL"),
                "identifier": os.getenv("GREENBOOK_E2E_IDENTIFIER", ""),
                "password": os.getenv("GREENBOOK_E2E_PASSWORD", ""),
                "code": None,
            },
        )
        if status != 200:
            raise RuntimeError(f"Java login failed: HTTP {status}")
        self.token = str(((body.get("token") or {}).get("accessToken")) or "")
        if not self.token:
            raise RuntimeError("Java login returned no access token")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        user = body.get("user") or {}
        if str(user.get("role") or "") != "USER":
            raise RuntimeError(f"E2E account is not USER: {user.get('role')}")
        return {"user_id": user.get("id"), "role": user.get("role")}

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, url in (
            ("java", f"{self.java}/actuator/health"),
            ("agent", f"{self.agent}/health"),
            ("frontend", f"{self.frontend}/"),
            ("frontend_agent_proxy", f"{self.frontend}/agent-api/health"),
        ):
            status, body = self.request("GET", url)
            result[name] = {"status": status, "body": body}
        return result

    def java_resources(self) -> dict[str, Any]:
        resources: dict[str, Any] = {}
        for name, url in (
            ("posts", f"{self.java}/api/v1/agent/me/posts?page=1&size=100"),
            ("drafts", f"{self.java}/api/v1/agent/me/drafts"),
            ("schedules", f"{self.java}/api/v1/agent/publications/schedules"),
        ):
            status, body = self.request("GET", url, headers=self.headers)
            resources[name] = {"status": status, "items": body if status == 200 else []}
        return resources

    def create_conversation(self, title: str) -> str:
        status, body = self.request(
            "POST",
            f"{self.agent}/api/v1/agent/conversations",
            headers=self.headers,
            json={"title": title, "surface": "HOME"},
        )
        if status != 200:
            raise RuntimeError(f"Conversation create failed: HTTP {status} {body}")
        return str(body["conversation_id"])

    def send_turn(self, conversation_id: str, content: str) -> dict[str, Any]:
        status, accepted = self.request(
            "POST",
            f"{self.agent}/api/v1/agent/conversations/{conversation_id}/messages",
            headers={**self.headers, "Idempotency-Key": uuid.uuid4().hex},
            json={"content": content, "client_timezone": "Asia/Shanghai"},
        )
        if status != 202:
            return {"accepted_status": status, "accepted": accepted}
        run_id = str(accepted.get("run_id") or "")
        started = time.monotonic()
        run: dict[str, Any] = {}
        while time.monotonic() - started < self.timeout_seconds:
            time.sleep(2.0)
            run_status, body = self.request(
                "GET", f"{self.agent}/api/v1/agent/runs/{run_id}", headers=self.headers
            )
            if run_status == 200 and isinstance(body, dict):
                run = body
                if str(body.get("status") or "") in TERMINAL | HUMAN | EXTERNAL_WAIT:
                    break
        return {
            "accepted_status": status,
            "accepted": accepted,
            "run": run,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    def wait_run_terminal(self, run_id: str, initial: dict[str, Any] | None = None) -> dict[str, Any]:
        """Observe a submitted Run until its durable continuation settles.

        This is harness observation only.  It never submits another Agent
        turn and never invokes an LLM while the Run is WAITING_EXTERNAL.
        """
        started = time.monotonic()
        run = dict(initial or {})
        while time.monotonic() - started < self.timeout_seconds:
            time.sleep(2.0)
            status, body = self.request(
                "GET", f"{self.agent}/api/v1/agent/runs/{run_id}", headers=self.headers
            )
            if status == 200 and isinstance(body, dict):
                run = body
                if str(body.get("status") or "") in TERMINAL | HUMAN:
                    break
        return run

    def conversation_projection(self, conversation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("tasks", f"/api/v1/agent/conversations/{conversation_id}/tasks"),
            ("messages", f"/api/v1/agent/conversations/{conversation_id}/messages"),
            ("activities", f"/api/v1/agent/conversations/{conversation_id}/activities"),
        ):
            status, body = self.request("GET", self.agent + path, headers=self.headers)
            result[name] = {"status": status, "body": body}
        return result

    def run_case(self, spec: CaseSpec, tag: str) -> dict[str, Any]:
        title = f"P0 {tag} {spec.case_id}"
        conversation_id = self.create_conversation(title)
        before = self.java_resources()
        turns: list[dict[str, Any]] = []
        for index, message in enumerate(spec.turns, start=1):
            content = message
            if spec.add_title_marker and index == 1:
                content += (
                    f"。本轮内部验收标记为 {tag}，请把它作为新建文章标题末尾的测试标记，"
                    "不要改变主题、目标或发布时间。"
                )
            turn = self.send_turn(conversation_id, content)
            if str((turn.get("run") or {}).get("status") or "") in EXTERNAL_WAIT:
                run_id = str(
                    ((turn.get("accepted") or {}).get("run_id"))
                    or ((turn.get("run") or {}).get("run_id"))
                    or ""
                )
                if run_id:
                    turn["run"] = self.wait_run_terminal(run_id, turn.get("run"))
            turns.append({"turn": index, "user_message": content, **turn})
            if str((turn.get("run") or {}).get("status") or "") in {"FAILED", "CANCELLED"}:
                break
        after = self.java_resources()
        projection = self.conversation_projection(conversation_id)
        return {
            "case_id": spec.case_id,
            "seed_id": spec.seed_id,
            "test_run_tag": tag,
            "conversation_id": conversation_id,
            "before_java": before,
            "turns": turns,
            "after_java": after,
            "projection": projection,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--tag", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    tag = args.tag or f"E2E-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8].upper()}"
    output_dir = Path(args.output) if args.output else Path(".runtime/e2e") / tag
    selected = [
        spec for spec in CASE_SPECS if not args.case_ids or spec.case_id in set(args.case_ids)
    ]
    if not selected:
        raise SystemExit("No matching --case value")

    runner = LiveE2E(output_dir, args.timeout)
    environment = {"test_run_tag": tag, "health": runner.health(), "auth": runner.login()}
    (output_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2, default=_json), encoding="utf-8"
    )
    summary: list[dict[str, Any]] = []
    for spec in selected:
        print(f"[{tag}] START {spec.case_id}", flush=True)
        result = runner.run_case(spec, tag)
        (output_dir / f"{spec.case_id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=_json), encoding="utf-8"
        )
        last_run = (result.get("turns") or [{}])[-1].get("run") or {}
        row = {
            "case_id": spec.case_id,
            "conversation_id": result.get("conversation_id"),
            "status": last_run.get("status"),
            "error_code": last_run.get("error_code"),
            "execution_path": last_run.get("execution_path"),
            "execution_id": last_run.get("execution_id"),
            "activity_count": len(((result.get("projection") or {}).get("activities") or {}).get("body", {}).get("items", [])),
        }
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json), encoding="utf-8"
    )
    print(json.dumps({"test_run_tag": tag, "output_dir": str(output_dir), "summary": summary}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
