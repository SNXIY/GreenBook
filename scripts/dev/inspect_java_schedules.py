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
              const h={Authorization:'Bearer '+(raw.accessToken||'')};
              const [a,b]=await Promise.all([
                fetch('http://127.0.0.1:8080/api/v1/agent/publications/schedules',{headers:h}),
                fetch('http://127.0.0.1:8080/api/v1/agent/me/posts?page=1&size=100',{headers:h})
              ]);
              return {schedulesStatus:a.status,schedules:await a.json(),postsStatus:b.status,posts:await b.json()};
            })()"""
        )
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
