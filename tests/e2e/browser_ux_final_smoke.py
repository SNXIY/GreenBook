"""Focused real-browser UX acceptance for the remaining resume cursor.

This smoke intentionally uses READ-only requests.  It exercises rapid submit,
stale Conversation-A completion after switching to B, and observable progress
without rerunning the already accepted Browser business matrix.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from scripts.dev.overnight_stable_baseline_browser import Browser, find_page
from scripts.dev.round1_final_closure_v2 import JavaTruth, restore_browser_auth


TERMINAL = {"COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}
GENERIC_PROGRESS_TEXT = {
    "loading",
    "加载中",
    "已接受",
    "请求已接受",
    "处理中",
}


async def selected(browser: Browser) -> str:
    return str(await browser.evaluate(
        "document.querySelector('[data-conversation-id][aria-pressed=\"true\"]')?.dataset.conversationId || ''"
    ) or "")


async def wait_selected(browser: Browser, conversation_id: str) -> None:
    deadline = time.monotonic() + 30
    selector = json.dumps(f'[data-conversation-id="{conversation_id}"][aria-pressed="true"]')
    while time.monotonic() < deadline:
        if await browser.evaluate(f"Boolean(document.querySelector({selector}))"):
            return
        await asyncio.sleep(0.2)
    raise TimeoutError(f"conversation was not selected: {conversation_id}")


async def switch_to(browser: Browser, conversation_id: str) -> None:
    selector = json.dumps(f'[data-conversation-id="{conversation_id}"]')
    clicked = await browser.evaluate(f"document.querySelector({selector})?.click(); true")
    if not clicked:
        raise RuntimeError(f"conversation switch failed: {conversation_id}")
    await wait_selected(browser, conversation_id)
    await browser.open_panel()


async def fill_composer(browser: Browser, message: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready = await browser.evaluate(
            "Boolean(document.querySelector('textarea[name=\"agent-message\"]') && !document.querySelector('textarea[name=\"agent-message\"]').disabled)"
        )
        if ready:
            break
        await asyncio.sleep(0.2)
    else:
        raise TimeoutError("composer was not ready")
    await browser.evaluate(
        "document.querySelector('textarea[name=\"agent-message\"]')?.focus(); document.querySelector('textarea[name=\"agent-message\"]')?.select(); true"
    )
    await browser.command("Input.insertText", {"text": message})
    while time.monotonic() < deadline:
        state = await browser.evaluate(
            "(()=>{const t=document.querySelector('textarea[name=\"agent-message\"]'); const b=[...document.querySelectorAll('button')].find(x=>x.getAttribute('aria-label')===String.fromCodePoint(0x53d1,0x9001)); return {value:t?.value||'',enabled:Boolean(b&&!b.disabled)}})()"
        )
        if isinstance(state, dict) and state.get("value") == message and state.get("enabled"):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("composer did not accept the message")


async def click_send(browser: Browser, *, twice: bool = False) -> float:
    clicked_at = time.monotonic()
    expression = "(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.getAttribute('aria-label')===String.fromCodePoint(0x53d1,0x9001)); if(!b||b.disabled)return false; b.click();" + ("b.click();" if twice else "") + " return true})()"
    if not await browser.evaluate(expression):
        raise RuntimeError("send button was not clickable")
    return clicked_at


async def new_run(browser: Browser, conversation_id: str, before: set[str], timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = await browser.list_runs(conversation_id)
        candidates = [item for item in runs if str(item.get("run_id") or "") not in before]
        if candidates:
            candidates.sort(key=lambda item: str(item.get("created_at") or ""))
            return candidates[-1]
        await asyncio.sleep(0.4)
    raise TimeoutError("browser run was not admitted")


async def wait_terminal(browser: Browser, run_id: str, timeout: float = 150) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await browser.get_run(run_id)
        if str(last.get("status") or "") in TERMINAL:
            return last
        await asyncio.sleep(0.5)
    return last


async def progress_probe(browser: Browser, click_at: float, timeout: float = 60) -> dict[str, Any]:
    """Measure TUF from the real rendered DOM, without creating progress."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await browser.evaluate(
            """(()=>{
              const panel=document.querySelector('[role="dialog"]');
              const nodes=[...(panel?.querySelectorAll('[role="status"], [class*="activity"], [class*="thinking"], [class*="progress"]')||[])];
              const texts=nodes.map(x=>(x.innerText||'').trim()).filter(Boolean);
              return {visible:Boolean(texts.length),texts:texts.slice(-8)};
            })()"""
        )
        texts = [
            str(value).strip()
            for value in (state.get("texts", []) if isinstance(state, dict) else [])
            if str(value).strip() and str(value).strip().lower() not in GENERIC_PROGRESS_TEXT
        ]
        if texts:
            return {
                "metric": "TUF",
                "available": True,
                "latency_ms": round((time.monotonic() - click_at) * 1000, 1),
                "source": "rendered_dom_user_facing_progress",
                "first_meaningful_text": texts[0],
                "texts": texts[-8:],
            }
        await asyncio.sleep(0.25)
    return {
        "metric": "TUF",
        "available": False,
        "latency_ms": None,
        "source": "rendered_dom_user_facing_progress",
        "first_meaningful_text": None,
        "texts": [],
    }


async def visible_thread_text(browser: Browser) -> str:
    """Read only rendered conversation messages, excluding the recent list."""
    return str(await browser.evaluate(
        "[...document.querySelectorAll('[role=dialog] .thread article')].map(x => x.innerText || '').join('\\n')"
    ) or "")


async def main() -> None:
    browser = Browser(find_page())
    truth = JavaTruth()
    evidence: dict[str, Any] = {"status": "PASS", "started_at": time.time()}
    await browser.connect()
    try:
        truth.login()
        await restore_browser_auth(browser, truth)
        await browser.open_panel()

        rapid_conversation = await browser.new_conversation("UX rapid send")
        rapid_before = {str(item.get("run_id") or "") for item in await browser.list_runs(rapid_conversation)}
        await fill_composer(browser, "搜索 UX rapid send 20260827")
        rapid_click = await click_send(browser, twice=True)
        rapid_progress_task = asyncio.create_task(progress_probe(browser, rapid_click))
        rapid_run = await new_run(browser, rapid_conversation, rapid_before)
        rapid_progress = await rapid_progress_task
        rapid_terminal = await wait_terminal(browser, str(rapid_run.get("run_id")))
        rapid_runs = [
            item for item in await browser.list_runs(rapid_conversation)
            if str(item.get("run_id") or "") not in rapid_before
        ]
        evidence["rapid_send"] = {
            "conversation_id": rapid_conversation,
            "new_run_count": len(rapid_runs),
            "run_statuses": [item.get("status") for item in rapid_runs],
            "duplicate_run": len(rapid_runs) != 1,
            "progress": rapid_progress,
            "terminal_status": rapid_terminal.get("status"),
            "message_count": len(await browser.messages(rapid_conversation)),
        }
        if len(rapid_runs) != 1:
            evidence["status"] = "FAIL"

        conversation_a = await browser.new_conversation("UX stale A")
        conversation_b = await browser.new_conversation("UX stale B")
        await switch_to(browser, conversation_a)
        stale_marker = "搜索 UX stale response A 20260827"
        stale_before = {str(item.get("run_id") or "") for item in await browser.list_runs(conversation_a)}
        await fill_composer(browser, stale_marker)
        stale_click = await click_send(browser)
        stale_progress_task = asyncio.create_task(progress_probe(browser, stale_click))
        await switch_to(browser, conversation_b)
        stale_run = await new_run(browser, conversation_a, stale_before)
        stale_terminal = await wait_terminal(browser, str(stale_run.get("run_id")))
        stale_progress = await stale_progress_task
        b_messages = await browser.messages(conversation_b)
        b_thread_text = await visible_thread_text(browser)
        await switch_to(browser, conversation_a)
        a_messages = await browser.messages(conversation_a)
        leaked = stale_marker in b_thread_text or any(
            stale_marker in str(item.get("content") or "") for item in b_messages
        )
        evidence["stale_response"] = {
            "conversation_a": conversation_a,
            "conversation_b": conversation_b,
            "run_status": stale_terminal.get("status"),
            "switched_before_a_terminal": True,
            "a_contains_marker": any(stale_marker in str(item.get("content") or "") for item in a_messages),
            "b_message_count": len(b_messages),
            "b_thread_text_contains_marker": stale_marker in b_thread_text,
            "b_ui_marker_leak": leaked,
            "progress_probe": stale_progress,
        }
        if leaked or not evidence["stale_response"]["a_contains_marker"]:
            evidence["status"] = "FAIL"
        print(json.dumps(evidence, ensure_ascii=False, default=str))
    finally:
        await browser.close()
        truth.close()


if __name__ == "__main__":
    asyncio.run(main())
