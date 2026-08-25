"""Tune bounded BM25 + Dense RRF candidates on one clean benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluate_retrieval_quality import clean_qrels, evaluate_provider, load_runs
from validate_retrieval_dataset import validate


def fuse(bm25: list[str], dense: list[str], bm25_top: int, dense_top: int,
         rrf_k: int) -> list[str]:
    scores: dict[str, float] = {}
    for rank, post_id in enumerate(bm25[:bm25_top], 1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, post_id in enumerate(dense[:dense_top], 1):
        scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores, key=lambda post_id: (-scores[post_id], int(post_id)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs", type=Path, action="append", required=True)
    parser.add_argument("--bm25-top", type=int, action="append", default=[])
    parser.add_argument("--dense-top", type=int, action="append", default=[])
    parser.add_argument("--rrf-k", type=int, action="append", default=[])
    args = parser.parse_args()

    report = validate(args.dataset, args.fixture)
    if report["status"] != "VALID":
        print(json.dumps({"status": "DATASET_INVALID"}, ensure_ascii=False))
        return 2
    qrels, rows = clean_qrels(report)
    runs = load_runs(args.runs)
    if "bm25" not in runs or "dense" not in runs:
        raise SystemExit("RRF requires bm25 and dense run files")
    bm25_tops = args.bm25_top or [20, 50, 100]
    dense_tops = args.dense_top or [20, 50, 100]
    rrf_ks = args.rrf_k or [20, 60, 100]
    query_ids = list(rows)
    results = []
    for bm25_top in bm25_tops:
        for dense_top in dense_tops:
            for rrf_k in rrf_ks:
                provider_runs = {}
                for query_id in query_ids:
                    provider_runs[query_id] = {
                        "results": fuse(
                            runs["bm25"].get(query_id, {}).get("results", []),
                            runs["dense"].get(query_id, {}).get("results", []),
                            bm25_top, dense_top, rrf_k,
                        )
                    }
                metrics = evaluate_provider(provider_runs, qrels, query_ids)
                results.append({
                    "bm25_top_n": bm25_top,
                    "dense_top_n": dense_top,
                    "rrf_k": rrf_k,
                    **{key: metrics[key] for key in
                       ("recall5", "recall10", "precision5", "mrr10", "ndcg10")},
                })
    results.sort(key=lambda row: (
        -(row["ndcg10"] or 0), -(row["recall10"] or 0), -(row["mrr10"] or 0)))
    print(json.dumps({
        "status": "VALID",
        "dataset": {key: report[key] for key in (
            "VALID_QUERY_COUNT", "VALID_QREL_COUNT", "SEARCHABLE_QREL_COUNT",
            "REMOVED_INVALID_COUNT", "MISSING_FIXTURE_COUNT")},
        "best": results[0] if results else None,
        "candidates": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
