"""Read-only shape probe for scheduler -> My Content projection."""

from __future__ import annotations

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
              const headers={Authorization:'Bearer '+(raw.accessToken||'')};
              const response=await fetch('http://127.0.0.1:8080/api/v1/agent/me/posts?page=1&size=100',{headers});
              const data=response.ok?await response.json():{status:response.status};
              const items=Array.isArray(data)?data:(data.items||[]);
              return {topKeys:Object.keys(data||{}),count:items.length,items:items.slice(0,20).map(x=>({keys:Object.keys(x),title:x.title,status:x.status,postId:x.postId,id:x.id,draftId:x.draftId,source:x}))};
            })()"""
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
