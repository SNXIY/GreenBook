import asyncio
import json
import time

from overnight_stable_baseline_browser import Browser, find_page


PREFIXES = ("GB-STABLE-R1-SCHED3-JAVA", "GB-STABLE-R1-SCHED3-REDIS")


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    deadline = time.monotonic() + 420
    try:
        while time.monotonic() < deadline:
            result = await browser.evaluate(
                """(async()=>{
                  const raw=JSON.parse(localStorage.getItem('zhiguang_auth_tokens')||'{}');
                  const headers={Authorization:'Bearer '+(raw.accessToken||'')};
                  const [postsResponse,schedulesResponse]=await Promise.all([
                    fetch('http://127.0.0.1:8080/api/v1/agent/me/posts?page=1&size=100',{headers}),
                    fetch('http://127.0.0.1:8080/api/v1/agent/publications/schedules',{headers})
                  ]);
                  return {
                    posts: postsResponse.ok ? await postsResponse.json() : {status:postsResponse.status},
                    schedules: schedulesResponse.ok ? await schedulesResponse.json() : {status:schedulesResponse.status}
                  };
                })()"""
            )
            posts = result.get("posts") if isinstance(result, dict) else []
            schedules = result.get("schedules") if isinstance(result, dict) else []
            if not isinstance(posts, list):
                posts = posts.get("items", []) if isinstance(posts, dict) else []
            if not isinstance(schedules, list):
                schedules = schedules.get("items", []) if isinstance(schedules, dict) else []
            matching_posts = [
                {"id": p.get("postId") or p.get("id"), "title": p.get("title"), "status": p.get("status")}
                for p in posts if any(str(p.get("title") or "").startswith(prefix) for prefix in PREFIXES)
            ]
            matching_schedules = [
                {"id": s.get("scheduleId") or s.get("id"), "draft_id": s.get("draftId"), "status": s.get("status"), "run_at": s.get("runAt") or s.get("run_at")}
                for s in schedules if any(str(s.get("title") or s.get("draftTitle") or "").startswith(prefix) for prefix in PREFIXES)
            ]
            print(json.dumps({"at":time.time(),"posts":matching_posts,"schedules":matching_schedules},ensure_ascii=True), flush=True)
            if all(any(str(item.get("title") or "").startswith(prefix) for item in matching_posts) for prefix in PREFIXES):
                break
            await asyncio.sleep(20)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
