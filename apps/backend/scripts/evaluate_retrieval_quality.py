"""Evaluate clean retrieval runs without conflating fixture and provider misses."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from validate_retrieval_dataset import validate


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def ndcg(results: list[str], qrel: dict[str, int], cutoff: int) -> float:
    ranked = results[:cutoff]
    dcg = sum((2 ** qrel.get(post_id, 0) - 1) / math.log2(rank + 2)
              for rank, post_id in enumerate(ranked))
    ideal = sorted(qrel.values(), reverse=True)[:cutoff]
    idcg = sum((2 ** grade - 1) / math.log2(rank + 2)
               for rank, grade in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def load_runs(paths: list[Path]) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            provider = str(row["provider"])
            query_id = str(row["query_id"])
            results = row.get("results", [])
            if isinstance(results, list):
                row["results"] = [str(value) for value in results]
            else:
                row["results"] = []
            grouped.setdefault(provider, {})[query_id] = row
    return grouped


def clean_qrels(report: dict[str, Any]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, Any]]]:
    qrels: dict[str, dict[str, int]] = {}
    rows: dict[str, dict[str, Any]] = {}
    non_searchable = {(str(item["query_id"]), str(item["post_id"]))
                      for item in report["non_searchable_qrels"]}
    for row in report["valid_rows"]:
        query_id = str(row["query_id"])
        qrels[query_id] = {
            str(post_id): int(grade)
            for post_id, grade in row.get("qrels", {}).items()
            if (query_id, str(post_id)) not in non_searchable
        }
        rows[query_id] = row
    return qrels, rows


def evaluate_provider(provider_runs: dict[str, dict[str, Any]],
                      qrels: dict[str, dict[str, int]],
                      all_query_ids: list[str]) -> dict[str, Any]:
    quality_rows: list[dict[str, float]] = []
    latencies: list[float] = []
    run_missing = 0
    retrieval_miss_pairs = 0
    quality_pair_count = 0
    no_result_false_positive = 0
    no_result_query_count = 0

    for query_id in all_query_ids:
        qrel = qrels.get(query_id, {})
        relevant = {post_id for post_id, grade in qrel.items() if grade > 0}
        run = provider_runs.get(query_id)
        if run is None:
            run_missing += 1
            results: list[str] = []
        else:
            results = run.get("results", [])
            raw_latencies = run.get("latencies_ms", run.get("latency_ms", []))
            if isinstance(raw_latencies, (int, float)):
                raw_latencies = [raw_latencies]
            if isinstance(raw_latencies, list):
                latencies.extend(float(value) for value in raw_latencies
                                 if isinstance(value, (int, float)))

        if relevant:
            quality_pair_count += len(relevant)
            top5 = results[:5]
            top10 = results[:10]
            hits5 = len(set(top5) & relevant)
            hits10 = len(set(top10) & relevant)
            retrieval_miss_pairs += len(relevant - set(top10))
            first = next((index + 1 for index, post_id in enumerate(top10)
                          if post_id in relevant), None)
            quality_rows.append({
                "recall5": hits5 / len(relevant),
                "recall10": hits10 / len(relevant),
                "precision5": hits5 / 5,
                "mrr10": 1 / first if first else 0.0,
                "ndcg10": ndcg(results, qrel, 10),
            })
        elif not relevant and qrel == {}:
            no_result_query_count += 1
            no_result_false_positive += len(results[:10])

    metrics = {
        metric: round(statistics.fmean(row[metric] for row in quality_rows), 6)
        for metric in ("recall5", "recall10", "precision5", "mrr10", "ndcg10")
    } if quality_rows else {metric: None for metric in
                            ("recall5", "recall10", "precision5", "mrr10", "ndcg10")}
    metrics.update({
        "quality_query_count": len(quality_rows),
        "quality_qrel_pair_count": quality_pair_count,
        "retrieval_miss_pairs_at10": retrieval_miss_pairs,
        "run_missing_query_count": run_missing,
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "no_result_query_count": no_result_query_count,
        "no_result_false_positive_at10": no_result_false_positive,
    })
    return metrics


def complementary_recall(runs: dict[str, dict[str, dict[str, Any]]],
                          qrels: dict[str, dict[str, int]]) -> dict[str, int] | None:
    if "bm25" not in runs or "dense" not in runs:
        return None
    bm25 = runs["bm25"]
    dense = runs["dense"]
    counts = {"BM25_ONLY_HITS": 0, "DENSE_ONLY_HITS": 0,
              "OVERLAP": 0, "BOTH_MISS": 0}
    for query_id, qrel in qrels.items():
        relevant = {post_id for post_id, grade in qrel.items() if grade > 0}
        bm25_hits = set(bm25.get(query_id, {}).get("results", [])[:10]) & relevant
        dense_hits = set(dense.get(query_id, {}).get("results", [])[:10]) & relevant
        counts["BM25_ONLY_HITS"] += len(bm25_hits - dense_hits)
        counts["DENSE_ONLY_HITS"] += len(dense_hits - bm25_hits)
        counts["OVERLAP"] += len(bm25_hits & dense_hits)
        counts["BOTH_MISS"] += len(relevant - bm25_hits - dense_hits)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs", type=Path, action="append", required=True)
    parser.add_argument("--allow-invalid", action="store_true")
    args = parser.parse_args()

    report = validate(args.dataset, args.fixture)
    if report["status"] != "VALID" and not args.allow_invalid:
        print(json.dumps({"status": "DATASET_INVALID", **{
            key: report[key] for key in ("VALID_QUERY_COUNT", "VALID_QREL_COUNT",
                                         "REMOVED_INVALID_COUNT", "MISSING_FIXTURE_COUNT")}},
                         ensure_ascii=False, indent=2))
        return 2
    qrels, rows = clean_qrels(report)
    runs = load_runs(args.runs)
    all_query_ids = list(rows)
    output = {
        "status": "VALID" if report["status"] == "VALID" else "DATASET_INVALID",
        "dataset": {key: report[key] for key in (
            "VALID_QUERY_COUNT", "VALID_QREL_COUNT", "SEARCHABLE_QREL_COUNT",
            "REMOVED_INVALID_COUNT", "MISSING_FIXTURE_COUNT",
            "NON_SEARCHABLE_QREL_COUNT")},
        "providers": {
            provider: evaluate_provider(provider_runs, qrels, all_query_ids)
            for provider, provider_runs in runs.items()
        },
        "complementary_recall": complementary_recall(runs, qrels),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
