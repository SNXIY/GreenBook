"""Read-only browser-authenticated conversation run listing."""

from __future__ import annotations

import argparse
import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("conversation_id")
    args = parser.parse_args()
    browser = Browser(find_page())
    await browser.connect()
    try:
        response = await browser.api("GET", "/api/v1/agent/runs?limit=100")
        rows = response.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("items") or []
        print(json.dumps([
            {
                "run_id": item.get("run_id"),
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "execution_id": item.get("execution_id"),
                "follow_up_of": item.get("follow_up_of"),
                "goal": item.get("goal"),
            }
            for item in rows
            if str(item.get("conversation_id") or "") == args.conversation_id
        ], ensure_ascii=False, indent=2))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
