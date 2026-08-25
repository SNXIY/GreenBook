"""Small real-browser Conversation Lifecycle smoke.

Run only with the canonical Frontend/API already running and a logged-in Edge
page attached to CDP :9222.  It intentionally uses chat plus a missing implicit
target, so it does not create a business Draft or issue a physical WRITE.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# The script is invoked by path from the repository root; make the existing
# browser harness importable without changing its production/runtime path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.dev.overnight_stable_baseline_browser import Browser, find_page, run_turn
from scripts.dev.round1_final_closure_v2 import JavaTruth, restore_browser_auth


async def wait_for(browser: Browser, expression: str, *, timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await browser.evaluate(expression)
        if value:
            return value
        await asyncio.sleep(0.25)
    raise TimeoutError(f"browser condition timed out: {expression}")


async def selected_conversation(browser: Browser) -> str:
    return str(await browser.evaluate(
        "document.querySelector('[data-conversation-id][aria-pressed=\"true\"]')?.dataset.conversationId || ''"
    ) or "")


async def click_new_conversation(browser: Browser) -> str:
    await wait_for(
        browser,
        "Boolean(document.querySelector('[data-testid=\"new-conversation\"]') && !document.querySelector('[data-testid=\"new-conversation\"]').disabled)",
    )
    await wait_for(
        browser,
        "Boolean(document.querySelector('[data-conversation-id][aria-pressed=\"true\"]'))",
    )
    before = await selected_conversation(browser)
    clicked = await browser.evaluate(
        "document.querySelector('[data-testid=\"new-conversation\"]')?.click(); true"
    )
    if not clicked:
        raise RuntimeError("new conversation button was not found")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        current = await selected_conversation(browser)
        if current and current != before:
            await wait_for(
                browser,
                "Boolean(document.querySelector('textarea[name=\"agent-message\"]'))",
            )
            return current
        await asyncio.sleep(0.25)
    raise TimeoutError("new conversation was not selected")


async def switch_to(browser: Browser, conversation_id: str) -> None:
    await browser.evaluate(
        f"document.querySelector('[data-conversation-id={json.dumps(conversation_id)}]')?.click(); true"
    )
    await wait_for(
        browser,
        f"Boolean(document.querySelector('[data-conversation-id={json.dumps(conversation_id)}][aria-pressed=\"true\"]'))",
    )
    await wait_for(
        browser,
        "Boolean(document.querySelector('textarea[name=\"agent-message\"]'))",
    )


async def run_turn_with_diagnostics(
    browser: Browser,
    conversation_id: str,
    text: str,
) -> dict[str, Any]:
    try:
        return await run_turn(browser, conversation_id, text, {}, 90.0)
    except Exception:
        print(json.dumps({
            "turn_failure_diagnostics": {
                "conversation_id": conversation_id,
                "selected": await selected_conversation(browser),
                "conversations": await browser.list_conversations(),
                "runs_response": await browser.api("GET", "/api/v1/agent/runs?limit=100"),
                "messages": await browser.messages(conversation_id),
                "snapshot": await browser.snapshot(),
            }
        }, ensure_ascii=False))
        raise


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    truth = JavaTruth()
    try:
        truth.login()
        await restore_browser_auth(browser, truth)
        await browser.open_panel()
        conversation_a = await click_new_conversation(browser)
        marker = "Conversation A only marker 20260825"
        a_result = await run_turn_with_diagnostics(browser, conversation_a, marker)

        conversation_b = await click_new_conversation(browser)
        b_messages_before = await browser.messages(conversation_b)
        if b_messages_before:
            raise AssertionError("new Conversation B was not created with empty messages")

        implicit_result = await run_turn_with_diagnostics(
            browser,
            conversation_b,
            "把刚才那篇改一下",
        )
        implicit_run = implicit_result.get("run") or {}
        if implicit_run.get("execution_id") or implicit_run.get("execution_ids"):
            raise AssertionError("implicit cross-conversation target admitted an execution")

        await switch_to(browser, conversation_a)
        a_messages = await browser.messages(conversation_a)
        b_messages = await browser.messages(conversation_b)
        if not any(marker in str(item.get("content") or "") for item in a_messages):
            raise AssertionError("Conversation A message was not restored")
        if any(marker in str(item.get("content") or "") for item in b_messages):
            raise AssertionError("Conversation A message leaked into B")

        await browser.evaluate("location.reload(); true")
        await asyncio.sleep(1.0)
        await browser.open_panel()
        restored_id = await wait_for(
            browser,
            "document.querySelector('[data-conversation-id][aria-pressed=\"true\"]')?.dataset.conversationId || ''",
        )
        if restored_id != conversation_a:
            raise AssertionError(f"reload selected {restored_id!r}, expected {conversation_a!r}")
        restored_messages = await browser.messages(conversation_a)
        if not any(marker in str(item.get("content") or "") for item in restored_messages):
            raise AssertionError("Conversation A message was not restored after reload")

        print(json.dumps({
            "status": "PASS",
            "conversation_a": conversation_a,
            "conversation_b": conversation_b,
            "a_run_status": a_result.get("status"),
            "implicit_b_run_status": implicit_result.get("status"),
            "implicit_b_execution": implicit_run.get("execution_id") or implicit_run.get("execution_ids"),
            "reload_selected": restored_id,
            "cross_conversation_message_leak": 0,
        }, ensure_ascii=False))
    finally:
        await browser.close()
        truth.close()


if __name__ == "__main__":
    asyncio.run(main())
