from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.llm import DeepSeekClient
from app.tools import tool_registry


async def main() -> None:
    scenarios = json.loads(
        (Path(__file__).parent / "multiturn_scenarios.json").read_text(
            encoding="utf-8"
        )
    )
    client = DeepSeekClient(Settings(), tool_registry)
    passed = 0
    reference_scores: list[float] = []
    try:
        for scenario in scenarios:
            decision = await client.decide_execution(
                prompt=scenario["request"],
                context_post_id=None,
                context_comment_id=None,
                client_timezone="Asia/Shanghai",
                history=list(scenario.get("history") or []),
                memories=[],
                recalled_memories=[],
                conversation_workspace=dict(scenario.get("workspace") or {}),
            )
            expected_refs = set(scenario.get("expected_refs") or [])
            actual_refs = set(decision.referenced_entities)
            reference_score = (
                len(expected_refs & actual_refs) / len(expected_refs | actual_refs)
                if expected_refs or actual_refs
                else 1.0
            )
            reference_scores.append(reference_score)
            ok = (
                decision.turn_relation == scenario["expected_relation"]
                and reference_score == 1.0
                and (
                    scenario.get("expected_path") is None
                    or decision.execution_path == scenario["expected_path"]
                )
                and (
                    not scenario.get("expected_clarification")
                    or bool(decision.direct_response)
                )
            )
            passed += int(ok)
            print(
                json.dumps(
                    {
                        "scenario": scenario["name"],
                        "passed": ok,
                        "relation": decision.turn_relation,
                        "refs": decision.referenced_entities,
                        "path": decision.execution_path,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await client.close()

    total = max(1, len(scenarios))
    print(
        json.dumps(
            {
                "turn_relation_accuracy": round(passed / total, 4),
                "entity_reference_jaccard": round(
                    sum(reference_scores) / total,
                    4,
                ),
                "scenario_count": len(scenarios),
            },
            ensure_ascii=False,
        )
    )
    if passed != len(scenarios):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
