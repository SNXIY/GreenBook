"""Run the overnight semantic multi-objective corpus through the public Agent API.

This is an evaluation harness only.  It starts every case at the public
conversation message boundary, reads the existing Task index/debug trace, and
never mutates storage directly.  Write cases intentionally use the configured
E2E account and leave their business results for later truth verification.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from measure_agent_performance_baseline import (
    ROOT,
    _auth,
    _client,
    _conversation_task_index,
    _env,
    _first_stream_event,
    _json_response,
    _load_dotenv,
    _login,
    _poll_run,
    _trace_summary,
)


TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "INTERRUPTED"}


def _post_message(
    client: httpx.Client,
    *,
    api: str,
    token: str,
    conversation_id: str,
    prompt: str,
    run_timeout_seconds: float,
) -> dict[str, Any]:
    headers = _auth(token) | {
        "Idempotency-Key": uuid.uuid4().hex,
        "Content-Type": "application/json; charset=utf-8",
    }
    started = time.perf_counter()
    response = client.post(
        f"{api}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": prompt, "client_timezone": "Asia/Shanghai"},
    )
    accepted_ms = round((time.perf_counter() - started) * 1000, 3)
    response.raise_for_status()
    accepted = _json_response(response)
    run_id = str(accepted.get("run_id") or "")
    if not run_id:
        raise RuntimeError("Agent did not return a run id")
    accepted_at = time.perf_counter()
    first_activity: dict[str, Any] = {}
    # The helper reports first visible Run activity when the stream boundary is
    # available.  It is deliberately labelled as TTA, never provider TTFT.
    first_state = {}
    final_run, first_state = _poll_run(
        client,
        api,
        token,
        conversation_id,
        run_id,
        accepted_at=accepted_at,
        timeout_seconds=run_timeout_seconds,
    )
    for key, value in first_state.items():
        first_activity.setdefault(key, value)
    trace_id = str(final_run.get("trace_id") or "")
    trace = _trace_summary(client, api, token, trace_id)
    task_index = _conversation_task_index(client, api, token, conversation_id)
    return {
        "prompt": prompt,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": str(final_run.get("status") or ""),
        "error_code": final_run.get("error_code"),
        "error": str(final_run.get("error") or "")[:500],
        "accepted_ms": accepted_ms,
        "e2e_ms": final_run.get("observed_e2e_ms"),
        "tta_ms": first_activity.get("first_activity_ms") or first_activity.get("first_state_ms"),
        "trace_id": trace_id,
        "trace": trace,
        "task_index": task_index,
        "final_run": final_run,
    }


def _debug_records(path: Path, run_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("run_id") or "") == run_id:
            records.append(record)
    return records


def _semantic_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for record in records:
        stage = str(record.get("stage") or "")
        if stage:
            stages[stage] = record.get("payload")
    raw = stages.get("raw") if isinstance(stages.get("raw"), dict) else {}
    normalized = stages.get("normalized") if isinstance(stages.get("normalized"), dict) else {}
    resolved = stages.get("resolved_semantic_state") if isinstance(stages.get("resolved_semantic_state"), dict) else {}
    attach = stages.get("objective_attach") if isinstance(stages.get("objective_attach"), dict) else {}
    route = stages.get("turn_route") if isinstance(stages.get("turn_route"), dict) else {}
    attached_objectives = (
        attach.get("objectives")
        if isinstance(attach.get("objectives"), list)
        else []
    )
    # Mutation objectives are materialized from explicit resource changes and
    # therefore do not always emit the normal objective_attach debug record.
    # The resolved semantic state is the authoritative fallback for evaluation
    # in that shape; keep the source explicit so a missing debug event is not
    # mistaken for semantic decomposition failure.
    resolved_objectives = (
        resolved.get("objectives")
        if isinstance(resolved.get("objectives"), list)
        else []
    )
    objectives = attached_objectives or resolved_objectives
    objective_source = (
        "objective_attach"
        if attached_objectives
        else ("resolved_semantic_state" if resolved_objectives else "none")
    )
    items = resolved.get("items") if isinstance(resolved.get("items"), list) else []
    if not items and isinstance(normalized.get("items"), list):
        items = normalized.get("items")
    return {
        "stages_present": sorted(stages),
        "raw": raw,
        "normalized": normalized,
        "resolved_semantic_state": resolved,
        "turn_route": route,
        "objective_attach": attach,
        "objective_source": objective_source,
        "objective_count": len(objectives),
        "objective_ids": [
            str(item.get("objective_id") or "")
            for item in objectives
            if isinstance(item, dict) and item.get("objective_id")
        ],
        "items": items,
    }


def _task_artifacts(task_index: list[dict[str, Any]]) -> dict[str, Any]:
    task_ids: list[str] = []
    execution_refs: list[dict[str, Any]] = []
    for task in task_index:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "")
        if task_id:
            task_ids.append(task_id)
        for ref in task.get("execution_refs") or []:
            if isinstance(ref, dict):
                execution_refs.append({
                    key: ref.get(key)
                    for key in ("execution_id", "task_id", "goal_id", "status")
                    if ref.get(key) not in (None, "")
                })
    return {
        "task_ids": list(dict.fromkeys(task_ids)),
        "execution_refs": execution_refs,
        "produced_artifact_observation": (
            "execution_refs are available; business resource ids require the Java truth read"
            if execution_refs else "not_exposed_by_task_index"
        ),
    }


def _business_snapshot(
    client: httpx.Client,
    *,
    java: str,
    token: str,
    keywords: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, path in (("drafts", "/api/v1/agent/me/drafts"), ("posts", "/api/v1/agent/me/posts")):
        try:
            response = client.get(
                java + path,
                headers=_auth(token),
                params={"page": 1, "size": 100},
            )
            if response.status_code != 200:
                result[name] = {"status_code": response.status_code}
                continue
            values = response.json()
            values = values if isinstance(values, list) else []
            matched = []
            for item in values:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get(key) or "") for key in ("title", "content", "summary"))
                if any(keyword and keyword in text for keyword in keywords):
                    matched.append({
                        key: item.get(key)
                        for key in ("draftId", "postId", "title", "status", "version", "createdAt", "updatedAt", "publishedAt")
                        if item.get(key) not in (None, "")
                    })
            result[name] = {"status_code": response.status_code, "matched": matched[:20]}
        except (httpx.HTTPError, ValueError):
            result[name] = {"status_code": None, "error": "read_failed"}
    return result


def _first_bad_state(
    case: dict[str, Any],
    *,
    semantic: dict[str, Any],
    run_result: dict[str, Any],
) -> str | None:
    expected = case.get("expected_objective_count")
    actual = semantic.get("objective_count")
    if isinstance(expected, int) and actual != expected:
        return "OBJECTIVE_DECOMPOSITION"
    status = str(run_result.get("status") or "").upper()
    if status in {"FAILED", "INTERRUPTED", "TIMEOUT"}:
        return "DURABLE_EXECUTION"
    if status == "WAITING_APPROVAL":
        return None
    return None


def _run_case(
    client: httpx.Client,
    *,
    case: dict[str, Any],
    api: str,
    java: str,
    token: str,
    debug_file: Path,
    run_timeout_seconds: float,
) -> dict[str, Any]:
    case_id = str(case["id"])
    conversation = _new_conversation_for_case(client, api, token, case_id)
    keywords = [str(case_id)]
    prompt_result = _post_message(
        client,
        api=api,
        token=token,
        conversation_id=conversation,
        prompt=str(case["prompt"]),
        run_timeout_seconds=run_timeout_seconds,
    )
    prompt_result["semantic"] = _semantic_snapshot(
        _debug_records(debug_file, prompt_result["run_id"])
    )
    prompt_result["artifacts"] = _task_artifacts(prompt_result["task_index"])
    if case_id == "MO-11":
        keywords.extend(["跨轮 Java", "跨轮 Agent"])
    if case_id == "MO-12":
        keywords.append("会话 A")
    prompt_result["business_snapshot"] = _business_snapshot(
        client, java=java, token=token, keywords=keywords
    )
    prompt_result["first_bad_state"] = _first_bad_state(
        case, semantic=prompt_result["semantic"], run_result=prompt_result
    )
    prompt_result["conversation_id"] = conversation
    prompt_result["follow_up"] = None

    follow_up_prompt = case.get("follow_up_prompt")
    if follow_up_prompt:
        follow_result = _post_message(
            client,
            api=api,
            token=token,
            conversation_id=conversation,
            prompt=str(follow_up_prompt),
            run_timeout_seconds=run_timeout_seconds,
        )
        follow_result["semantic"] = _semantic_snapshot(
            _debug_records(debug_file, follow_result["run_id"])
        )
        follow_result["artifacts"] = _task_artifacts(follow_result["task_index"])
        follow_result["business_snapshot"] = _business_snapshot(
            client,
            java=java,
            token=token,
            keywords=keywords,
        )
        expected_follow_up = case.get("expected_follow_up_objective_count")
        follow_bad = None
        if isinstance(expected_follow_up, int) and follow_result["semantic"].get("objective_count") != expected_follow_up:
            follow_bad = "OBJECTIVE_DECOMPOSITION"
        elif str(follow_result.get("status") or "").upper() in {"FAILED", "INTERRUPTED", "TIMEOUT"}:
            follow_bad = "DURABLE_EXECUTION"
        follow_result["first_bad_state"] = follow_bad
        prompt_result["follow_up"] = follow_result

    isolation_prompt = case.get("isolation_follow_up_prompt")
    if isolation_prompt:
        isolated_conversation = _new_conversation_for_case(client, api, token, case_id + "-isolated")
        isolated_result = _post_message(
            client,
            api=api,
            token=token,
            conversation_id=isolated_conversation,
            prompt=str(isolation_prompt),
            run_timeout_seconds=run_timeout_seconds,
        )
        isolated_result["semantic"] = _semantic_snapshot(
            _debug_records(debug_file, isolated_result["run_id"])
        )
        isolated_result["artifacts"] = _task_artifacts(isolated_result["task_index"])
        isolated_result["business_snapshot"] = _business_snapshot(
            client,
            java=java,
            token=token,
            keywords=["会话 A 专属", "不应自动绑定"],
        )
        isolated_result["conversation_id"] = isolated_conversation
        expected_follow_up = case.get("expected_follow_up_objective_count")
        isolated_bad = None
        if isinstance(expected_follow_up, int) and isolated_result["semantic"].get("objective_count") != expected_follow_up:
            isolated_bad = "OBJECTIVE_DECOMPOSITION"
        elif str(isolated_result.get("status") or "").upper() in {"FAILED", "INTERRUPTED", "TIMEOUT"}:
            isolated_bad = "DURABLE_EXECUTION"
        isolated_result["first_bad_state"] = isolated_bad
        prompt_result["follow_up"] = isolated_result
    return prompt_result


def _new_conversation_for_case(client: httpx.Client, api: str, token: str, case_id: str) -> str:
    response = client.post(
        f"{api}/api/v1/agent/conversations",
        headers=_auth(token),
        json={"title": f"overnight-{case_id}-{uuid.uuid4().hex[:8]}", "surface": "HOME"},
    )
    response.raise_for_status()
    conversation_id = str(_json_response(response).get("conversation_id") or "")
    if not conversation_id:
        raise RuntimeError(f"Agent did not return a conversation id for {case_id}")
    return conversation_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "docs" / "worklogs" / "overnight-20260826-27" / "multi_objective_cases.json",
    )
    parser.add_argument(
        "--debug-file",
        type=Path,
        default=ROOT / ".runtime" / "overnight-rag-r2-r3-interpreter.jsonl",
    )
    parser.add_argument("--run-timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based case index to resume from without rerunning earlier cases",
    )
    parser.add_argument(
        "--end-index",
        type=int,
        default=0,
        help="optional inclusive 1-based case index; zero means the end",
    )
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime" / "overnight-multi-objective-matrix.json")
    args = parser.parse_args()
    dotenv = _load_dotenv(args.env_file)
    api = _env(dotenv, "GREENBOOK_AGENT_API_URL", "http://127.0.0.1:8094").rstrip("/")
    java = _env(dotenv, "GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise RuntimeError("multi-objective cases must be a JSON array")
    start_index = max(1, args.start_index)
    end_index = args.end_index if args.end_index > 0 else len(cases)
    if start_index > len(cases) or end_index < start_index:
        raise RuntimeError(f"invalid case range: {start_index}-{end_index} for {len(cases)} cases")
    selected_cases = cases[start_index - 1:end_index]
    with _client(timeout=90.0) as client:
        token = _login(client, java, dotenv)
        results: list[dict[str, Any]] = []
        for index, case in enumerate(selected_cases, start=start_index):
            result = _run_case(
                client,
                case=case,
                api=api,
                java=java,
                token=token,
                debug_file=args.debug_file,
                run_timeout_seconds=args.run_timeout_seconds,
            )
            result["case_id"] = case.get("id")
            result["category"] = case.get("category")
            result["expected"] = {
                key: case.get(key)
                for key in (
                    "expected_objective_count", "expected_follow_up_objective_count",
                    "expected_actions", "expected_follow_up_actions",
                    "expected_dependencies", "required_artifacts", "target_resource_ids",
                    "temporal_facts", "publication_intent", "expected_follow_up_state",
                )
                if key in case
            }
            results.append(result)
            print(
                f"[{index}/{len(cases)}] {case.get('id')} "
                f"status={result.get('status')} "
                f"objectives={result.get('semantic', {}).get('objective_count')} "
                f"first_bad={result.get('first_bad_state')}",
                flush=True,
            )
    output = {
        "schema_version": "overnight_multi_objective_matrix_v1",
        "mode": "real_local_public_agent_api",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases_file": str(args.cases),
        "debug_file": str(args.debug_file),
        "case_range": {"start_index": start_index, "end_index": end_index},
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
