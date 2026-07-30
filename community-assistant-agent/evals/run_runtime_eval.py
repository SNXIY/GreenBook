from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation import evaluate_retrieval, evaluate_runtime


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic metric-contract fixtures, not a production benchmark."
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Acknowledge that all counts and retrieval labels are synthetic fixtures.",
    )
    args = parser.parse_args()
    if not args.fixture:
        parser.error(
            "This command uses synthetic fixtures. Pass --fixture, or run "
            "evals/run_runtime_report.py for observed database metrics."
        )

    runtime = evaluate_runtime(
        resumed_tasks=10,
        recovered_tasks=10,
        stale_results=5,
        rejected_stale_results=5,
        approval_decisions=8,
        correct_approval_decisions=8,
        artifact_versions=20,
        correct_artifact_versions=20,
        terminal_tool_jobs=12,
        completed_tool_jobs=11,
    )
    retrieval = evaluate_retrieval(
        relevant_by_query=[{"post-1"}, {"post-4"}, {"post-8"}],
        ranked_results=[
            ["post-1", "post-2"],
            ["post-3", "post-4"],
            ["post-8"],
        ],
    )
    print(
        json.dumps(
            {
                "mode": "synthetic_contract_fixture",
                "benchmark": False,
                "disclaimer": (
                    "These values verify metric formulas only and must not be "
                    "reported as observed runtime performance."
                ),
                "runtime": runtime.as_dict(),
                "retrieval": retrieval.as_dict(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
