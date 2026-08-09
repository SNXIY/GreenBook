from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.database import AgentEvent, Approval, Artifact, Database, Run, ToolJob, utc_now


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _average(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _distribution(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _artifact_is_structurally_valid(artifact: Artifact) -> bool:
    return (
        artifact.version >= 1
        and bool(re.fullmatch(r"[0-9a-f]{64}", artifact.content_hash))
        and isinstance(artifact.content, dict)
        and isinstance(artifact.parent_artifact_ids, list)
    )


async def build_report(*, days: int) -> dict[str, Any]:
    settings = get_settings()
    database = Database(settings.database_url)
    cutoff = utc_now() - timedelta(days=days)
    try:
        async with database.sessions() as session:
            runs = list(
                (
                    await session.scalars(
                        select(Run)
                        .where(Run.created_at >= cutoff)
                        .order_by(Run.created_at)
                    )
                ).all()
            )
            run_ids = [run.id for run in runs]
            if run_ids:
                events = list(
                    (
                        await session.scalars(
                            select(AgentEvent).where(AgentEvent.run_id.in_(run_ids))
                        )
                    ).all()
                )
                approvals = list(
                    (
                        await session.scalars(
                            select(Approval).where(Approval.run_id.in_(run_ids))
                        )
                    ).all()
                )
                artifacts = list(
                    (
                        await session.scalars(
                            select(Artifact).where(Artifact.run_id.in_(run_ids))
                        )
                    ).all()
                )
                tool_jobs = list(
                    (
                        await session.scalars(
                            select(ToolJob).where(ToolJob.run_id.in_(run_ids))
                        )
                    ).all()
                )
            else:
                events = []
                approvals = []
                artifacts = []
                tool_jobs = []
    finally:
        await database.close()

    completed = [run for run in runs if run.status == "COMPLETED"]
    retried = [run for run in runs if run.attempts > 1]
    resumed_run_ids = {
        event.run_id for event in events if event.type == "RUN_RESUMED"
    }
    resumed = [run for run in runs if run.id in resumed_run_ids]
    end_to_end_ms = [
        max(0, int((run.completed_at - run.created_at).total_seconds() * 1000))
        for run in completed
        if run.completed_at is not None
    ]
    terminal_tool_jobs = [
        job for job in tool_jobs if job.status in {"COMPLETED", "FAILED", "DEAD", "CANCELLED"}
    ]
    structurally_valid_artifacts = sum(
        _artifact_is_structurally_valid(artifact) for artifact in artifacts
    )

    return {
        "mode": "observed_runtime_report",
        "benchmark": False,
        "generated_at": utc_now().isoformat(),
        "window_days": days,
        "sample_sizes": {
            "runs": len(runs),
            "completed_runs": len(completed),
            "events": len(events),
            "approvals": len(approvals),
            "artifacts": len(artifacts),
            "tool_jobs": len(tool_jobs),
        },
        "runs": {
            "status_distribution": _distribution([run.status for run in runs]),
            "execution_path_distribution": _distribution(
                [run.execution_path for run in runs]
            ),
            "workload_lane_distribution": _distribution(
                [run.workload_lane for run in runs]
            ),
            "completion_rate": _ratio(len(completed), len(runs)),
            "average_model_calls": _average([run.model_calls for run in runs]),
            "average_tool_calls": _average([run.tool_calls for run in runs]),
            "average_model_duration_ms": _average(
                [run.model_duration_ms for run in runs]
            ),
            "average_tool_duration_ms": _average(
                [run.tool_duration_ms for run in runs]
            ),
            "end_to_end_duration_ms": {
                "average": _average(end_to_end_ms),
                "p50": _percentile(end_to_end_ms, 0.50),
                "p95": _percentile(end_to_end_ms, 0.95),
            },
        },
        "recovery": {
            "retried_runs": len(retried),
            "retried_runs_completed": sum(
                run.status == "COMPLETED" for run in retried
            ),
            "retry_recovery_rate": _ratio(
                sum(run.status == "COMPLETED" for run in retried),
                len(retried),
            ),
            "resumed_runs": len(resumed),
            "resumed_runs_completed": sum(
                run.status == "COMPLETED" for run in resumed
            ),
            "resume_recovery_rate": _ratio(
                sum(run.status == "COMPLETED" for run in resumed),
                len(resumed),
            ),
        },
        "tool_queue": {
            "status_distribution": _distribution([job.status for job in tool_jobs]),
            "terminal_completion_rate": _ratio(
                sum(job.status == "COMPLETED" for job in terminal_tool_jobs),
                len(terminal_tool_jobs),
            ),
            "dead_lettered": sum(job.dead_lettered_at is not None for job in tool_jobs),
        },
        "approvals": {
            "status_distribution": _distribution(
                [approval.status for approval in approvals]
            ),
        },
        "artifacts": {
            "type_distribution": _distribution(
                [artifact.artifact_type for artifact in artifacts]
            ),
            "structural_integrity_rate": _ratio(
                structurally_valid_artifacts,
                len(artifacts),
            ),
        },
        "limitations": [
            "This report contains observed operational aggregates, not semantic quality labels.",
            "Approval accuracy and stale-result rejection accuracy require injected fault cases with expected outcomes.",
            "Intent accuracy, tool selection accuracy, RAG HitRate and MRR require a versioned labeled dataset.",
            "Small or zero sample sizes are reported as null rather than treated as perfect scores.",
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a privacy-safe report from observed Assistant database records."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")

    report = await build_report(days=args.days)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    asyncio.run(main())
