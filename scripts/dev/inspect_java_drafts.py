import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    try:
        result = await browser.evaluate(
            """(async()=>{
              const raw=JSON.parse(localStorage.getItem('zhiguang_auth_tokens')||'{}');
              const r=await fetch('http://127.0.0.1:8080/api/v1/agent/me/drafts',{headers:{Authorization:'Bearer '+(raw.accessToken||'')}});
              return {status:r.status,data:await r.json()};
            })()"""
        )
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
