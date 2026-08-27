"""Run one additional turn in an existing browser-authenticated conversation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts.dev.overnight_stable_baseline_browser import (
    Browser,
    find_page,
    run_turn,
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--hitl", default="")
    args = parser.parse_args()

    browser = Browser(find_page())
    await browser.connect()
    try:
        await browser.prefer_conversation(args.conversation_id)
        result = await run_turn(
            browser,
            args.conversation_id,
            args.text,
            {"hitl": args.hitl} if args.hitl else {},
            args.timeout,
        )
    finally:
        await browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result.get("status"), "run_id": result.get("run_id")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
