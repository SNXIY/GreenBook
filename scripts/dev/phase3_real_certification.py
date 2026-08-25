"""Real Phase 3 certification against the running Java/Agent/Postgres stack."""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

API = os.getenv("GREENBOOK_REAL_AGENT_API", "http://127.0.0.1:8097")
JAVA = os.getenv("GREENBOOK_REAL_JAVA_API", "http://127.0.0.1:8080")
PHONE = "13592298973"
PASSWORD = "FanZK061345%"


def login() -> str:
    response = httpx.post(
        f"{JAVA}/api/v1/auth/login",
        json={
            "identifierType": "PHONE",
            "identifier": PHONE,
            "password": PASSWORD,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["token"]["accessToken"]


def post_message(
    conversation_id: str,
    content: str,
    headers: dict[str, str],
) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    response = httpx.post(
        f"{API}/api/v1/agent/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return time.perf_counter() - started, response.json()


def stream_run(
    run_id: str,
    headers: dict[str, str],
    wall_zero: float,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    with httpx.stream(
        "GET",
        f"{API}/api/v1/agent/runs/{run_id}/stream",
        headers=headers,
        timeout=180,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:].strip())
            event_type = payload.get("event_type") or payload.get("status") or "?"
            events.append(
                {
                    "at": round(time.perf_counter() - wall_zero, 3),
                    "event_type": event_type,
                    "created_at": payload.get("created_at"),
                    "payload": payload.get("payload") or {},
                }
            )
            if event_type in {"COMPLETED", "FAILED", "CANCELLED", "WAITING_USER"}:
                break
    return {
        "run_id": run_id,
        "completed_at": round(time.perf_counter() - wall_zero, 3),
        "events": events,
    }


def main() -> None:
    token = login()
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = str(uuid.uuid4())
    created = httpx.post(
        f"{API}/api/v1/agent/conversations",
        json={"conversation_id": conversation_id},
        headers=headers,
        timeout=30,
    )
    created.raise_for_status()
    conversation_id = created.json().get("conversation_id", conversation_id)

    prompts = {
        "A": "找几篇最近关于 MCP 的帖子并总结共同观点",
        "B": "看看我最近发布的一篇 Redis 帖子表现怎么样",
        "C": "把我之前那篇 Java 草稿标题改得更简洁一点",
    }
    wall_zero = time.perf_counter()
    accepted: dict[str, dict[str, Any]] = {}
    for label, prompt in prompts.items():
        elapsed, response = post_message(conversation_id, prompt, headers)
        accepted[label] = response
        print(
            json.dumps(
                {
                    "label": label,
                    "accepted_after_seconds": round(elapsed, 3),
                    "run_id": response.get("run_id"),
                    "status": response.get("status"),
                    "conversation_id": conversation_id,
                    "task_ids": response.get("task_ids", []),
                    "execution_ids": response.get("execution_ids", []),
                },
                ensure_ascii=False,
            )
        )
        time.sleep(0.5)

    statuses = {}
    for label, response in accepted.items():
        run_id = response.get("run_id")
        current = httpx.get(f"{API}/api/v1/agent/runs/{run_id}", headers=headers, timeout=30)
        statuses[label] = current.json() if current.status_code == 200 else {"status_code": current.status_code}
    print(json.dumps({"post_accept_statuses": statuses}, ensure_ascii=False))

    traces: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(stream_run, response["run_id"], headers, wall_zero): label
            for label, response in accepted.items()
        }
        for future in as_completed(futures):
            trace = future.result()
            trace["label"] = futures[future]
            traces.append(trace)
    print(json.dumps({"run_traces": sorted(traces, key=lambda item: item["label"])}, ensure_ascii=False))

    tasks = httpx.get(
        f"{API}/api/v1/agent/conversations/{conversation_id}/tasks",
        headers=headers,
        timeout=30,
    )
    print(
        json.dumps(
            {
                "task_index_status": tasks.status_code,
                "task_index": tasks.json() if tasks.status_code == 200 else tasks.text[:500],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
