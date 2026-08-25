"""Fresh Golden 3 browser regression after the Run convergence fix."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.dev.overnight_stable_baseline_browser import Browser, find_page, load_utf8_cases
from scripts.dev.round1_final_closure_v2 import JavaTruth, restore_browser_auth, run_case


FIXTURE = ROOT / "scripts" / "dev" / "fixtures" / "golden3_retest_v2.json"
OUT = ROOT / ".runtime" / "round1-final-v2"


async def main() -> None:
    tag = time.strftime("%Y%m%d-%H%M%S")
    truth = JavaTruth()
    truth.login()
    browser = Browser(find_page())
    await browser.connect()
    try:
        await restore_browser_auth(browser, truth)
        await browser.open_panel()
        cases = load_utf8_cases(FIXTURE)
        turns = [
            (
                str(item["text"]),
                {"hitl_sequence": list(item.get("hitl_sequence") or [])},
            )
            for item in cases
        ]
        case = await run_case(browser, f"golden3-retest-{tag}", turns, 300.0, truth)
    finally:
        await browser.close()
        truth.close()
    output = OUT / f"golden3-retest-{tag}.json"
    output.write_text(
        json.dumps({"tag": tag, "fixture": str(FIXTURE), "case": case}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output),
        "tag": tag,
        "name": case.get("name"),
        "turns": [
            {
                "turn": turn.get("turn"),
                "status": turn.get("status"),
                "run_id": turn.get("run_id"),
                "clicked_hitl": turn.get("clicked_hitl"),
            }
            for turn in case.get("turns", [])
        ],
    }, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
