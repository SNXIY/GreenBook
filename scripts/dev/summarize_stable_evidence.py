"""Read-only summary for browser JSONL evidence."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / ".runtime" / "stable-baseline"
WRITE_ACTIONS = {
    "GENERATE_CONTENT",
    "CREATE_DRAFT",
    "MANAGE_DRAFT",
    "UPDATE_DRAFT",
    "CREATE_SCHEDULE",
    "SCHEDULE_PUBLISH",
    "UPDATE_SCHEDULE",
    "MANAGE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
    "DELETE_DRAFT",
    "DELETE_POST",
}


def rows() -> list[dict]:
    result = []
    for path in sorted(ROOT.glob("round-1-GB-STABLE-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item["_file"] = path.name
            result.append(item)
    return result


def p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95)))
    return round(ordered[index], 3)


def main() -> None:
    data = rows()
    status = Counter(str(item.get("status") or "") for item in data)
    elapsed = [
        float(item["elapsed_seconds"])
        for item in data
        if isinstance(item.get("elapsed_seconds"), (int, float))
    ]
    capability_elapsed: dict[str, list[float]] = defaultdict(list)
    physical_writes = Counter()
    for item in data:
        run = item.get("run") or {}
        caps = {
            str(step.get("capability") or "").upper()
            for step in (run.get("steps") or [])
            if str(step.get("status") or "").upper() == "COMPLETED"
        }
        for capability in caps:
            if capability in WRITE_ACTIONS:
                physical_writes[capability] += 1
            capability_elapsed[capability].append(float(item.get("elapsed_seconds") or 0))
    hitl_clicks = sum(len(item.get("clicked_hitl") or []) for item in data)
    ui_leaks = sum(1 for item in data if item.get("ui_internal_leak"))
    harness_errors = sum(1 for item in data if item.get("status") == "HARNESS_ERROR")
    print(
        json.dumps(
            {
                "files": len({item["_file"] for item in data}),
                "turns": len(data),
                "status": dict(status),
                "elapsed_seconds": {
                    "count": len(elapsed),
                    "mean": round(statistics.mean(elapsed), 3) if elapsed else None,
                    "p50": p50(elapsed),
                    "p95": p95(elapsed),
                    "max": round(max(elapsed), 3) if elapsed else None,
                },
                "physical_write_step_counts": dict(physical_writes),
                "hitl_clicks": hitl_clicks,
                "ui_internal_leak_count": ui_leaks,
                "harness_error_count": harness_errors,
                "elapsed_by_capability": {
                    key: {
                        "count": len(values),
                        "p50": p50(values),
                        "p95": p95(values),
                        "max": round(max(values), 3),
                    }
                    for key, values in sorted(capability_elapsed.items())
                },
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
