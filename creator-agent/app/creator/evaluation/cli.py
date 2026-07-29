from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import get_settings
from app.creator.evaluation.composition import build_creator_evaluation_pipeline
from app.creator.evaluation.dataset import (
    load_evaluation_dataset,
    load_evaluation_observations,
)
from app.creator.evaluation.models import (
    EvaluationMode,
    EvaluationObservationSet,
    EvaluationRunReport,
    EvaluationSnapshotRequest,
)
from app.creator.infrastructure.database import CreatorDatabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the MindFlow Creator evaluation pipeline."
    )
    parser.add_argument("--dataset")
    parser.add_argument("--observations")
    parser.add_argument("--output")
    parser.add_argument("--evaluation-run-id")
    parser.add_argument("--candidate-name")
    parser.add_argument("--candidate-version")
    parser.add_argument("--baseline-report")
    parser.add_argument("--baseline-evaluation-run-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--actor-id", default="creator-eval-cli")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in EvaluationMode],
        default=EvaluationMode.OFFLINE_REGRESSION.value,
    )
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--create-schema", action="store_true")
    parser.add_argument("--case-id")
    parser.add_argument("--creator-id")
    parser.add_argument("--task-id")
    parser.add_argument("--run-id")
    parser.add_argument("--fail-on-threshold", action="store_true")
    return parser


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    dataset_path = args.dataset or settings.creator_evaluation_dataset_path
    output_path = args.output or settings.creator_evaluation_output_path
    dataset = load_evaluation_dataset(dataset_path)
    database = None
    pipeline = None
    use_snapshot = bool(args.run_id or args.task_id)
    if use_snapshot or args.persist or args.baseline_evaluation_run_id:
        database = CreatorDatabase.from_settings(settings)
        if args.create_schema:
            await database.create_schema_for_development()

    try:
        if use_snapshot:
            required = {
                "--case-id": args.case_id,
                "--tenant-id": args.tenant_id,
                "--creator-id": args.creator_id,
                "--task-id": args.task_id,
                "--run-id": args.run_id,
            }
            missing = [flag for flag, value in required.items() if not value]
            if missing:
                raise ValueError("Snapshot evaluation requires " + ", ".join(missing))
            matching = tuple(case for case in dataset.cases if case.id == args.case_id)
            if len(matching) != 1:
                raise ValueError(f"Dataset does not contain case {args.case_id!r}")
            dataset = dataset.model_copy(update={"cases": matching})
            assert database is not None
            observation = await database.evaluation_snapshot_reader.capture(
                EvaluationSnapshotRequest(
                    case_id=args.case_id,
                    tenant_id=args.tenant_id,
                    creator_id=args.creator_id,
                    task_id=args.task_id,
                    run_id=args.run_id,
                )
            )
            observations = EvaluationObservationSet(
                dataset_id=dataset.id,
                dataset_version=dataset.version,
                observations=(observation,),
            )
            tenant_id = args.tenant_id
        else:
            observations_path = (
                args.observations or settings.creator_evaluation_observations_path
            )
            observations = load_evaluation_observations(observations_path)
            tenant_ids = {
                observation.tenant_id for observation in observations.observations
            }
            if args.tenant_id:
                tenant_id = args.tenant_id
            elif len(tenant_ids) == 1:
                tenant_id = next(iter(tenant_ids))
            else:
                raise ValueError(
                    "--tenant-id is required for a multi-tenant observation file"
                )

        if args.baseline_report and args.baseline_evaluation_run_id:
            raise ValueError(
                "Use either --baseline-report or --baseline-evaluation-run-id"
            )
        baseline = None
        if args.baseline_report:
            baseline = EvaluationRunReport.model_validate(
                json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
            )
        elif args.baseline_evaluation_run_id:
            assert database is not None
            baseline = await database.evaluation_store.get(
                args.baseline_evaluation_run_id
            )
            if baseline is None:
                raise ValueError(
                    "Baseline evaluation run was not found: "
                    f"{args.baseline_evaluation_run_id}"
                )

        pipeline = build_creator_evaluation_pipeline(
            settings,
            store=database.evaluation_store if database is not None else None,
        )
        result = await pipeline.evaluate(
            dataset,
            observations,
            tenant_id=tenant_id,
            actor_id=args.actor_id,
            candidate_name=(
                args.candidate_name or settings.creator_evaluation_candidate_name
            ),
            candidate_version=(
                args.candidate_version or settings.creator_evaluation_candidate_version
            ),
            mode=EvaluationMode(args.mode),
            evaluation_run_id=args.evaluation_run_id,
            baseline=baseline,
            persist=args.persist,
            metadata={"command": "app.creator.evaluation.cli"},
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                result.report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "evaluation_run_id": result.report.id,
                    "outcome": result.report.outcome.value,
                    "passed": result.report.passed,
                    "overall_score": result.report.overall_score,
                    "candidate_name": result.report.candidate_name,
                    "candidate_version": result.report.candidate_version,
                    "metric_deltas": result.report.metric_deltas,
                    "cases": len(result.report.cases),
                    "replayed": result.replayed,
                    "output": str(output),
                },
                ensure_ascii=False,
            )
        )
        return 2 if args.fail_on_threshold and not result.report.passed else 0
    finally:
        if pipeline is not None:
            await pipeline.aclose()
        if database is not None:
            await database.dispose()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"creator evaluation failed: {exc}\n")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
