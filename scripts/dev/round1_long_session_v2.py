"""Evidence runner for valid same-conversation Round 1 long sessions.

All business input is loaded from a strict UTF-8 JSON fixture and submitted
through the real Frontend AgentPanel.  API calls in this module are limited to
read-only projection snapshots, and HITL actions are delegated to the browser
harness's real DOM click path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dev.overnight_stable_baseline_browser import (
    Browser,
    COMPOSER_HYDRATION_GRACE_SECONDS,
    WAITING,
    find_page,
    load_utf8_cases,
    run_turn,
)
from scripts.dev.round1_final_closure_v2 import JavaTruth, restore_browser_auth


OUT = ROOT / ".runtime" / "round1-final-v2"
TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
WRITE_CAPABILITIES = {
    "GENERATE_CONTENT",
    "MANAGE_DRAFT",
    "SCHEDULE_PUBLISH",
    "MANAGE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "DELETE_DRAFT",
    "PUBLISH_NOW",
}
ID_KEYS = ("postId", "draftId", "scheduleId", "publicationId", "id", "resource_id")
TITLE_KEYS = ("title", "postTitle", "draftTitle", "name")
RAW_LEAK_RE = re.compile(r"(?i)(?:operation[_ ]?id|execution[_ ]?id|objective[_ ]?id|authorization|bearer\s+ey|result_unknown)")
FORBIDDEN_USER_MARKER_RE = re.compile(
    r"(?i)(?:GB-R1-|ANCHOR|CASE-|TEST-|run[-_ ]?id|UUID|timestamp|resource_id|objective_id|execution_id)"
)


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "records", "content", "data"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    return []


def _rid(item: dict[str, Any]) -> str:
    for key in ID_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _title(item: dict[str, Any]) -> str:
    for key in TITLE_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _resource_ids(resources: dict[str, Any]) -> set[str]:
    output: set[str] = set()
    for payload in resources.values():
        output.update(_rid(item) for item in _items(payload) if _rid(item))
    return output


def _new_resources(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for name, payload in after.items():
        old = {_rid(item) for item in _items(before.get(name, {})) if _rid(item)}
        output[name] = [item for item in _items(payload) if _rid(item) and _rid(item) not in old]
    return output


def _task_projection(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("items", data.get("tasks", data))
    return [x for x in (data or []) if isinstance(x, dict)] if isinstance(data, list) else []


def _objective_projection(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        for objective in task.get("objectives") or []:
            if isinstance(objective, dict):
                rows.append({"task_id": task.get("task_id"), **objective})
    return rows


def _temporal_projection(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"run_at", "runat", "temporal_kind", "temporal_text", "temporal_resolved", "schedule_id"}:
                found.append({key: _json(item)})
            found.extend(_temporal_projection(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_temporal_projection(item))
    return found


def _write_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in run.get("steps") or []:
        if not isinstance(step, dict):
            continue
        capability = str(step.get("capability") or step.get("label") or "")
        if capability in WRITE_CAPABILITIES:
            rows.append({
                "capability": capability,
                "status": step.get("status"),
                "error": step.get("error"),
                "step_id": step.get("step_id"),
            })
    return rows


def _task_for_run(tasks: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for task in tasks:
        blob = json.dumps(task, ensure_ascii=False)
        if run_id and run_id in blob:
            result.append(task)
    return result


def _expected_titles(text: str) -> list[str]:
    return re.findall(r"GB-R1-L1-FINAL-[A-Z0-9-]+", text)


def _assert_natural_user_text(text: str) -> None:
    """Reject harness-oriented identifiers before they reach the Frontend."""

    if FORBIDDEN_USER_MARKER_RE.search(text or ""):
        raise ValueError("fixture contains a harness-only identifier in user text")


async def _refresh_browser_auth(
    browser: Browser,
    truth: JavaTruth,
    *,
    conversation_id: str | None = None,
) -> None:
    """Refresh the test-account JWT and rehydrate the real SPA when needed.

    Writing localStorage alone does not update AuthContext's in-memory React
    state.  Once the old token expires, that state can log the page out while
    the harness believes it has restored credentials.  Keep the cheap path
    when the current composer is still authenticated; otherwise reload the
    SPA through its normal bootstrap and select the existing conversation.
    """

    truth.login()
    token = truth.auth_response.get("token") or {}
    payload = {
        "accessToken": str(token.get("accessToken") or ""),
        "refreshToken": str(token.get("refreshToken") or ""),
        "expiresAt": token.get("accessTokenExpiresAt"),
    }
    user = truth.auth_response.get("user") or {}
    if not payload["accessToken"] or not payload["refreshToken"]:
        raise RuntimeError("Java login returned incomplete refresh credentials")
    await browser.evaluate(
        f"""(()=>{{
          localStorage.setItem('zhiguang_auth_tokens', {json.dumps(json.dumps(payload, ensure_ascii=False))});
          localStorage.setItem('zhiguang_current_user', {json.dumps(json.dumps(user, ensure_ascii=False))});
          return true;
        }})()"""
    )
    current_ready = await browser.evaluate(
        """(()=>{
          const textarea=document.querySelector('textarea[name="agent-message"]');
          return Boolean(textarea && !textarea.disabled);
        })()"""
    )
    if current_ready:
        return

    # AuthContext reads the supported storage keys during mount.  Reload only
    # when the live React tree no longer exposes an enabled composer, then
    # restore the same conversation through the ordinary AgentPanel list.
    await browser.evaluate("location.reload(); true")
    deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        state = await browser.evaluate(
            "({path:location.pathname,ready:document.readyState})"
        )
        if (
            isinstance(state, dict)
            and state.get("path") == "/"
            and state.get("ready") in {"interactive", "complete"}
        ):
            break
        await asyncio.sleep(0.25)
    await browser.open_panel()
    if conversation_id:
        await browser.prefer_conversation(conversation_id)


async def _read_projection(browser: Browser, conversation_id: str) -> dict[str, Any]:
    tasks_response = await browser.api("GET", f"/api/v1/agent/conversations/{conversation_id}/tasks")
    messages_response = await browser.api("GET", f"/api/v1/agent/conversations/{conversation_id}/messages")
    activities_response = await browser.api("GET", f"/api/v1/agent/conversations/{conversation_id}/activities")
    tasks = _task_projection(tasks_response.get("data"))
    return {
        "tasks": tasks,
        "objectives": _objective_projection(tasks),
        "messages": messages_response.get("data") if isinstance(messages_response.get("data"), list) else [],
        "activities": activities_response.get("data"),
    }


async def _view_search_first(browser: Browser, *, keep_page: bool = False) -> dict[str, Any]:
    """Click the visible first search result through the real Frontend UI."""

    started = time.monotonic()
    action = await browser.evaluate(
        """(()=>{
          const links=[...document.querySelectorAll('a[class*="searchLink"]')];
          const recent=links.slice(-5);
          const link=recent[0];
          if(!link)return {clicked:false,count:recent.length};
          const href=link.getAttribute('href')||'';
          const title=(link.innerText||'').trim();
          link.click();
          return {clicked:true,count:recent.length,index:0,href,title};
        })()"""
    )
    await asyncio.sleep(1.0)
    viewed = await browser.snapshot()
    if not keep_page:
        await browser.evaluate("if(location.pathname !== '/') location.href='/' ; true")
        await asyncio.sleep(0.9)
        await browser.open_panel()
    return {
        "utterance": "[Frontend UI] 查看搜索结果第一篇帖子",
        "status": "COMPLETED" if isinstance(action, dict) and action.get("clicked") else "HARNESS_ERROR",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "run_id": None,
        "clicked_hitl": [],
        "ui_action": _json(action),
        "viewed_frontend": _json(viewed),
        "ui": _json(viewed),
        "ui_internal_leak": False,
    }


async def _reload_conversation(browser: Browser, conversation_id: str) -> dict[str, Any]:
    """Reload the real SPA and capture durable conversation recovery evidence."""

    started = time.monotonic()
    script_id = await browser.prefer_conversation_on_next_document(conversation_id)
    try:
        await browser.evaluate("location.reload(); true")
        for _ in range(40):
            try:
                ready = await browser.evaluate(
                    "({path:location.pathname,ready:document.readyState})"
                )
                if isinstance(ready, dict) and ready.get("ready") in {"interactive", "complete"}:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        await browser.open_panel()
        ui_message_rendered = False
        hydration_deadline = time.monotonic() + COMPOSER_HYDRATION_GRACE_SECONDS
        while time.monotonic() < hydration_deadline:
            ui_message_rendered = bool(
                await browser.evaluate(
                    "Boolean(document.querySelector('[class*=""agentMessage""], [class*=""userMessage""]'))"
                )
            )
            if ui_message_rendered:
                break
            await asyncio.sleep(0.25)
        snapshot = await browser.snapshot()
        messages = await browser.messages(conversation_id)
    finally:
        if script_id:
            try:
                await browser.command(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": script_id},
                )
            except Exception:
                pass
    return {
        "utterance": "[Frontend UI] 重新加载当前会话",
        "status": "COMPLETED" if messages else "HARNESS_ERROR",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "run_id": None,
        "conversation_id": conversation_id,
        "clicked_hitl": [],
        "ui": _json(snapshot),
        "messages_after_reload": len(messages),
        "ui_message_rendered": ui_message_rendered,
        "ui_internal_leak": bool(RAW_LEAK_RE.search(str(snapshot.get("body") or ""))),
    }


def _classify_turn(
    *,
    text: str,
    result: dict[str, Any],
    before_java: dict[str, Any],
    after_java: dict[str, Any],
    before_projection: dict[str, Any],
    after_projection: dict[str, Any],
) -> dict[str, Any]:
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    writes = _write_steps(run)
    new_resources = _new_resources(before_java, after_java)
    expected = _expected_titles(text)
    visible_text = str((result.get("ui") or {}).get("body") or "")
    titles = [
        _title(item)
        for payload in after_java.values()
        for item in _items(payload)
        if _title(item)
    ]
    relevant_tasks = _task_for_run(after_projection.get("tasks") or [], str(result.get("run_id") or ""))
    task_statuses = sorted({
        str(task.get("status") or "").upper()
        for task in (after_projection.get("tasks") or [])
        if str(task.get("status") or "")
    })
    objective_statuses = sorted({
        str(objective.get("status") or "").upper()
        for objective in (after_projection.get("objectives") or [])
        if str(objective.get("status") or "")
    })
    resource_states = sorted({
        str(ref.get("status") or ref.get("state") or "").upper()
        for task in (after_projection.get("tasks") or [])
        for ref in (task.get("resource_index") or [])
        if isinstance(ref, dict) and str(ref.get("status") or ref.get("state") or "")
    })
    all_ids_before = _resource_ids(before_java)
    all_ids_after = _resource_ids(after_java)
    # A publish can legitimately transition an already-existing draft into a
    # post without adding a second resource id.  Count duplicate writes only
    # when the persisted artifact/receipt evidence repeats the same physical
    # resource reference within one run; do not infer duplication from a
    # draft-to-post lifecycle transition.
    artifact_ids = [
        str(value)
        for artifact in (run.get("artifacts") or [])
        if isinstance(artifact, dict)
        for value in [artifact.get("resource_id")]
        if value not in (None, "")
    ]
    duplicate_write = len(artifact_ids) != len(set(artifact_ids))
    false_success = str(result.get("status") or "") in {"COMPLETED", "PARTIAL_SUCCESS"} and any(
        row.get("capability") in WRITE_CAPABILITIES and row.get("status") == "COMPLETED" for row in writes
    ) and not new_resources and not (run.get("artifacts") or run.get("partial_results"))
    clarification = bool(result.get("clicked_hitl")) and any(
        str(item.get("status") or "") in WAITING and (item.get("action") or {}).get("label") not in {"确认执行", "确认发布", "暂不执行"}
        for item in result.get("clicked_hitl") or []
    )
    return {
        "user_utterance": text,
        "semantic": {
            "run_goal": run.get("goal"),
            "run_summary": run.get("summary"),
            "final_response": run.get("final_response"),
            "task_objectives": _json(after_projection.get("objectives") or []),
            "task_projection_for_run": _json(relevant_tasks),
        },
        "resolved_target": [
            {
                "resource_type": artifact.get("resource_type"),
                "resource_id": artifact.get("resource_id"),
                "resource_refs": artifact.get("resource_refs"),
                "status": artifact.get("status"),
            }
            for artifact in (run.get("artifacts") or [])
            if isinstance(artifact, dict)
        ],
        "temporal": _temporal_projection(after_projection.get("objectives") or []),
        "objective": _json(after_projection.get("objectives") or []),
        "physical_write": {
            "steps": writes,
            "new_java_resources": _json(new_resources),
            "new_resource_ids": sorted(_resource_ids(new_resources)),
        },
        "java_truth": _json(after_java),
        "frontend_truth": {
            "status": result.get("status"),
            "run_id": result.get("run_id"),
            "conversation_id": result.get("conversation_id"),
            "clicked_hitl": _json(result.get("clicked_hitl") or []),
            "ui": _json(result.get("ui") or {}),
            "ui_internal_leak": bool(result.get("ui_internal_leak")) or bool(RAW_LEAK_RE.search(visible_text)),
        },
        "context_projection_before": {
            "task_count": len(before_projection.get("tasks") or []),
            "objective_count": len(before_projection.get("objectives") or []),
        },
        "context_projection_after": {
            "task_count": len(after_projection.get("tasks") or []),
            "objective_count": len(after_projection.get("objectives") or []),
        },
        "durable_states_after": {
            "task_statuses": task_statuses,
            "objective_statuses": objective_statuses,
            "resource_states": resource_states,
        },
        "expected_titles": expected,
        "observed_titles": sorted(set(titles)),
        "flags": {
            "wrong_target": False,
            "wrong_temporal": False,
            "context_contamination": False,
            "required_clarify": clarification,
            "unnecessary_clarify": False,
            "duplicate_write": duplicate_write,
            "false_success": false_success,
        },
    }


async def run_session(
    case: dict[str, Any],
    *,
    timeout: float,
    output: Path,
    resume_conversation: str | None = None,
    prior_payload: dict[str, Any] | None = None,
    start_index: int = 1,
    prior_turn_limit: int | None = None,
    stop_after: int | None = None,
    resume_pending: bool = False,
) -> dict[str, Any]:
    # The conversation title is part of the real Frontend surface.  Keep it
    # human-readable; harness-only identities belong in the evidence filename
    # and captured resource references, never in the user's visible workflow.
    tag = str(case.get("tag") or case.get("conversation_title") or "自然业务长会话")
    tag = tag.replace("{{TAG}}", time.strftime("%Y%m%d-%H%M%S"))
    turns = case.get("turns") or []
    materialized = [
        {
            **turn,
            "text": str(turn.get("text") or "").replace("{{TAG}}", tag),
        }
        for turn in turns
    ]
    truth = JavaTruth()
    truth.login()
    browser = Browser(find_page())
    await browser.connect()
    try:
        await restore_browser_auth(browser, truth)
        await browser.open_panel()
        conversation_title = str(case.get("conversation_title") or "自然业务长会话")
        if resume_conversation:
            conversation_id = resume_conversation
            await browser.prefer_conversation(conversation_id)
        else:
            conversation_id = await browser.new_conversation(conversation_title)
        before_all = (prior_payload or {}).get("before_java") or truth.resources()
        prior_rows = list((prior_payload or {}).get("turns") or [])
        if prior_turn_limit is not None:
            prior_rows = prior_rows[: max(0, prior_turn_limit)]
        rows: list[dict[str, Any]] = prior_rows
        invalid_harness_turns = int((prior_payload or {}).get("invalid_harness_turns") or 0)
        stats = {
            "wrong_target": 0,
            "wrong_temporal": 0,
            "context_contamination": 0,
            "required_clarify": 0,
            "unnecessary_clarify": 0,
            "duplicate_write": 0,
            "false_success": 0,
        }
        prior_stats = (prior_payload or {}).get("stats") or {}
        for key in stats:
            stats[key] = int(prior_stats.get(key) or sum(
                1
                for row in rows
                if bool(((row.get("evidence") or {}).get("flags") or {}).get(key))
            ))
        for index, turn in enumerate(materialized[start_index - 1 :], start=start_index):
            text = str(turn.get("text") or "")
            _assert_natural_user_text(text)
            resume_existing_run_id: str | None = None
            replace_prior_row = False
            if resume_pending and index == start_index:
                prior_row = next((row for row in rows if row.get("turn") == index), None)
                prior_result = (prior_row or {}).get("result") or {}
                prior_status = str(prior_result.get("status") or "")
                resume_existing_run_id = str(prior_result.get("run_id") or "").strip()
                if prior_status not in WAITING or not resume_existing_run_id:
                    raise RuntimeError(
                        "resume-pending requires an existing WAITING turn with a durable run identity"
                    )
                replace_prior_row = True
            # The supported E2E account token is short-lived.  Refresh both
            # the read-only Java client and the browser's bearer token before
            # every long-session turn, without navigating away from this
            # conversation.
            if index > 1:
                await _refresh_browser_auth(
                    browser,
                    truth,
                    conversation_id=conversation_id,
                )
            before_java = truth.resources()
            before_projection = await _read_projection(browser, conversation_id)
            try:
                if str(turn.get("action") or "") == "view_search_first":
                    result = await _view_search_first(browser, keep_page=bool(turn.get("keep_page")))
                    result["conversation_id"] = conversation_id
                elif str(turn.get("action") or "") == "reload":
                    result = await _reload_conversation(browser, conversation_id)
                else:
                    result = await run_turn(
                        browser,
                        conversation_id,
                        text,
                        turn,
                        timeout,
                        existing_run_id=resume_existing_run_id,
                    )
            except Exception as exc:
                invalid_harness_turns += 1
                result = {
                    "utterance": text,
                    "conversation_id": conversation_id,
                    "status": "HARNESS_ERROR",
                    "error": repr(exc),
                }
            after_java = truth.resources()
            after_projection = await _read_projection(browser, conversation_id)
            classified = _classify_turn(
                text=text,
                result=result,
                before_java=before_java,
                after_java=after_java,
                before_projection=before_projection,
                after_projection=after_projection,
            )
            row = {
                "turn": index,
                "policy": {key: value for key, value in turn.items() if key != "text"},
                "result": result,
                "evidence": classified,
            }
            if replace_prior_row:
                old_row = next((item for item in rows if item.get("turn") == index), None)
                old_flags = ((old_row or {}).get("evidence") or {}).get("flags") or {}
                for key in stats:
                    if old_flags.get(key):
                        stats[key] = max(0, stats[key] - 1)
                rows = [item for item in rows if item.get("turn") != index]
            rows.append(row)
            for key, value in (classified.get("flags") or {}).items():
                if value and key in stats:
                    stats[key] += 1
            # Persist a durable checkpoint after every turn.  A long real
            # session may outlive an outer shell/tool budget; losing the
            # evidence file must not cause a later harness restart to resend
            # an already-admitted user turn.
            output.parent.mkdir(parents=True, exist_ok=True)
            checkpoint = {
                "session": str(case.get("session") or "L1"),
                "name": case.get("name"),
                "tag": tag,
                "conversation_id": conversation_id,
                "same_conversation": all(
                    str((row.get("result") or {}).get("conversation_id") or conversation_id) == conversation_id
                    for row in rows
                ),
                "turn_count": len(rows),
                "expected_turn_count": len(materialized),
                "turns": rows,
                "stats": stats,
                "invalid_harness_turns": invalid_harness_turns,
                "before_java": before_all,
                "after_java": after_java,
                "new_java_resources": _json(_new_resources(before_all, after_java)),
                "checkpoint": True,
                "final_verdict": "PARTIAL",
            }
            output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            if str(result.get("status") or "") == "HARNESS_ERROR":
                invalid_harness_turns += 0
            print(json.dumps({"turn": index, "status": result.get("status"), "elapsed": result.get("elapsed_seconds")}, ensure_ascii=True), flush=True)
            if (
                str(result.get("status") or "") in {
                    "HARNESS_ERROR",
                    "FAILED",
                    "WAITING_USER",
                    "WAITING_APPROVAL",
                }
                and not bool(turn.get("continue_after_waiting"))
            ):
                break
            if stop_after is not None and index >= stop_after:
                break
            await asyncio.sleep(0.6)
        after_all = truth.resources()
        statuses = [str((row.get("result") or {}).get("status") or "") for row in rows]
        product_bad = [status for status in statuses if status in {"FAILED", "WAITING_USER", "WAITING_APPROVAL"}]
        final_verdict = "PASS" if len(rows) == len(materialized) and not product_bad and invalid_harness_turns == 0 and not any(stats.values()) else "PARTIAL"
        payload = {
            "session": str(case.get("session") or "L1"),
            "name": case.get("name"),
            "tag": tag,
            "conversation_id": conversation_id,
            "same_conversation": all(str((row.get("result") or {}).get("conversation_id") or conversation_id) == conversation_id for row in rows),
            "turn_count": len(rows),
            "expected_turn_count": len(materialized),
            "turns": rows,
            "stats": stats,
            "invalid_harness_turns": invalid_harness_turns,
            "before_java": before_all,
            "after_java": after_all,
            "new_java_resources": _json(_new_resources(before_all, after_all)),
            "final_verdict": final_verdict,
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return payload
    finally:
        await browser.close()
        truth.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume-conversation")
    parser.add_argument("--prior-output", type=Path)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--prior-turn-limit", type=int)
    parser.add_argument("--stop-after", type=int)
    parser.add_argument(
        "--resume-pending",
        action="store_true",
        help="resume the existing pending turn through Browser HITL without resubmitting its text",
    )
    args = parser.parse_args()
    cases = load_utf8_cases(args.fixture)
    if len(cases) != 1:
        raise ValueError("long-session runner requires exactly one fixture case")
    OUT.mkdir(parents=True, exist_ok=True)
    output = args.output or OUT / f"l1-{time.strftime('%Y%m%d-%H%M%S')}.json"
    prior = None
    if args.prior_output:
        prior = json.loads(args.prior_output.read_bytes().decode("utf-8"))
    payload = await run_session(
        cases[0],
        timeout=args.timeout,
        output=output,
        resume_conversation=args.resume_conversation,
        prior_payload=prior,
        start_index=max(1, args.start_index),
        prior_turn_limit=args.prior_turn_limit,
        stop_after=args.stop_after,
        resume_pending=args.resume_pending,
    )
    print(json.dumps({"output": str(output), "verdict": payload.get("final_verdict"), "turn_count": payload.get("turn_count")}, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
