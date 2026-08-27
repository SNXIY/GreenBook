"""Measure a small real Agent E2E performance baseline.

This is an evaluation-only harness. It talks to the already configured local
Java/MCP/Agent services through their public boundaries and reads the existing
Run performance projection and debug trace. It does not patch production code,
change runtime configuration, or add a second execution path.

The benchmark deliberately uses a small scenario set. Write scenarios create
real drafts/schedules in the configured E2E account; they are named by the
scenario and are not deleted by this harness because deletion would be an
additional measured business operation and a destructive side effect.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "docs" / "evaluation" / "agent_performance_baseline_results.json"
TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "INTERRUPTED"}
WAITING = {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    label: str
    prompt: str
    repeats: int = 2


SCENARIOS = (
    Scenario("simple_read", "A. Simple READ", "搜索最近的 Agent 帖子"),
    Scenario(
        "simple_write",
        "B. Simple WRITE",
        "写一篇关于 Java 集合框架的学习草稿，只保存为草稿，不发布。",
    ),
    Scenario(
        "sequential_dependent",
        "C. Sequential dependent task",
        "写一篇关于 Java 后端可靠性的帖子，并安排在明天 23:30 发布。",
    ),
    Scenario(
        "independent_multi_objective",
        "D. Independent multi-objective",
        "请完成两个互不依赖的内容任务，并分别保存为两个草稿，不能合并：第一篇草稿标题为《Java 集合框架》，介绍 Java 集合框架；第二篇草稿标题为《Agent 可观测性》，介绍 Agent 可观测性。两篇都不要发布。",
    ),
    Scenario(
        "search_creation",
        "E. Search + Creation",
        "搜索最近的 Java 相关帖子，然后参考搜索结果写一篇关于 Java 性能的草稿，只保存草稿，不发布。",
    ),
    Scenario(
        "rag_grounded_query",
        "F. RAG grounded query",
        "根据 GreenBook 社区里现有关于 Agent Memory 的帖子，总结大家主要讨论了哪些问题，并给出对应的社区证据。",
    ),
    Scenario(
        "rag_java_concurrency",
        "G. RAG Java concurrency query",
        "基于社区里关于 Java 并发的现有内容，总结大家常讨论的几个问题，并说明依据来自哪些帖子。",
    ),
    Scenario(
        "rag_evaluation_material",
        "H. RAG evaluation material query",
        "社区里有没有关于 RAG 评测的资料？如果有，请基于这些资料总结，不要使用社区资料之外的信息。",
    ),
)


RAG_SCENARIO_IDS = {
    "rag_grounded_query",
    "rag_java_concurrency",
    "rag_evaluation_material",
}


EXPECTED_PATHS = {
    "simple_read": (
        "POST /messages -> TurnCoordinator -> Interpreter -> Complex/ActionLoop "
        "-> MCP read -> Java -> terminal Run"
    ),
    "simple_write": (
        "POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop "
        "-> Durable Runtime -> MCP write -> Java -> verified draft"
    ),
    "sequential_dependent": (
        "POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop "
        "-> Durable Runtime draft -> continuation -> Durable Runtime schedule"
    ),
    "independent_multi_objective": (
        "POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop "
        "-> two independent Objectives -> Durable Runtime -> MCP/Java writes"
    ),
    "search_creation": (
        "POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop "
        "-> MCP search -> Observation -> ActionLoop -> Durable Runtime write"
    ),
    "rag_grounded_query": (
        "POST /messages -> TurnCoordinator -> Interpreter -> "
        "ANSWER_FROM_KNOWLEDGE -> MCP -> Java Hybrid/RAG -> generation/citation"
    ),
    "rag_java_concurrency": (
        "POST /messages -> TurnCoordinator -> Interpreter -> "
        "ANSWER_FROM_KNOWLEDGE -> MCP -> Java Hybrid/RAG -> generation/citation"
    ),
    "rag_evaluation_material": (
        "POST /messages -> TurnCoordinator -> Interpreter -> "
        "ANSWER_FROM_KNOWLEDGE -> MCP -> Java Hybrid/RAG -> generation/citation or fail-closed no-answer"
    ),
}


def _safe_text(value: Any, limit: int = 240) -> str:
    return str(value or "")[:limit]


def _safe_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in run.get("steps") or []:
        if not isinstance(item, dict):
            continue
        result.append({
            key: _safe_text(item.get(key))
            for key in (
                "step_id", "goal_id", "objective_id", "capability", "tool_name",
                "status", "error_code", "error",
            )
            if item.get(key) not in (None, "")
        })
    return result


def _safe_decisions(partial: dict[str, Any]) -> list[dict[str, Any]]:
    raw = partial.get("decisions")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        decision = item.get("decision") if isinstance(item.get("decision"), dict) else item
        if not isinstance(decision, dict):
            continue
        value: dict[str, Any] = {"ordinal": index}
        for key in (
            "iteration", "action", "tool_name", "semantic_action", "capability",
            "decision_source", "llm_called", "status",
        ):
            if key in decision and decision[key] not in (None, ""):
                value[key] = decision[key]
        if decision.get("reason") not in (None, ""):
            value["reason"] = _safe_text(decision.get("reason"))
        result.append(value)
    return result


def _safe_tool_calls(partial: dict[str, Any]) -> list[dict[str, Any]]:
    raw = partial.get("tool_calls")
    # RuntimeResult stores the count in ``tool_calls`` and the detailed
    # observations in ``tool_results``/``observations``.  Prefer the detailed
    # existing projection when it is available; otherwise a terminal
    # continuation can make a real MCP call look nameless to the benchmark.
    if not isinstance(raw, list):
        raw = partial.get("tool_results")
    if not isinstance(raw, list):
        raw = partial.get("observations")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        value: dict[str, Any] = {"ordinal": index}
        for key in (
            "tool_name", "capability", "semantic_action", "action", "status",
            "outcome", "ok", "code", "error_code", "latency_ms", "request_sent",
        ):
            if key in item and item[key] not in (None, ""):
                value[key] = item[key]
        result.append(value)
    return result


def _objective_summary(
    run: dict[str, Any],
    partial: dict[str, Any],
    steps: list[dict[str, Any]],
    task_index: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    objective_ids = sorted({
        str(item.get(key))
        for item in steps
        for key in ("objective_id", "goal_id")
        if item.get(key)
    })
    task_ids = [str(value) for value in (partial.get("task_ids") or []) if value]
    task_index = task_index or []
    for task in task_index:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "")
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        for goal in [*(task.get("goals") or []), *(task.get("execution_refs") or [])]:
            if not isinstance(goal, dict):
                continue
            objective_id = str(
                goal.get("objective_id") or goal.get("goal_id") or goal.get("id") or ""
            )
            if objective_id and objective_id not in objective_ids:
                objective_ids.append(objective_id)
    raw_dependencies = partial.get("dependencies")
    dependencies = raw_dependencies if isinstance(raw_dependencies, (list, dict)) else None
    raw_bindings = partial.get("resource_bindings")
    resource_bindings = raw_bindings if isinstance(raw_bindings, (list, dict)) else None
    execution_order = [
        item.get("step_id")
        for item in steps
        if item.get("step_id")
    ]
    if not execution_order and run.get("execution_id"):
        execution_order = [str(run["execution_id"])]
    return {
        "objective_count": len(objective_ids) if objective_ids else None,
        "objective_ids": objective_ids,
        "task_ids": task_ids,
        "dependencies": dependencies,
        "resource_bindings": resource_bindings,
        "execution_order": execution_order,
        "step_count": len(steps),
        "observation_source": (
            "conversation_task_index"
            if task_index and objective_ids
            else "run_steps"
            if objective_ids
            else "partial_results.task_ids"
            if task_ids
            else "not_exposed_by_run_projection"
        ),
    }


def _actual_path(
    scenario: Scenario,
    run: dict[str, Any],
    trace: dict[str, Any],
    *,
    tool_calls: int | float | None,
    java_calls: int | float | None,
) -> str:
    stages = [str(value) for value in trace.get("stages") or []]
    route = next((value for value in stages if value.startswith("route_")), "")
    parts = ["POST /messages", "TurnCoordinator", "Interpreter"]
    if route == "route_chat":
        parts.append("CHAT")
    elif route:
        parts.append(route.removeprefix("route_").upper())
    elif run.get("execution_path"):
        parts.append(str(run.get("execution_path")))
    if "actionloop_started" in stages:
        parts.append("ActionLoop")
    if "operation_created" in stages or run.get("execution_id"):
        parts.append("Durable Runtime")
    if "continuation_start" in stages or stages.count("actionloop_started") > 1:
        parts.append("Continuation")
    if tool_calls is not None and tool_calls > 0:
        parts.append("MCP")
    if java_calls is not None and java_calls > 0:
        parts.append("Java")
    return " -> ".join(dict.fromkeys(parts))


def _path_audit(
    scenario: Scenario,
    run: dict[str, Any],
    trace: dict[str, Any],
    *,
    tool_calls: int | float | None,
    java_calls: int | float | None,
    tool_names: list[str],
    task_index: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stages = {str(value) for value in trace.get("stages") or []}
    error_code = str(run.get("error_code") or "").upper()
    expected_tool = "community.answer_from_knowledge"
    actual_path = _actual_path(
        scenario,
        run,
        trace,
        tool_calls=tool_calls,
        java_calls=java_calls,
    )
    first_divergence = None
    path_valid = True
    if scenario.scenario_id in RAG_SCENARIO_IDS:
        path_valid = expected_tool in tool_names
        if not path_valid:
            first_divergence = "ANSWER_FROM_KNOWLEDGE_ADMISSION"
    elif scenario.scenario_id == "independent_multi_objective":
        objective_count = _objective_summary(
            run, {}, _safe_steps(run), task_index=task_index
        ).get("objective_count")
        if objective_count != 2:
            path_valid = False
            first_divergence = "SEMANTIC_BENCHMARK_INVALID"
        elif not ("operation_created" in stages or run.get("execution_id")):
            path_valid = False
            first_divergence = "DURABLE_SUBMIT"
        elif error_code == "PERMISSION_DENIED" and (java_calls or 0) == 0:
            path_valid = False
            first_divergence = "MCP_RUNTIME_TRUST"
        elif not tool_calls:
            path_valid = False
            first_divergence = "MCP_CALL"
    elif scenario.scenario_id == "sequential_dependent":
        semantic_actions = {
            str(item.get("semantic_action") or "").upper()
            for item in (trace.get("spans") or [])
            if isinstance(item, dict)
        }
        has_two_actionloops = sum(
            1 for item in (trace.get("spans") or [])
            if isinstance(item, dict) and item.get("stage") == "actionloop_started"
        ) >= 2
        path_valid = (
            has_two_actionloops
            and "GENERATE_CONTENT" in semantic_actions
            and "SCHEDULE_PUBLISH" in semantic_actions
            and bool(tool_calls and tool_calls > 0)
            and bool(java_calls and java_calls > 0)
        )
        if not has_two_actionloops or "SCHEDULE_PUBLISH" not in semantic_actions:
            first_divergence = "DEPENDENCY_CONTINUATION"
        elif not tool_calls:
            first_divergence = "MCP_CALL"
        elif not java_calls:
            first_divergence = "JAVA_CALL"
    elif scenario.scenario_id == "search_creation":
        has_search = "community.search_public_posts" in tool_names
        has_durable_write = "content.create_draft" in tool_names
        path_valid = has_search and has_durable_write
        if not has_search:
            first_divergence = "SEARCH_MCP_CALL"
        elif not has_durable_write:
            first_divergence = "DURABLE_CREATE_SUBMIT"
    elif scenario.scenario_id == "simple_read":
        path_valid = "actionloop_started" in stages and bool(tool_calls and tool_calls > 0)
        if "actionloop_started" not in stages:
            first_divergence = "ACTION_LOOP_ADMISSION"
        elif not path_valid:
            first_divergence = "MCP_READ_CALL"
    else:
        path_valid = "operation_created" in stages or bool(run.get("execution_id"))
        if not path_valid:
            first_divergence = "DURABLE_SUBMIT"
        elif error_code == "PERMISSION_DENIED" and (java_calls or 0) == 0:
            path_valid = False
            first_divergence = "MCP_RUNTIME_TRUST"
        elif not tool_calls:
            path_valid = False
            first_divergence = "MCP_CALL"
    return {
        "expected_path": EXPECTED_PATHS.get(scenario.scenario_id, ""),
        "actual_path": actual_path,
        "path_valid": path_valid,
        "first_divergence": first_divergence,
        "observed_route_stages": sorted(stages),
        "observed_tool_names": tool_names,
        "execution_path": run.get("execution_path"),
        "workload_lane": run.get("workload_lane"),
    }


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _conversation_task_index(
    client: httpx.Client,
    api: str,
    token: str,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """Read the public conversation Task index for objective cardinality.

    Run projection intentionally keeps the terminal envelope compact and may
    omit GoalTree details.  The public task index is the existing read model
    for semantic objective/execution ownership; using it here avoids calling a
    one-objective benchmark invalid when the durable Task has two Goal refs.
    """
    try:
        response = client.get(
            f"{api}/api/v1/agent/conversations/{conversation_id}/tasks",
            headers=_auth(token),
        )
        if response.status_code != 200:
            return []
        payload = _json_response(response)
        items = payload.get("items")
        return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    except httpx.HTTPError:
        return []


def _env(dotenv: dict[str, str], name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None and value.strip():
        return value.strip()
    return str(dotenv.get(name, default) or default).strip()


def _base_url(value: str, default: str) -> str:
    return (value or default).rstrip("/")


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        trust_env=False,
    )


def _json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _login(client: httpx.Client, java: str, dotenv: dict[str, str]) -> str:
    token = _env(dotenv, "GREENBOOK_E2E_ACCESS_TOKEN")
    if token:
        return token
    identifier = _env(dotenv, "GREENBOOK_E2E_IDENTIFIER")
    password = _env(dotenv, "GREENBOOK_E2E_PASSWORD")
    if not identifier or not password:
        raise RuntimeError(
            "GREENBOOK_E2E_ACCESS_TOKEN or GREENBOOK_E2E_IDENTIFIER/PASSWORD is required"
        )
    response = client.post(
        f"{java}/api/v1/auth/login",
        json={
            "identifierType": _env(dotenv, "GREENBOOK_E2E_IDENTIFIER_TYPE", "EMAIL"),
            "identifier": identifier,
            "password": password,
            "code": None,
        },
    )
    response.raise_for_status()
    payload = _json_response(response)
    access_token = str(((payload.get("token") or {}).get("accessToken") or "")).strip()
    if not access_token:
        raise RuntimeError("Java login did not return an access token")
    return access_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_conversation(client: httpx.Client, api: str, token: str, title: str) -> str:
    response = client.post(
        f"{api}/api/v1/agent/conversations",
        headers=_auth(token),
        json={"title": title, "surface": "HOME"},
    )
    response.raise_for_status()
    conversation_id = str(_json_response(response).get("conversation_id") or "").strip()
    if not conversation_id:
        raise RuntimeError("Agent did not return a conversation id")
    return conversation_id


def _semantic_confirmation(
    client: httpx.Client,
    api: str,
    token: str,
    conversation_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    response = client.get(
        f"{api}/api/v1/agent/conversations/{conversation_id}/activities",
        headers=_auth(token),
    )
    if response.status_code != 200:
        return None
    payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    for item in items or []:
        if (
            isinstance(item, dict)
            and str(item.get("run_id") or "") == run_id
            and str(item.get("activity_type") or "") == "NEEDS_SEMANTIC_CONFIRMATION"
        ):
            return item
    return None


def _confirm_semantic_task(
    client: httpx.Client,
    api: str,
    token: str,
    activity: dict[str, Any],
) -> bool:
    safe = dict(activity.get("safe_payload") or {})
    task_id = str(activity.get("task_id") or "")
    if not task_id or not safe.get("confirmation_id"):
        return False
    response = client.post(
        f"{api}/api/v1/agent/tasks/{task_id}/semantic-confirmation",
        headers=_auth(token),
        json={
            "action": "CONFIRM",
            "confirmation_id": safe.get("confirmation_id"),
            "expected_task_version": safe.get("task_version"),
            "expected_confirmation_version": safe.get("confirmation_version"),
        },
    )
    return response.status_code in {200, 201, 202}


def _first_stream_event(
    api: str,
    token: str,
    run_id: str,
    accepted_at: float,
    result: dict[str, Any],
) -> None:
    """Capture first run activity as the available TTFT proxy.

    The current API accepts a run immediately and does not expose provider
    token-level streaming. The first SSE activity is therefore reported as
    ``first_activity_ms``/TTFT proxy, never as a token-level claim.
    """
    try:
        with _client(timeout=8.0) as client:
            with client.stream(
                "GET",
                f"{api}/api/v1/agent/runs/{run_id}/stream",
                headers=_auth(token),
            ) as response:
                if response.status_code != 200:
                    result["stream_status"] = response.status_code
                    return
                for line in response.iter_lines():
                    if line.startswith("event:") or line.startswith("data:"):
                        result["first_activity_ms"] = round(
                            (time.perf_counter() - accepted_at) * 1000, 3
                        )
                        result["first_activity_line"] = line[:80]
                        return
    except Exception as exc:  # diagnostics must not abort terminal polling
        result["stream_error"] = type(exc).__name__


def _poll_run(
    client: httpx.Client,
    api: str,
    token: str,
    conversation_id: str,
    run_id: str,
    *,
    accepted_at: float,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first_observation: dict[str, Any] = {}
    confirmed: set[str] = set()
    deadline = time.perf_counter() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        try:
            response = client.get(
                f"{api}/api/v1/agent/runs/{run_id}",
                headers=_auth(token),
            )
            if response.status_code == 200:
                latest = _json_response(response)
        except httpx.HTTPError:
            latest = latest or {}
        status = str(latest.get("status") or "").upper()
        if status and status != "ACCEPTED" and "first_state_ms" not in first_observation:
            first_observation["first_state_ms"] = round(
                (time.perf_counter() - accepted_at) * 1000, 3
            )
            first_observation["first_state"] = status
        if status in WAITING:
            activity = _semantic_confirmation(client, api, token, conversation_id, run_id)
            activity_id = str((activity or {}).get("activity_id") or "")
            if activity and activity_id and activity_id not in confirmed:
                if _confirm_semantic_task(client, api, token, activity):
                    confirmed.add(activity_id)
                    first_observation["semantic_confirmations"] = len(confirmed)
                    time.sleep(0.1)
                    continue
            # Waiting states are durable checkpoints, not terminal states.  A
            # measurement/evaluation run must return the checkpoint so the
            # caller can record it and continue with independent cases; it
            # must not poll forever for a human response that this harness is
            # not authorized to invent.
            if status in {"WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"}:
                break
        if status in TERMINAL:
            # One short read-after-terminal refresh lets the durable
            # performance projection settle before it is captured.
            time.sleep(0.15)
            try:
                refreshed = client.get(
                    f"{api}/api/v1/agent/runs/{run_id}",
                    headers=_auth(token),
                )
                if refreshed.status_code == 200:
                    latest = _json_response(refreshed)
            except httpx.HTTPError:
                pass
            break
        time.sleep(0.2)
    if not latest:
        latest = {"status": "POLL_ERROR"}
    if str(latest.get("status") or "").upper() not in TERMINAL | WAITING:
        latest["status"] = "TIMEOUT"
    latest["observed_e2e_ms"] = round((time.perf_counter() - accepted_at) * 1000, 3)
    return latest, first_observation


def _find_numbers(value: Any, wanted: set[str], found: dict[str, list[float]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in wanted and isinstance(item, (int, float)) and not isinstance(item, bool):
                found.setdefault(normalized, []).append(float(item))
            _find_numbers(item, wanted, found)
    elif isinstance(value, list):
        for item in value:
            _find_numbers(item, wanted, found)


def _first_number(found: dict[str, list[float]], *names: str) -> float | None:
    for name in names:
        values = found.get(name.lower()) or []
        if values:
            return values[0]
    return None


def _metric_value(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _trace_summary(client: httpx.Client, api: str, token: str, trace_id: str) -> dict[str, Any]:
    if not trace_id:
        return {"available": False, "span_count": 0, "stages": [], "spans": []}
    try:
        response = client.get(
            f"{api}/debug/traces/{trace_id}",
            headers=_auth(token),
        )
        if response.status_code != 200:
            return {
                "available": False,
                "status_code": response.status_code,
                "span_count": 0,
                "stages": [],
                "spans": [],
            }
        payload = _json_response(response)
        spans = payload.get("spans") if isinstance(payload.get("spans"), list) else []
        stages = [str(item.get("stage") or "") for item in spans if isinstance(item, dict)]
        safe_spans = []
        for item in spans:
            if not isinstance(item, dict):
                continue
            safe_spans.append({
                key: item[key]
                for key in (
                    "seq", "stage", "task_id", "objective_id", "operation_id",
                    "execution_id", "tool_call_id", "semantic_action", "status",
                    "latency_ms", "error_code", "at",
                )
                if item.get(key) not in (None, "")
            })
        return {
            "available": True,
            "span_count": len(spans),
            "stages": stages,
            "spans": safe_spans,
            "parallel_signal": any("parallel" in stage.lower() for stage in stages),
        }
    except httpx.HTTPError as exc:
        return {
            "available": False,
            "error": type(exc).__name__,
            "span_count": 0,
            "stages": [],
            "spans": [],
        }


def _execution_timeline(
    client: httpx.Client,
    api: str,
    token: str,
    execution_ids: list[str],
) -> dict[str, Any]:
    """Read the existing durable Runtime timeline without adding instrumentation."""

    safe_items: list[dict[str, Any]] = []
    available_ids: list[str] = []
    for execution_id in dict.fromkeys(str(value) for value in execution_ids if value):
        try:
            response = client.get(
                f"{api}/api/v1/executions/{execution_id}/timeline",
                headers=_auth(token),
            )
            if response.status_code != 200:
                continue
            payload = _json_response(response)
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
            available_ids.append(execution_id)
            for item in items:
                if not isinstance(item, dict):
                    continue
                safe: dict[str, Any] = {
                    key: item[key]
                    for key in (
                        "item_id", "kind", "source", "timestamp", "event_id",
                        "event_type", "step_id", "invocation_id", "tool_call_id",
                        "tool_name", "operation_id", "external_operation_id",
                        "receipt_id", "operation_status",
                    )
                    if item.get(key) not in (None, "")
                }
                item_payload = item.get("payload")
                if isinstance(item_payload, dict):
                    observation = item_payload.get("observation")
                    if isinstance(observation, dict):
                        safe["observation"] = {
                            key: observation[key]
                            for key in (
                                "result_status", "resource_count", "failure_kind",
                                "missing_required_reference",
                            )
                            if observation.get(key) not in (None, "")
                        }
                    payload_tool = item_payload.get("tool_name")
                    if payload_tool and "tool_name" not in safe:
                        safe["tool_name"] = _safe_text(payload_tool)
                safe["execution_id"] = execution_id
                safe_items.append(safe)
        except httpx.HTTPError:
            continue
    tool_names = sorted({
        str(item.get("tool_name"))
        for item in safe_items
        if item.get("tool_name")
    })
    return {
        "available": bool(available_ids),
        "execution_ids": available_ids,
        "item_count": len(safe_items),
        "tool_names": tool_names,
        "items": safe_items,
    }


def _extract_record(
    scenario: Scenario,
    repetition: int,
    prompt: str,
    accepted_ms: float,
    first_activity: dict[str, Any],
    run: dict[str, Any],
    trace: dict[str, Any],
    runtime_timeline: dict[str, Any],
    task_index: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    performance = run.get("performance") if isinstance(run.get("performance"), dict) else {}
    partial = run.get("partial_results") if isinstance(run.get("partial_results"), dict) else {}
    stages = performance.get("stage_durations_ms") if isinstance(performance.get("stage_durations_ms"), dict) else {}
    found: dict[str, list[float]] = {}
    _find_numbers(
        partial,
        {
            "embedding_latency_ms",
            "chunk_retrieval_latency_ms",
            "generation_latency_ms",
            "search_latency_ms",
            "rag_latency_ms",
            "total_rag_latency_ms",
        },
        found,
    )
    raw_java_calls = _metric_value(performance.get("java_calls"))
    # The current projection uses zero as its default when Java call
    # instrumentation did not record a boundary. Do not report that default
    # as an observed call count.
    java_calls = raw_java_calls if raw_java_calls is not None and raw_java_calls > 0 else None
    raw_java_ms = _metric_value(performance.get("java_latency_ms"))
    # ``0`` is the current projection default when Java instrumentation has
    # not recorded a call. Do not report that default as a real zero.
    java_ms = raw_java_ms if java_calls is not None and java_calls > 0 else None
    raw_tool_ms = _metric_value(performance.get("tool_latency_ms"))
    tool_calls = _metric_value(performance.get("tool_calls"))
    # A zero tool duration is a real no-tool observation for chat, but it is
    # not a measured boundary latency. Keep the count and suppress the
    # misleading zero duration.
    tool_ms = raw_tool_ms if tool_calls is not None and tool_calls > 0 else None
    generation_ms = _first_number(found, "generation_latency_ms")
    rag_internal_ms = _first_number(found, "rag_latency_ms", "total_rag_latency_ms")
    rag_latency_method = "instrumentation_unavailable"
    rag_path_observed = True
    rag_path_observation = "not_applicable"
    if scenario.scenario_id in RAG_SCENARIO_IDS:
        rag_path_observed = bool(tool_calls is not None and tool_calls > 0)
        rag_path_observation = (
            "canonical_tool_boundary_observed"
            if rag_path_observed
            else "canonical_rag_tool_not_observed"
        )
        if not rag_path_observed:
            rag_latency_method = "not_observed_route_or_chat"
    if (
        rag_internal_ms is None
        and scenario.scenario_id in RAG_SCENARIO_IDS
        and tool_calls is not None
        and tool_calls > 0
        and tool_ms is not None
    ):
        # The RAG scenario is intentionally a one-capability query. The
        # existing tool boundary is inclusive of MCP -> Java evidence
        # retrieval -> generation, so preserve it as an explicitly labelled
        # operation proxy rather than inventing an internal Java duration.
        rag_internal_ms = float(tool_ms)
        rag_latency_method = "single_rag_tool_boundary_inclusive"

    extracted = {
        "embedding_latency_ms": _first_number(found, "embedding_latency_ms"),
        "chunk_retrieval_latency_ms": _first_number(found, "chunk_retrieval_latency_ms"),
        "generation_component_latency_ms": generation_ms,
        "rag_latency_ms": rag_internal_ms,
        "search_latency_ms": _first_number(found, "search_latency_ms"),
    }
    if (
        scenario.scenario_id == "simple_read"
        and extracted["search_latency_ms"] is None
        and tool_ms is not None
        and tool_calls == 1
    ):
        extracted["search_latency_ms"] = tool_ms
        extracted["search_latency_method"] = "single_search_tool_boundary_inclusive"
    else:
        extracted["search_latency_method"] = "instrumentation_unavailable"
    extracted["rag_latency_method"] = rag_latency_method

    e2e_ms = _metric_value(run.get("observed_e2e_ms"))
    server_total_ms = _metric_value(performance.get("total_latency_ms"))
    output_tokens = _metric_value(performance.get("output_tokens"))
    input_tokens = _metric_value(performance.get("input_tokens"))
    steps = _safe_steps(run)
    decisions = _safe_decisions(partial)
    partial_tool_calls = _safe_tool_calls(partial)
    tool_names = sorted({
        str(item.get("tool_name"))
        for item in [*steps, *partial_tool_calls]
        if item.get("tool_name")
    } | {
        str(value)
        for value in (runtime_timeline.get("tool_names") or [])
        if value
    } | {
        str(item.get("semantic_action"))
        for item in (trace.get("spans") or [])
        if isinstance(item, dict)
        and item.get("stage") == "mcp_call"
        and item.get("semantic_action")
    })
    path_audit = _path_audit(
        scenario,
        run,
        trace,
        tool_calls=tool_calls,
        java_calls=java_calls,
        tool_names=tool_names,
        task_index=task_index,
    )
    stage_timestamps = {
        key: value
        for key, value in (performance.get("stage_timestamps") or {}).items()
        if key in {
            "api_received", "runner_started", "worker_claimed", "worker_started",
            "context_start", "context_ready", "semantic_start", "semantic_resolved",
            "route_decided", "actionloop_first_llm_start", "actionloop_first_llm_end",
            "execution_submit_start", "execution_submitted", "java_start", "java_end",
            "projection_persisted", "continuation_start", "continuation_finished",
            "final_response_start", "final_response_finished", "run_terminal_start",
            "run_terminal",
        }
    }
    llm_events = [
        {
            key: item[key]
            for key in ("category", "latency_ms", "input_tokens", "output_tokens")
            if key in item
        }
        for item in (performance.get("llm_events") or [])
        if isinstance(item, dict)
    ]
    execution_ids = [str(value) for value in (run.get("execution_ids") or []) if value]
    if run.get("execution_id") and str(run["execution_id"]) not in execution_ids:
        execution_ids.insert(0, str(run["execution_id"]))
    task_ids = [str(value) for value in (run.get("task_ids") or []) if value]
    if partial.get("task_ids"):
        task_ids = list(dict.fromkeys([*task_ids, *(str(value) for value in partial["task_ids"] if value)]))
    for task in task_index or []:
        task_id = str(task.get("task_id") or "") if isinstance(task, dict) else ""
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_label": scenario.label,
        "repetition": repetition,
        "prompt": prompt,
        "run_id": str(run.get("run_id") or ""),
        "conversation_id": str(run.get("conversation_id") or ""),
        "status": str(run.get("status") or ""),
        "error_code": run.get("error_code"),
        "error": _safe_text(run.get("error")),
        "execution_id": str(run.get("execution_id") or ""),
        "execution_path": run.get("execution_path"),
        "workload_lane": run.get("workload_lane"),
        "accepted_ms": accepted_ms,
        "tta_ms": first_activity.get("first_activity_ms") or first_activity.get("first_state_ms"),
        "tta_source": "first_run_sse_activity" if first_activity.get("first_activity_ms") else "first_non_accepted_state",
        # The current provider/API does not expose a token timestamp.  Keep
        # the token-level metric explicitly unavailable instead of relabeling
        # first SSE activity as TTFT.
        "ttft_ms": None,
        "ttft_source": "unavailable_provider_token_timestamp",
        # Backward-compatible aliases for consumers of the V1 artifact.
        "ttft_proxy_ms": first_activity.get("first_activity_ms") or first_activity.get("first_state_ms"),
        "ttft_proxy_source": "first_run_sse_activity" if first_activity.get("first_activity_ms") else "first_non_accepted_state",
        "observed_e2e_ms": e2e_ms,
        "server_total_latency_ms": server_total_ms,
        "llm_calls": _metric_value(performance.get("llm_calls")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "semantic_llm_calls": _metric_value(performance.get("semantic_llm_calls")),
        "creator_llm_calls": _metric_value(performance.get("creator_llm_calls")),
        "actionloop_iterations": _metric_value(performance.get("actionloop_iterations")),
        "queue_wait_ms": _metric_value(stages.get("queue_wait_ms")),
        "interpreter_semantic_ms": _metric_value(stages.get("semantic_ms")),
        "route_ms": _metric_value(stages.get("route_ms")),
        "memory_total_ms": _metric_value(performance.get("memory_total_ms")),
        "search_latency_ms": extracted["search_latency_ms"],
        "rag_latency_ms": extracted["rag_latency_ms"],
        "rag_path_observed": rag_path_observed,
        "rag_path_observation": rag_path_observation,
        "embedding_latency_ms": extracted["embedding_latency_ms"],
        "chunk_retrieval_latency_ms": extracted["chunk_retrieval_latency_ms"],
        "rag_generation_latency_ms": extracted["generation_component_latency_ms"],
        "mcp_tool_latency_ms": tool_ms,
        "tool_calls": tool_calls,
        "java_calls": java_calls,
        "java_latency_ms": java_ms,
        "java_latency_observation": (
            "recorded" if java_ms is not None else "not_instrumented_or_default_zero"
        ),
        "search_latency_method": extracted["search_latency_method"],
        "rag_latency_method": extracted["rag_latency_method"],
        "final_response_latency_ms": (
            _metric_value(stages.get("final_response_ms"))
            if stages.get("final_response_ms") is not None
            else None
        ),
        "runner_latency_ms": _metric_value(performance.get("runner_latency_ms")),
        "stage_durations_ms": dict(stages),
        "execution_ids": execution_ids,
        "task_ids": task_ids,
        "partial_result_keys": sorted(partial.keys()),
        "steps": steps,
        "decisions": decisions,
        "tool_call_details": partial_tool_calls,
        "execution_timeline": runtime_timeline,
        "llm_events": llm_events,
        "stage_timestamps": stage_timestamps,
        "objective_summary": _objective_summary(
            run, partial, steps, task_index=task_index
        ),
        "path_audit": path_audit,
        "trace": trace,
        "trace_parallel_signal": bool(trace.get("parallel_signal")),
        "semantic_confirmations": int(first_activity.get("semantic_confirmations") or 0),
        "performance_available": bool(performance),
        "performance_missing_fields": [
            name
            for name, value in {
                "llm_calls": performance.get("llm_calls"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "actionloop_iterations": performance.get("actionloop_iterations"),
                "queue_wait_ms": stages.get("queue_wait_ms"),
                "interpreter_semantic_ms": stages.get("semantic_ms"),
                "memory_total_ms": performance.get("memory_total_ms"),
                "mcp_tool_latency_ms": tool_ms,
                "java_latency_ms": java_ms,
            }.items()
            if value is None
        ],
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 3)


def _stats(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [float(item[field]) for item in records if isinstance(item.get(field), (int, float))]
    if not values:
        return {"n": 0, "p50": None, "p95": None, "sample_limitation": "no_observed_values"}
    limitation = None if len(values) >= 5 else f"n={len(values)}; p95 is descriptive only"
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 3),
        "p95": _percentile(values, 0.95),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "sample_limitation": limitation,
    }


_SUCCESS_STATUSES = frozenset({"COMPLETED", "PARTIAL_SUCCESS"})


def _is_successful(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "").upper() in _SUCCESS_STATUSES


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "accepted_ms",
        "tta_ms",
        "observed_e2e_ms",
        "server_total_latency_ms",
        "llm_calls",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "java_calls",
        "actionloop_iterations",
        "queue_wait_ms",
        "interpreter_semantic_ms",
        "memory_total_ms",
        "search_latency_ms",
        "rag_latency_ms",
        "mcp_tool_latency_ms",
        "java_latency_ms",
        "final_response_latency_ms",
    )
    return {field: _stats(records, field) for field in fields}


def _scenario_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_scenario.setdefault(str(record["scenario_id"]), []).append(record)
    return {
        scenario_id: {
            "label": items[0]["scenario_label"],
            "sample_count": len(items),
            "success_count": sum(1 for item in items if _is_successful(item)),
            "failure_count": sum(1 for item in items if not _is_successful(item)),
            "valid_path_count": sum(
                1 for item in items if (item.get("path_audit") or {}).get("path_valid")
            ),
            "statuses": {status: sum(1 for item in items if item.get("status") == status) for status in sorted({str(item.get("status") or "") for item in items})},
            "metrics": _aggregate([item for item in items if _is_successful(item)]),
            "non_success_metrics": _aggregate([item for item in items if not _is_successful(item)]),
            "all_observed_metrics": _aggregate(items),
            "execution_count_p50": _stats(
                [{"value": len(item.get("execution_ids") or [])} for item in items],
                "value",
            ),
            "parallel_signal_count": sum(1 for item in items if item.get("trace_parallel_signal")),
        }
        for scenario_id, items in by_scenario.items()
    }


def _validation_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    invalid = [
        item for item in records
        if not (item.get("path_audit") or {}).get("path_valid")
    ]
    divergences: dict[str, int] = {}
    for item in invalid:
        name = str((item.get("path_audit") or {}).get("first_divergence") or "UNKNOWN")
        divergences[name] = divergences.get(name, 0) + 1
    return {
        "sample_count": len(records),
        "path_valid_count": len(records) - len(invalid),
        "path_invalid_count": len(invalid),
        "first_divergence_distribution": divergences,
        "records": [
            {
                "scenario_id": item.get("scenario_id"),
                "repetition": item.get("repetition"),
                "status": item.get("status"),
                "path_audit": item.get("path_audit"),
                "objective_summary": item.get("objective_summary"),
            }
            for item in records
        ],
    }


def _health(client: httpx.Client, api: str, java: str, mcp: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, url in (
        ("agent_api", f"{api}/health"),
        ("java", f"{java}/actuator/health"),
        ("mcp", f"{mcp}/health"),
    ):
        try:
            response = client.get(url)
            result[name] = {"status_code": response.status_code, "ready": 200 <= response.status_code < 300}
        except httpx.HTTPError as exc:
            result[name] = {"status_code": None, "ready": False, "error": type(exc).__name__}
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    dotenv = _load_dotenv(args.env_file)
    api = _base_url(_env(dotenv, "GREENBOOK_AGENT_API_URL"), f"http://127.0.0.1:{_env(dotenv, 'GREENBOOK_AGENT_API_PORT', '8094')}")
    java = _base_url(_env(dotenv, "GREENBOOK_JAVA_BASE_URL"), "http://127.0.0.1:8080")
    mcp = _base_url(_env(dotenv, "GREENBOOK_BUSINESS_MCP_BASE_URL"), f"http://127.0.0.1:{_env(dotenv, 'GREENBOOK_MCP_PORT', '8095')}")
    if mcp.endswith("/mcp"):
        mcp_health = mcp[:-4]
    else:
        mcp_health = mcp
    selected = [item for item in SCENARIOS if not args.scenario or item.scenario_id in args.scenario]
    if not selected:
        raise RuntimeError(f"unknown scenario; choose from {', '.join(item.scenario_id for item in SCENARIOS)}")

    records: list[dict[str, Any]] = []
    started_at = time.perf_counter()
    with _client(timeout=max(30.0, args.request_timeout_seconds)) as client:
        health = _health(client, api, java, mcp_health)
        if not health.get("agent_api", {}).get("ready") or not health.get("mcp", {}).get("ready"):
            raise RuntimeError(f"required services are not ready: {health}")
        token = _login(client, java, dotenv)
        for scenario in selected:
            repeats = args.repeats if args.repeats is not None else scenario.repeats
            for repetition in range(1, repeats + 1):
                conversation_id = _new_conversation(
                    client,
                    api,
                    token,
                    f"perf-{scenario.scenario_id}-{repetition}-{uuid.uuid4().hex[:8]}",
                )
                prompt = scenario.prompt
                headers = _auth(token) | {
                    "Idempotency-Key": uuid.uuid4().hex,
                    "Content-Type": "application/json; charset=utf-8",
                }
                request_started = time.perf_counter()
                response = client.post(
                    f"{api}/api/v1/agent/conversations/{conversation_id}/messages",
                    headers=headers,
                    json={"content": prompt, "client_timezone": "Asia/Shanghai"},
                )
                accepted_ms = round((time.perf_counter() - request_started) * 1000, 3)
                response.raise_for_status()
                accepted = _json_response(response)
                run_id = str(accepted.get("run_id") or "")
                if not run_id:
                    raise RuntimeError("Agent did not return a run id")
                accepted_at = time.perf_counter()
                first_activity: dict[str, Any] = {}
                stream_thread = threading.Thread(
                    target=_first_stream_event,
                    args=(api, token, run_id, accepted_at, first_activity),
                    daemon=True,
                )
                stream_thread.start()
                final_run, first_state = _poll_run(
                    client,
                    api,
                    token,
                    conversation_id,
                    run_id,
                    accepted_at=accepted_at,
                    timeout_seconds=args.run_timeout_seconds,
                )
                for key, value in first_state.items():
                    first_activity.setdefault(key, value)
                stream_thread.join(timeout=1.0)
                trace_id = str(final_run.get("trace_id") or "")
                trace = _trace_summary(client, api, token, trace_id)
                execution_ids_for_timeline = [
                    str(value)
                    for value in (final_run.get("execution_ids") or [])
                    if value
                ]
                if final_run.get("execution_id"):
                    execution_ids_for_timeline.insert(0, str(final_run["execution_id"]))
                runtime_timeline = _execution_timeline(
                    client,
                    api,
                    token,
                    execution_ids_for_timeline,
                )
                task_index = _conversation_task_index(
                    client, api, token, conversation_id
                )
                record = _extract_record(
                    scenario,
                    repetition,
                    prompt,
                    accepted_ms,
                    first_activity,
                    final_run,
                    trace,
                    runtime_timeline,
                    task_index,
                )
                records.append(record)
                print(
                    f"[{len(records)}] {scenario.scenario_id}#{repetition} "
                    f"status={record['status']} e2e_ms={record['observed_e2e_ms']} "
                    f"llm={record['llm_calls']} java_ms={record['java_latency_ms']}"
                )

    by_status: dict[str, int] = {}
    for record in records:
        by_status[str(record.get("status") or "")] = by_status.get(str(record.get("status") or ""), 0) + 1
    successful_records = [record for record in records if _is_successful(record)]
    non_success_records = [record for record in records if not _is_successful(record)]
    return {
        "schema_version": "agent_performance_baseline_v2",
        "mode": "real_local_e2e_observation",
        "purpose": args.purpose,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "configuration": {
            "agent_api": api,
            "java": java,
            "mcp_health": mcp_health,
            "dispatch": _env(dotenv, "GREENBOOK_AGENT_EXECUTION_DISPATCH"),
            "storage": _env(dotenv, "GREENBOOK_AGENT_RUNTIME_STORAGE"),
            "process_role": _env(dotenv, "GREENBOOK_AGENT_PROCESS_ROLE"),
            "in_process_worker": _env(dotenv, "GREENBOOK_AGENT_IN_PROCESS_WORKER"),
            "request_timeout_seconds": args.request_timeout_seconds,
            "run_timeout_seconds": args.run_timeout_seconds,
        },
        "health": health,
        "scenario_ids": [item.scenario_id for item in selected],
        "requested_repeats": args.repeats,
        "sample_count": len(records),
        "status_distribution": by_status,
        "wall_clock_seconds": round(time.perf_counter() - started_at, 3),
        "tta_definition": "time from accepted POST response to first Run SSE activity or first non-accepted Run state",
        "ttft_definition": "UNAVAILABLE: provider/model token timestamps are not exposed by the current API",
        "latency_definition": "observed_e2e_ms is measured from accepted POST completion to terminal Run read; server_total_latency_ms is the existing Run projection",
        "records": records,
        "validation": _validation_summary(records),
        "by_scenario": _scenario_summary(records),
        "overall": _aggregate(successful_records),
        "non_success": {
            "count": len(non_success_records),
            "status_distribution": {
                status: sum(1 for item in non_success_records if item.get("status") == status)
                for status in sorted({str(item.get("status") or "") for item in non_success_records})
            },
            "metrics": _aggregate(non_success_records),
        },
        "all_observed": _aggregate(records),
        "limitations": [
            "This is a small scenario sample, not a capacity or load benchmark.",
            "p95 is descriptive only for groups with fewer than five observations.",
            "Current API exposes first Run activity rather than provider token-level TTFT.",
            "Search latency is unavailable except for the single-Java-call simple-read proxy.",
            "RAG latency is derived from the inclusive Java evidence call plus generation component when the tool state exposes it.",
            "Write scenarios intentionally leave their created drafts/schedules in the dedicated E2E account.",
        ],
        "production_files_changed": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--scenario", action="append", choices=[item.scenario_id for item in SCENARIOS])
    parser.add_argument("--repeats", type=int, default=2, help="repetitions per selected scenario")
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=420.0)
    parser.add_argument(
        "--purpose",
        choices=["validation", "measurement"],
        default="measurement",
        help="Whether this run validates canonical paths or collects measurement samples.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.request_timeout_seconds <= 0 or args.run_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    print(f"Samples: {result['sample_count']} | statuses: {result['status_distribution']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
