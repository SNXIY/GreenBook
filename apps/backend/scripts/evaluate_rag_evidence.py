"""Evaluate evidence retrieval and citation structure on a frozen RAG set."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import uuid
from pathlib import Path
from typing import Any

from validate_rag_dataset import validate

INSUFFICIENT = "当前社区资料不足"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def stable_chunk_id(post_id: str, chunk_index: int) -> str:
    """Java UUID.nameUUIDFromBytes equivalent for PostChunker stable IDs."""
    digest = bytearray(hashlib.md5(f"greenbook:post-chunk:{post_id}:{chunk_index}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def load_runs(paths: list[Path]) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            provider = str(row["provider"])
            grouped.setdefault(provider, {})[str(row["query_id"])] = row
    return grouped


def _evidence_ids(run: dict[str, Any], cutoff: int) -> list[str]:
    raw = run.get("evidence", run.get("results", []))
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw[:cutoff]:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("chunk_id", item.get("chunkId"))
            if value:
                result.append(str(value))
    return result


def _latencies(run: dict[str, Any], name: str) -> list[float]:
    raw = run.get("latencies_ms", {})
    if isinstance(raw, dict):
        raw = raw.get(name, [])
    elif name != "total":
        return []
    if isinstance(raw, (int, float)):
        raw = [raw]
    return [float(value) for value in raw if isinstance(value, (int, float))]


def _labeled_metric(runs: list[dict[str, Any]], name: str) -> float | None:
    values = [float(run[name]) for run in runs if isinstance(run.get(name), (int, float))]
    return round(statistics.fmean(values), 6) if values else None


def evaluate_provider(provider_runs: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    recall_rows: list[dict[str, float]] = []
    latencies = {name: [] for name in ("chunk_retrieval", "embedding", "generation", "total")}
    miss_count = 0
    expected_count = 0
    no_answer_count = 0
    no_answer_false_positive = 0
    citation_checks = 0
    citation_correct = 0
    structural_faithful = 0
    generation_observed = 0
    generation_rows: list[dict[str, Any]] = []

    for row in rows:
        query_id = str(row["query_id"])
        expected = {
            stable_chunk_id(str(entry["post_id"]), int(entry["chunk_index"]))
            for entry in row.get("gold_chunks", [])
        }
        run = provider_runs.get(query_id, {})
        evidence5 = _evidence_ids(run, 5)
        evidence10 = _evidence_ids(run, 10)
        if expected:
            expected_count += len(expected)
            hits5 = len(set(evidence5) & expected)
            hits10 = len(set(evidence10) & expected)
            miss_count += len(expected - set(evidence10))
            recall_rows.append({
                "recall5": hits5 / len(expected),
                "recall10": hits10 / len(expected),
            })
        elif row.get("category") == "no_answer":
            no_answer_count += 1
            no_answer_false_positive += len(evidence10)

        for name in latencies:
            latencies[name].extend(_latencies(run, name))

        sources = run.get("sources", [])
        if not isinstance(sources, list):
            sources = []
        evidence_by_id = {
            str(item.get("chunk_id", item.get("chunkId"))): item
            for item in run.get("evidence", [])
            if isinstance(item, dict) and item.get("chunk_id", item.get("chunkId"))
        }
        if sources:
            citation_checks += len(sources)
            for source in sources:
                if not isinstance(source, dict):
                    continue
                chunk_id = str(source.get("chunk_id", source.get("chunkId", "")))
                evidence_item = evidence_by_id.get(chunk_id)
                same_post = evidence_item is not None and str(source.get("post_id", source.get("postId", ""))) == str(
                    evidence_item.get("post_id", evidence_item.get("postId", ""))
                )
                if chunk_id and same_post:
                    citation_correct += 1

        if "answer" in run:
            generation_observed += 1
            answer = str(run.get("answer") or "").strip()
            if answer == INSUFFICIENT or not answer:
                structural_faithful += 1 if not sources else 0
            elif sources and all(
                str(source.get("chunk_id", source.get("chunkId", ""))) in evidence_by_id
                for source in sources if isinstance(source, dict)
            ):
                structural_faithful += 1
        if "faithfulness" in run or "citation_correctness" in run:
            generation_rows.append(run)

    quality = {
        metric: round(statistics.fmean(row[metric] for row in recall_rows), 6)
        if recall_rows else None
        for metric in ("recall5", "recall10")
    }
    quality.update({
        "evidence_expected_chunk_count": expected_count,
        "retrieval_miss_count_at10": miss_count,
        "RETRIEVAL_MISS": miss_count,
        "no_answer_query_count": no_answer_count,
        "no_answer_false_positive_at10": no_answer_false_positive,
    })
    citation = {
        "citation_correctness_structural": round(citation_correct / citation_checks, 6)
        if citation_checks else None,
        "citation_checks": citation_checks,
        "faithfulness_structural_proxy": round(structural_faithful / generation_observed, 6)
        if generation_observed else None,
        "faithfulness_labeled": _labeled_metric(generation_rows, "faithfulness"),
        "citation_correctness_labeled": _labeled_metric(generation_rows, "citation_correctness"),
        "generation_labeled_query_count": len(generation_rows),
        "generation_observed_query_count": generation_observed,
    }
    return {
        "quality": quality,
        "citation": citation,
        "latency_ms": {
            name: {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            for name, values in latencies.items()
        },
        "run_missing_query_count": sum(1 for row in rows if str(row["query_id"]) not in provider_runs),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path)
    parser.add_argument("--runs", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--allow-invalid", action="store_true")
    args = parser.parse_args()
    report = validate(args.dataset, args.fixture, args.chunk_fixture)
    if report["status"] != "VALID" and not args.allow_invalid:
        print(json.dumps({"status": "DATASET_INVALID", **{
            key: report[key] for key in (
                "VALID_QUERY_COUNT", "VALID_QREL_COUNT", "REMOVED_INVALID_COUNT",
                "MISSING_FIXTURE_COUNT", "MISSING_CHUNK_FIXTURE_COUNT",
            )
        }}, ensure_ascii=False, indent=2))
        return 2
    rows = report["valid_rows"]
    runs = load_runs(args.runs)
    output = {
        "status": "VALID" if report["status"] == "VALID" else "DATASET_INVALID",
        "dataset": {key: report[key] for key in (
            "VALID_QUERY_COUNT", "VALID_QREL_COUNT", "REMOVED_INVALID_COUNT",
            "MISSING_FIXTURE_COUNT", "MISSING_CHUNK_FIXTURE_COUNT",
            "NON_SEARCHABLE_QREL_COUNT",
        )},
        "providers": {
            provider: evaluate_provider(provider_runs, rows)
            for provider, provider_runs in runs.items()
        },
    }
    if args.report:
        args.report.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
