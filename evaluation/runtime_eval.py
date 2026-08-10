"""Dataset evaluator for real Assistant Runtime observations.

The evaluator never creates fake Java/Creator results. It compares a JSONL
expectation with observations collected from live API/Worker/DB runs.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from .artifact_eval import compare_artifacts
from .task_graph_eval import compare_task_graph
from .tool_eval import recovery_metrics, tool_metrics


class EvaluationReport(dict[str, Any]):
    """JSON-serialisable report with named accuracy metrics and badcases."""


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(value)
    return rows


def evaluate_cases(
    cases: Iterable[dict[str, Any]],
    observations: dict[str, dict[str, Any]] | None = None,
    *,
    blocked_reason: str | None = None,
) -> EvaluationReport:
    observations = observations or {}
    case_reports: list[dict[str, Any]] = []
    badcases: list[dict[str, Any]] = []
    metric_checks: dict[str, list[bool]] = {}
    for case in cases:
        case_id = str(case.get("case_id", case.get("input", "case")))
        actual = observations.get(case_id, {})
        checks = compare_task_graph(case, actual)
        checks.extend(compare_artifacts(case.get("expected_artifacts", []), actual.get("artifacts", [])))
        side_effect_expected = case.get("expected_side_effects")
        if side_effect_expected is not None:
            checks.append({
                "metric": "runtime_success_rate",
                "field": "side_effects",
                "expected": side_effect_expected,
                "actual": actual.get("side_effects", []),
                "ok": actual.get("side_effects", []) == side_effect_expected,
            })
        if not actual and blocked_reason:
            checks.append({
                "metric": "runtime_success_rate",
                "field": "environment",
                "expected": "LIVE_OBSERVATION",
                "actual": "BLOCKED_BY_ENV",
                "ok": False,
            })
        for check in checks:
            metric_checks.setdefault(check["metric"], []).append(bool(check["ok"]))
        passed = bool(checks) and all(bool(check["ok"]) for check in checks)
        report = {"case_id": case_id, "passed": passed, "checks": checks}
        if not passed:
            report["badcase"] = blocked_reason or "EXPECTATION_MISMATCH"
            badcases.append(report)
        case_reports.append(report)

    metrics = {
        name: sum(values) / len(values) if values else 0.0
        for name, values in metric_checks.items()
    }
    return EvaluationReport(
        run_id=f"agent-eval-{uuid.uuid4()}",
        status="BLOCKED_BY_ENV" if blocked_reason else "COMPLETED",
        blocked_reason=blocked_reason,
        metrics=metrics,
        badcases=badcases,
        cases=case_reports,
    )


def merge_runtime_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {**tool_metrics(observations), **recovery_metrics(observations)}
