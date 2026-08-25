"""Read-only browser-authenticated run inspection for evaluation evidence."""

from __future__ import annotations

import argparse
import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_ids", nargs="+")
    args = parser.parse_args()
    browser = Browser(find_page())
    await browser.connect()
    try:
        output = []
        for run_id in args.run_ids:
            response = await browser.api("GET", f"/api/v1/agent/runs/{run_id}")
            run = response.get("data") or {}
            conversation_id = str(run.get("conversation_id") or "")
            messages = await browser.messages(conversation_id) if conversation_id else []
            output.append({
                "run_id": run_id,
                "status_code": response.get("status"),
                "status": run.get("status"),
                "conversation_id": conversation_id,
                "execution_id": run.get("execution_id"),
                "execution_ids": run.get("execution_ids"),
                "task_ids": run.get("task_ids"),
                "approval": run.get("approval"),
                "steps": run.get("steps"),
                "artifacts": run.get("artifacts"),
                "performance": run.get("performance"),
                "messages": [
                    {
                        "role": item.get("role"),
                        "content": str(item.get("content") or "")[-1000:],
                        "parts": item.get("parts"),
                        "run_id": item.get("run_id"),
                        "execution_id": item.get("execution_id"),
                    }
                    for item in messages[-6:]
                ],
            })
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
