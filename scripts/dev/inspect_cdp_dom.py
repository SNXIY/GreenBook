import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    try:
        await browser.command("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__gbErrors=[]; window.addEventListener('error',e=>window.__gbErrors.push(String(e.error?.stack||e.message))); window.addEventListener('unhandledrejection',e=>window.__gbErrors.push(String(e.reason?.stack||e.reason)));"})
        await browser.command("Page.reload")
        await asyncio.sleep(2)
        snapshot = await browser.snapshot()
        state = await browser.evaluate("JSON.stringify({href:location.href, authenticated:Boolean(localStorage.getItem('zhiguang_auth_tokens')), rootLength:document.getElementById('root')?.innerHTML.length||0, errors:window.__gbErrors||[]})")
        print(json.dumps({"url": snapshot.get("url"), "textareas": len(snapshot.get("textareas") or []), "buttons": snapshot.get("buttons"), "state": state}, ensure_ascii=True))
        await browser.evaluate("document.querySelector('button[class*=agentTrigger]')?.click(); true")
        await asyncio.sleep(1)
        print(json.dumps(await browser.snapshot(), ensure_ascii=True))
        messages = await browser.messages("8dea51c8-0b99-4ee7-93db-c4b1df3c7f63")
        print(json.dumps([{"role": item.get("role"), "run_id": item.get("run_id"), "content": str(item.get("content") or "")[:120]} for item in messages], ensure_ascii=True))
    finally:
        await browser.close()


asyncio.run(main())
