"""Evaluation of Artifact producer/consumer, schema, and lifecycle facts."""

from __future__ import annotations

from typing import Any


def compare_artifacts(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    by_id = {str(item.get("artifact_id")): item for item in actual}
    for item in expected:
        artifact_id = str(item.get("artifact_id", ""))
        candidate = by_id.get(artifact_id) if artifact_id else None
        if candidate is None and item.get("artifact_type"):
            candidate = next(
                (value for value in actual if value.get("artifact_type") == item["artifact_type"]),
                None,
            )
        checks.append({
            "metric": "artifact_resolution_accuracy",
            "expected": item,
            "actual": candidate or {},
            "ok": bool(candidate) and all(
                candidate.get(field) == value
                for field, value in item.items()
                if field != "artifact_id"
            ),
        })
    return checks
