from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.model_routing import ModelRouter


def main() -> None:
    scenarios = json.loads(
        (Path(__file__).parent / "model_routing_scenarios.json").read_text(
            encoding="utf-8"
        )
    )
    router = ModelRouter(
        Settings(
            DEEPSEEK_API_KEY=os.getenv("DEEPSEEK_API_KEY", "evaluation-only"),
            service_shared_secret=os.getenv(
                "ASSISTANT_SERVICE_SHARED_SECRET", "evaluation-only"
            ),
        )
    )
    passed = 0
    thinking_routes = 0
    fast_routes = 0
    for scenario in scenarios:
        selected = router.candidates(scenario["operation"])[0]
        ok = (
            selected.tier == scenario["expected_tier"]
            and selected.thinking is scenario["expected_thinking"]
        )
        passed += int(ok)
        thinking_routes += int(selected.thinking)
        fast_routes += int(selected.tier == "fast")
        print(
            json.dumps(
                {
                    "operation": scenario["operation"],
                    "passed": ok,
                    "tier": selected.tier,
                    "model": selected.model,
                    "thinking": selected.thinking,
                    "timeout_seconds": selected.timeout_seconds,
                },
                ensure_ascii=False,
            )
        )
    total = max(1, len(scenarios))
    print(
        json.dumps(
            {
                "route_accuracy": round(passed / total, 4),
                "fast_route_rate": round(fast_routes / total, 4),
                "thinking_route_rate": round(thinking_routes / total, 4),
                "policy_signature": router.identity()["signature"],
            },
            ensure_ascii=False,
        )
    )
    if passed != len(scenarios):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
