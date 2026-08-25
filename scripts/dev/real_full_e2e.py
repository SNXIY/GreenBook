"""Real full-flow E2E: run all six multi-turn cases against the live stack.

Uses one USER account (serialized — the runtime caps concurrent runs per
user). Each case is an independent conversation; each turn waits for its Run
to reach a terminal state before the next turn starts. Results are written as
JSON lines to .tmp-e2e-full/result.jsonl for cheap review.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

JAVA = "http://127.0.0.1:8080"
API = "http://127.0.0.1:8094"
PHONE = "10001000000"
PASSWORD = "FanZK061345%"
TIMEOUT_SECONDS = 600
POLL_SECONDS = 2.0

OUT_DIR = Path(__file__).resolve().parents[2] / ".tmp-e2e-full"
OUT_DIR.mkdir(exist_ok=True)
RESULT = OUT_DIR / "result.jsonl"

TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
HUMAN = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}

client = httpx.Client(timeout=30)


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def record(case: str, turn: int, **fields) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "case": case,
        "turn": turn,
        **fields,
    }
    with RESULT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def login() -> str:
    r = client.post(
        f"{JAVA}/api/v1/auth/login",
        json={"identifierType": "PHONE", "identifier": PHONE, "password": PASSWORD, "code": None},
    )
    r.raise_for_status()
    token = r.json()["token"]["accessToken"]
    log(f"login OK user_id={r.json()['user']['id']}")
    return token


def new_conversation(token: str, title: str) -> str:
    r = client.post(
        f"{API}/api/v1/agent/conversations",
        json={"title": title, "surface": "HOME"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    conv = r.json()["conversation_id"]
    log(f"conversation created: {conv}")
    return conv


def send_and_wait(token: str, conv: str, content: str, case: str, turn: int) -> dict:
    started = time.perf_counter()
    # Keep the message bytes explicit. This client is also invoked from
    # PowerShell on Windows, where implicit stdin/console encodings can turn
    # non-ASCII text into question marks before httpx sees it.
    request_body = json.dumps(
        {"content": content, "client_timezone": "Asia/Shanghai"},
        ensure_ascii=False,
    ).encode("utf-8")
    r = client.post(
        f"{API}/api/v1/agent/conversations/{conv}/messages",
        content=request_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": uuid.uuid4().hex,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    if r.status_code != 202:
        record(case, turn, error=f"accept http {r.status_code}", body=r.text[:500])
        return {"error": f"http {r.status_code}: {r.text[:300]}"}
    accepted = r.json()
    run_id = accepted["run_id"]
    follow_up_of = accepted.get("follow_up_of")
    log(f"turn {turn} accepted run={run_id} follow_up_of={follow_up_of}")

    last = None
    deadline = time.perf_counter() + TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            rr = client.get(
                f"{API}/api/v1/agent/runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if rr.status_code != 200:
                continue
            last = rr.json()
        except httpx.HTTPError:
            continue
        status = str(last.get("status") or "")
        if status in TERMINAL or status in HUMAN:
            elapsed = round(time.perf_counter() - started, 1)
            summary = {
                "run_id": run_id,
                "status": status,
                "elapsed_s": elapsed,
                "execution_path": last.get("execution_path"),
                "model_calls": (last.get("budget") or {}).get("model_calls"),
                "error": last.get("error") or last.get("error_code"),
                "final_response": (last.get("final_response") or "")[:120],
                "follow_up_of": follow_up_of,
                "task_ids": last.get("task_ids") or [],
                "execution_ids": last.get("execution_ids") or [],
            }
            log(f"turn {turn} terminal status={status} elapsed={elapsed}s")
            record(case, turn, **summary)
            return summary
    record(case, turn, error="timeout", run_id=run_id)
    return {"error": "timeout", "run_id": run_id}


def task_items(token: str) -> list[dict]:
    try:
        r = client.get(
            f"{JAVA}/api/v1/knowposts/task-items",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 200:
            return r.json() or []
    except httpx.HTTPError:
        pass
    return []


def summarize_drafts(token: str, case: str, turn: int) -> None:
    items = task_items(token)
    drafts = [
        {
            "id": it.get("id"),
            "title": it.get("title"),
            "status": it.get("status"),
            "origin": it.get("contentOrigin"),
        }
        for it in items
        if it.get("status") in {"draft", "rejected", "published"}
    ]
    record(case, turn, task_items=drafts)
    log(f"drafts after turn {turn}: {json.dumps(drafts, ensure_ascii=False)[:400]}")


CASES = [
    {
        "name": "case1-multi-task-plus-edit",
        "turns": [
            "我准备下周做一个 Java 和 AI Agent 学习系列。先帮我安排这些事情：搜一下社区里最近比较热门的 Java 面试相关帖子，总结大家最关注的几个问题，然后基于这些内容写一篇《Java 后端实习面试最容易被问到的 10 个问题》，明天上午 9 点发布。再找一些最近关于 AI Agent 的讨论，写一篇《2026 年 Agent 开发需要掌握哪些核心技术》，明天下午 2 点发布。不用搜索，直接写一篇比较轻松的《为什么学了很多八股还是不会做项目》，后天晚上 8 点发布。顺便总结一下我这次一共安排了哪些内容，哪些已经设置发布时间，哪些还只是草稿。",
            "Java 那篇别明天早上发了，改成明天下午 4 点。Agent 那篇保持不变。第三篇标题有点太负面，把它改成《为什么学了很多技术还是做不好项目》，正文也改得更偏项目实践一点。",
        ],
    },
    {
        "name": "case2-delta-chain",
        "turns": [
            "帮我发布一篇如何学习Java的帖子，然后五分钟之后发布",
            "先去检索一些热门的关于agent学习的帖子，然后参考他们的设计思想，帮我发布一篇如何学习agent的帖子，然后明天上午八点发布",
            "刚刚Java那篇修改一下标题，要求新颖，吸引人，然后发布时间改成明天下午五点",
            "刚刚agent那篇取消发布",
        ],
    },
    {
        "name": "case3-same-type-multi-goal",
        "turns": [
            "帮我写三篇 Java 相关帖子。第一篇讲 Java 集合，明天上午八点发布。第二篇讲 JVM，明天下午两点发布。第三篇讲 Spring Boot，明天下午五点发布。",
            "把下午那篇改到晚上八点。",
        ],
    },
    {
        "name": "case4-modify-then-reference",
        "turns": [
            "搜一下 Redis 缓存相关的热门帖子，参考以后写一篇 Redis 学习指南，明天上午十点发布。再直接写一篇 MySQL 索引优化，明天下午三点发布。",
            "Redis 那篇改成下午四点。",
            "MySQL 那篇标题换一下。",
            "刚刚那篇正文再精简一点，但发布时间不要动。",
        ],
    },
    {
        "name": "case5-cancel-keep-draft",
        "turns": [
            "帮我写一篇《如何学习 LangGraph》，明天下午三点发布。",
            "刚刚那篇先取消发布，草稿保留。",
        ],
    },
    {
        "name": "case6-append-after-done",
        "turns": [
            "帮我发布一篇《Java 集合详解》，明天十点发布。",
            "再给它补一段 HashMap 扩容机制。",
            "对了，再帮我写一篇 JVM GC 的，后天十点发。",
            "第一篇再加一个面试题总结。",
        ],
    },
]


def main() -> None:
    token = login()
    for case in CASES:
        name = case["name"]
        log(f"========== {name} ==========")
        conv = new_conversation(token, name)
        for i, content in enumerate(case["turns"], start=1):
            log(f"[{name}] turn {i}: {content[:60]}...")
            result = send_and_wait(token, conv, content, name, i)
            if "error" in result:
                log(f"[{name}] turn {i} ERROR: {result['error']}")
                break
            summarize_drafts(token, name, i)
            # Small pause between turns so DB projections settle.
            time.sleep(1.0)
        log(f"========== {name} DONE ==========")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - keep the harness alive
        log(f"FATAL: {exc!r}")
        sys.exit(1)
