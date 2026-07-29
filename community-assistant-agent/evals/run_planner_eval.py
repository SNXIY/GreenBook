from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.evaluation import evaluate_plan
from app.llm import DeepSeekClient
from app.tools import tool_registry


async def main() -> None:
    scenarios = json.loads(
        (Path(__file__).parent / "scenarios.json").read_text(encoding="utf-8")
    )
    client = DeepSeekClient(get_settings(), tool_registry)
    passed = 0
    totals: dict[str, float] = {}
    try:
        for scenario in scenarios:
            intent = await client.understand_intent(
                prompt=scenario["prompt"],
                context_post_id=scenario.get("context_post_id"),
                context_comment_id=None,
                history=[],
                memories=[],
            )
            plan = await client.plan(
                prompt=scenario["prompt"],
                context_post_id=scenario.get("context_post_id"),
                context_comment_id=None,
                client_timezone="Asia/Shanghai",
                history=[],
                memories=[],
                structured_intent=intent,
            )
            tools = [step.tool for step in plan.steps]
            required = set(scenario.get("required_tools", []))
            forbidden = set(scenario.get("forbidden_tools", []))
            evaluation = evaluate_plan(
                intent=intent,
                plan=plan,
                expected_domain=scenario["expected_domain"],
                required_capabilities=set(
                    scenario.get("required_capabilities", [])
                ),
                required_tools=required,
                forbidden_tools=forbidden,
                expected_agents=set(scenario.get("expected_agents", [])),
            )
            metrics = evaluation.as_dict()
            ok = (
                required.issubset(tools)
                and forbidden.isdisjoint(tools)
                and metrics["overall"] >= 0.8
            )
            passed += int(ok)
            for key, value in metrics.items():
                totals[key] = totals.get(key, 0.0) + value
            print(
                json.dumps(
                    {
                        "scenario": scenario["id"],
                        "passed": ok,
                        "tools": tools,
                        "agents": [step.agent for step in plan.steps],
                        "intent": intent.model_dump(mode="json"),
                        "metrics": metrics,
                        "summary": plan.summary,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await client.close()
    print(f"{passed}/{len(scenarios)} scenarios passed")
    print(
        json.dumps(
            {
                "aggregate": {
                    key: round(value / max(1, len(scenarios)), 4)
                    for key, value in totals.items()
                }
            },
            ensure_ascii=False,
        )
    )
    if passed != len(scenarios):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
