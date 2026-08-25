"""Focused browser-only closure for Golden 3/7/9.

This runner intentionally keeps confirmation and approval actions on the real
Frontend DOM.  It uses API/Java reads only for observation and evidence.  All
business input is constructed in Python and sent through CDP, never through a
PowerShell string boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.dev.overnight_stable_baseline_browser import (
    Browser,
    find_page,
    run_turn,
)


OUT = ROOT / ".runtime" / "round1-final-v2"
OUT.mkdir(parents=True, exist_ok=True)
TRACE = ROOT / ".runtime" / "golden-p0-diagnostic-20260824" / "interpreter.jsonl"


def trace_for(run_id: str) -> list[dict[str, Any]]:
    if not TRACE.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in TRACE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(item.get("run_id") or "") != run_id:
            continue
        payload = item.get("payload") or {}
        if isinstance(payload, dict):
            rows.append(
                {
                    "stage": item.get("stage"),
                    "payload": {
                        key: payload.get(key)
                        for key in (
                            "raw",
                            "normalized",
                            "resolved",
                            "target_candidates",
                            "objective_count",
                            "items",
                            "needs_clarification",
                            "reason",
                        )
                        if key in payload
                    },
                }
            )
    return rows


class JavaTruth:
    def __init__(self) -> None:
        load_dotenv(ROOT / ".env")
        self.base = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        self.client = httpx.Client(timeout=30.0, trust_env=False)
        self.headers: dict[str, str] = {}
        self.auth_response: dict[str, Any] = {}

    def login(self) -> None:
        body = {
            "identifierType": os.getenv("GREENBOOK_E2E_IDENTIFIER_TYPE", "EMAIL"),
            "identifier": os.getenv("GREENBOOK_E2E_IDENTIFIER", ""),
            "password": os.getenv("GREENBOOK_E2E_PASSWORD", ""),
            "code": None,
        }
        response = self.client.post(
            f"{self.base}/api/v1/auth/login",
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        response.raise_for_status()
        self.auth_response = response.json()
        token = str(((self.auth_response.get("token") or {}).get("accessToken")) or "")
        if not token:
            raise RuntimeError("Java login returned no token")
        self.headers = {"Authorization": f"Bearer {token}"}

    def resources(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("posts", "/api/v1/agent/me/posts?page=1&size=100"),
            ("drafts", "/api/v1/agent/me/drafts"),
            ("schedules", "/api/v1/agent/publications/schedules"),
        ):
            response = self.client.get(f"{self.base}{path}", headers=self.headers)
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text[:2000]
            result[name] = {"status": response.status_code, "body": body}
        return result

    def close(self) -> None:
        self.client.close()


async def restore_browser_auth(browser: Browser, truth: JavaTruth) -> None:
    """Restore the supported test-account session for the real Frontend.

    The business actions remain browser UI actions.  This setup step only
    restores the same JWT pair after the browser's old session expired, then
    lets AuthContext fetch /auth/me through the normal Frontend path.
    """

    token = truth.auth_response.get("token") or {}
    access = str(token.get("accessToken") or "")
    refresh = str(token.get("refreshToken") or "")
    expires = token.get("accessTokenExpiresAt")
    user = truth.auth_response.get("user") or {}
    if not access or not refresh or expires in (None, ""):
        raise RuntimeError("login response cannot restore browser auth state")
    payload = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires,
    }
    await browser.evaluate(
        f"""(()=>{{
          localStorage.setItem('zhiguang_auth_tokens', {json.dumps(json.dumps(payload, ensure_ascii=False))});
          localStorage.setItem('zhiguang_current_user', {json.dumps(json.dumps(user, ensure_ascii=False))});
          location.href='/';
          return true;
        }})()"""
    )
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        snapshot = await browser.snapshot()
        body = str(snapshot.get("body") or "")
        ready = await browser.evaluate(
            "Boolean(document.querySelector('button[class*=agentTrigger]'))"
        )
        if (
            "/login" not in str(snapshot.get("url") or "")
            and "登录状态已失效" not in body
            and "退出" in body
            and ready
        ):
            return
        await asyncio.sleep(0.4)
    raise RuntimeError("Frontend did not restore the authenticated Agent panel")


def items(resource: dict[str, Any]) -> list[dict[str, Any]]:
    body = resource.get("body")
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("items", "records", "content", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def find_title(resources: dict[str, Any], title: str) -> dict[str, Any] | None:
    for collection, resource in resources.items():
        for item in items(resource):
            item_title = item.get("title") or item.get("postTitle") or item.get("draftTitle")
            if str(item_title or "") == title:
                return {"collection": collection, "item": item}
    return None


def new_items(before: dict[str, Any], after: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    old_ids = {
        str(item.get("postId") or item.get("draftId") or item.get("id") or "")
        for item in items(before.get(collection, {}))
    }
    return [
        item
        for item in items(after.get(collection, {}))
        if str(item.get("postId") or item.get("draftId") or item.get("id") or "") not in old_ids
    ]


async def run_case(
    browser: Browser,
    name: str,
    turns: list[tuple[str, dict[str, Any]]],
    timeout: float,
    truth: JavaTruth,
) -> dict[str, Any]:
    conversation_id = await browser.new_conversation(f"GreenBook Stable Round 1 v2 {name}")
    before = truth.resources()
    rows: list[dict[str, Any]] = []
    for index, (text, policy) in enumerate(turns, start=1):
        result = await run_turn(browser, conversation_id, text, policy, timeout)
        run_id = str(result.get("run_id") or "")
        result["turn"] = index
        result["trace"] = trace_for(run_id)
        result["java_after_turn"] = truth.resources()
        rows.append(result)
        # A failed or unattended human state is evidence for this case; do
        # not continue by guessing or calling an internal API.
        if str(result.get("status") or "") in {"FAILED", "HARNESS_ERROR", "WAITING_USER", "WAITING_APPROVAL"}:
            break
        await asyncio.sleep(0.8)
    after = truth.resources()
    return {
        "name": name,
        "conversation_id": conversation_id,
        "before_java": before,
        "turns": rows,
        "after_java": after,
        "new_posts": new_items(before, after, "posts"),
        "new_drafts": new_items(before, after, "drafts"),
        "new_schedules": new_items(before, after, "schedules"),
    }


async def main() -> None:
    tag = time.strftime("%Y%m%d-%H%M%S")
    unique_title = f"绿书稳定性实战：Java 后端实践 {tag}"
    truth = JavaTruth()
    truth.login()
    browser = Browser(find_page())
    await browser.connect()
    try:
        await restore_browser_auth(browser, truth)
        await browser.open_panel()
        cases = [
            await run_case(
                browser,
                "golden3",
                [
                    (
                        "写一篇 Java 后端学习指南并立即发布",
                        {"hitl_sequence": ["confirm", "approve"]},
                    )
                ],
                300.0,
                truth,
            ),
            await run_case(
                browser,
                "golden7",
                [
                    (
                        "写一篇 Java 学习帖子，五分钟后发布",
                        {"hitl": "confirm"},
                    ),
                    (
                        "把刚才那篇 Java 学习帖子的发布时间改到明天下午四点",
                        {},
                    ),
                ],
                300.0,
                truth,
            ),
            await run_case(
                browser,
                "golden9",
                [
                    (
                        f"请写一篇标题为《{unique_title}》的 Java 后端实践帖子并立即发布",
                        {"hitl_sequence": ["confirm", "approve"]},
                    ),
                    (
                        "删除这篇帖子",
                        {"hitl": "approve"},
                    ),
                ],
                300.0,
                truth,
            ),
        ]
    finally:
        await browser.close()
        truth.close()
    output = OUT / "golden-3-7-9-v2.json"
    output.write_text(json.dumps({"tag": tag, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep the console summary ASCII-safe on the Windows PowerShell code page.
    # The full evidence remains UTF-8 in the JSON artifact above.
    summary = {
        "output": str(output),
        "tag": tag,
        "cases": [
            {
                "name": case.get("name"),
                "turns": [
                    {
                        "turn": turn.get("turn"),
                        "status": turn.get("status"),
                        "run_id": turn.get("run_id"),
                        "clicked_hitl": turn.get("clicked_hitl"),
                    }
                    for turn in case.get("turns", [])
                ],
            }
            for case in cases
        ],
    }
    print(json.dumps(summary, ensure_ascii=True, default=str))


if __name__ == "__main__":
    asyncio.run(main())
