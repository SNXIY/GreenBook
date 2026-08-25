"""Read-only monitor for one real scheduler acceptance run."""

from __future__ import annotations

import asyncio
import json
import time

from overnight_stable_baseline_browser import Browser, find_page


DRAFT_ID = "350038249634402304"
SCHEDULE_ID = "350038274598899712"


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    deadline = time.monotonic() + 420
    try:
        while time.monotonic() < deadline:
            result = await browser.evaluate(
                f"""(async()=>{{
                  const raw=JSON.parse(localStorage.getItem('zhiguang_auth_tokens')||'{{}}');
                  const headers={{Authorization:'Bearer '+(raw.accessToken||'')}};
                  const [postsResponse,schedulesResponse]=await Promise.all([
                    fetch('http://127.0.0.1:8080/api/v1/agent/me/posts?page=1&size=100',{{headers}}),
                    fetch('http://127.0.0.1:8080/api/v1/agent/publications/schedules',{{headers}})
                  ]);
                  const posts=postsResponse.ok?await postsResponse.json():[];
                  const schedules=schedulesResponse.ok?await schedulesResponse.json():[];
                  const postItems=Array.isArray(posts)?posts:(posts.items||[]);
                  const scheduleItems=Array.isArray(schedules)?schedules:(schedules.items||[]);
                  const schedule=scheduleItems.find(x=>String(x.scheduleId||x.id||'')==='{SCHEDULE_ID}')||null;
                  const post=postItems.find(x=>String(x.draftId||x.draft_id||'')==='{DRAFT_ID}')||null;
                  return {{schedule: schedule ? {{status:schedule.status,runAt:schedule.runAt||schedule.run_at}} : null,
                    post: post ? {{status:post.status,publishTime:post.publishTime||post.publish_time}} : null}};
                }})()"""
            )
            print(json.dumps({"at": time.time(), **(result or {})}, ensure_ascii=False), flush=True)
            schedule = (result or {}).get("schedule") if isinstance(result, dict) else None
            post = (result or {}).get("post") if isinstance(result, dict) else None
            if schedule and str(schedule.get("status") or "").upper() in {"PUBLISHED", "COMPLETED", "CANCELLED", "FAILED"}:
                if post and str(post.get("status") or "").lower() == "published":
                    break
            await asyncio.sleep(20)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
