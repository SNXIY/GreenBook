"""Run Phase15-F evaluation against live GreenBook services.

This command never substitutes fake Java/Creator responses. Without a real
user JWT (or Java login credentials) it reports BLOCKED_BY_ENV and exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.runtime_eval import evaluate_cases, load_jsonl, merge_runtime_metrics  # noqa: E402


PROMPTS = [
    "帮我分析最近一个月 GreenBook 中 Java 学习相关的热门帖子，总结新人最常见的问题。结合这些内容生成一篇《Java 后端实习准备指南》，内容包括八股、项目经验、学习路线，安排下周一上午 9 点发布。另外单独整理一篇 Redis 缓存高频面试问题，只保存草稿，不发布。",
    "Java 那篇改成下午 2 点发布，标题改成《Java 后端实习准备指南：从八股到项目实战》，Redis 那篇不动。",
    "Redis 那篇增加布隆过滤器和互斥锁内容，发布时间保持不变。",
    "刚才改过标题的那篇，再提前半小时。",
    "把这两篇的标题、草稿状态和发布时间告诉我，不要修改。",
]


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def url(name: str, default: str) -> str:
    value = env(name, default)
    return value.rstrip("/")


def http_json(
    method: str,
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, Any]:
    request = urllib.request.Request(
        endpoint,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"body": raw}
        return exc.code, payload


def probe(name: str, endpoint: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        status, body = http_json("GET", endpoint, timeout=timeout)
        return {"name": name, "ready": 200 <= status < 300, "status_code": status, "body": body}
    except (OSError, ValueError) as exc:
        return {"name": name, "ready": False, "error": str(exc)}


def probe_tcp(name: str, host: str, port: int) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=3):
            return {"name": name, "ready": True, "host": host, "port": port}
    except OSError as exc:
        return {"name": name, "ready": False, "error": str(exc)}


def live_health() -> list[dict[str, Any]]:
    java = url("JAVA_BASE_URL", "http://127.0.0.1:8080")
    creator = url("CREATOR_BASE_URL", "http://127.0.0.1:8092")
    assistant = url(
        "ASSISTANT_BASE_URL",
        f"http://127.0.0.1:{env('ASSISTANT_API_PORT', '8094')}",
    )
    checks = [
        probe("Java", f"{java}/actuator/health"),
        probe("Creator", f"{creator}/actuator/health/ready"),
        probe("Assistant API", f"{assistant}/health"),
        probe_tcp("PostgreSQL", "127.0.0.1", int(env("POSTGRES_PORT", "25432"))),
        probe_tcp("MySQL", env("MYSQL_HOST", "127.0.0.1"), int(env("MYSQL_PORT", "3306"))),
    ]
    health_file = Path(env("ASSISTANT_WORKER_HEALTH_FILE", ".runtime/assistant-worker-health.json"))
    if not health_file.is_absolute():
        health_file = ROOT / health_file
    try:
        worker = json.loads(health_file.read_text(encoding="utf-8"))
        checks.append({"name": "Assistant Worker", "ready": worker.get("status") == "READY", "body": worker})
    except (OSError, json.JSONDecodeError) as exc:
        checks.append({"name": "Assistant Worker", "ready": False, "error": str(exc)})
    return checks


def login() -> str | None:
    token = env("GREENBOOK_E2E_ACCESS_TOKEN")
    if token:
        return token
    identifier = env("GREENBOOK_E2E_IDENTIFIER")
    password = env("GREENBOOK_E2E_PASSWORD")
    if not identifier or not password:
        return None
    status, payload = http_json(
        "POST",
        f"{url('JAVA_BASE_URL', 'http://127.0.0.1:8080')}/api/v1/auth/login",
        body={
            "identifierType": env("GREENBOOK_E2E_IDENTIFIER_TYPE", "EMAIL"),
            "identifier": identifier,
            "password": password,
            "code": None,
        },
        timeout=15,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Java login failed with HTTP {status}")
    return str(payload["token"]["accessToken"])


def wait_run(base_url: str, token: str, run_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        status, payload = http_json(
            "GET", f"{base_url}/api/v1/assistant/runs/{run_id}", headers=headers, timeout=15,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"run polling failed with HTTP {status}")
        if str(payload.get("status", "")).upper() in {
            "COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "INTERRUPTED",
        }:
            return payload
        time.sleep(2)
    raise TimeoutError(f"run {run_id} did not finish within {timeout}s")


def observation_from_run(run: dict[str, Any]) -> dict[str, Any]:
    partial = run.get("partial_results") or {}
    nodes = partial.get("nodes") or []
    return {
        "execution_status": run.get("status"),
        "tasks": partial.get("task_ids") or [],
        "goals": nodes,
        "dependencies": [node.get("depends_on", []) for node in nodes],
        "agents": [node.get("agent_name") for node in nodes if node.get("agent_name")],
        "artifacts": [artifact for node in nodes for artifact in node.get("output_artifacts", [])],
        "side_effects": [
            artifact.get("artifact_type")
            for node in nodes
            for artifact in node.get("output_artifacts", [])
            if artifact.get("artifact_type") in {"SCHEDULE", "PUBLISHED_POST", "CONTENT_DRAFT"}
        ],
        "raw_run": run,
    }


def run_live(base_url: str, token: str, timeout: int) -> dict[str, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    status, payload = http_json(
        "POST",
        f"{base_url}/api/v1/assistant/conversations",
        headers=headers,
        body={"title": "Phase15-F Final Multi-Agent E2E", "surface": "HOME"},
        timeout=15,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"conversation creation failed with HTTP {status}")
    conversation_id = str(payload["conversation_id"])
    observations: dict[str, dict[str, Any]] = {}
    for index, prompt in enumerate(PROMPTS, 1):
        status, accepted = http_json(
            "POST",
            f"{base_url}/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": f"phase15f-{conversation_id}-{index}"},
            body={"content": prompt, "client_timezone": "Asia/Shanghai"},
            timeout=15,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"message submission failed with HTTP {status}")
        run_id = str(accepted["run_id"])
        run = wait_run(base_url, token, run_id, timeout)
        observations[f"round-{index}"] = observation_from_run(run)
    return observations


def print_report(report: dict[str, Any], health: list[dict[str, Any]]) -> None:
    print("GreenBook Agent Evaluation")
    print(f"Status: {report.get('status', 'UNKNOWN')}")
    if report.get("blocked_reason"):
        print(f"Blocked: {report['blocked_reason']}")
    metric_names = [
        "task_decomposition_accuracy",
        "target_resolution_accuracy",
        "planner_accuracy",
        "tool_success_rate",
        "runtime_success_rate",
        "recovery_rate",
        "artifact_resolution_accuracy",
    ]
    metrics = report.get("metrics", {})
    for name in metric_names:
        value = (
            "BLOCKED_BY_ENV"
            if report.get("status") == "BLOCKED_BY_ENV"
            else metrics.get(name, 0.0)
        )
        print(f"{name}: {value}")
    print("Health:")
    for item in health:
        print(f"  {item['name']}: {'READY' if item.get('ready') else 'UNAVAILABLE'}")
    print(f"Badcases: {len(report.get('badcases', []))}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", default=str(ROOT / "evaluation" / "datasets"))
    parser.add_argument("--observations", help="JSON object keyed by case_id; no live calls")
    parser.add_argument("--output", default=str(ROOT / ".runtime" / "phase15f-evaluation.json"))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()
    load_dotenv()

    health = live_health()
    blocked: str | None = None
    observations: dict[str, dict[str, Any]] = {}
    if args.observations:
        observations = json.loads(Path(args.observations).read_text(encoding="utf-8"))
    elif not args.no_live:
        if not all(item.get("ready") for item in health):
            blocked = "BLOCKED_BY_ENV: live service health is incomplete"
        else:
            try:
                token = login()
                if not token:
                    blocked = "BLOCKED_BY_ENV: GREENBOOK_E2E_ACCESS_TOKEN or login credentials are missing"
                else:
                    observations = run_live(
                        url("ASSISTANT_BASE_URL", f"http://127.0.0.1:{env('ASSISTANT_API_PORT', '8094')}"),
                        token,
                        args.timeout,
                    )
            except Exception as exc:
                blocked = f"BLOCKED_BY_ENV: live E2E failed before complete observation: {exc}"

    cases = [
        case
        for file in sorted(Path(args.dataset_dir).glob("*.jsonl"))
        for case in load_jsonl(file)
    ]
    report = evaluate_cases(cases, observations, blocked_reason=blocked)
    if observations:
        report["runtime_metrics"] = merge_runtime_metrics(list(observations.values()))
    report["health"] = health
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(report, health)
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
