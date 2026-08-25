import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    try:
        snapshot = await browser.snapshot()
        value = await browser.evaluate(
            """(()=>({
              url: location.href,
              cards: [...document.querySelectorAll('article')].map(a => ({
                text: (a.innerText || '').slice(-1200),
                buttons: [...a.querySelectorAll('button')].map(b => ({text:b.innerText, disabled:b.disabled, class:String(b.className)}))
              })).slice(-8)
            }))()"""
        )
        print(json.dumps({"snapshot": snapshot, "cards": value}, ensure_ascii=True))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
