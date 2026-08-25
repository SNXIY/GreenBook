"""Evaluate MySQL/BM25/Dense/RRF runs against the 100-query qrels fixture.

The script intentionally consumes exported run files; it never mutates MySQL,
Elasticsearch or Qdrant and does not build embeddings.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    return {
        row["query_id"]: {str(k): int(v) for k, v in row.get("qrels", {}).items()}
        for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if row.get("query_id")
    }


def ndcg(results: list[str], qrel: dict[str, int], cutoff: int) -> float:
    ranked = results[:cutoff]
    dcg = sum((2 ** qrel.get(post_id, 0) - 1) / math.log2(rank + 2)
              for rank, post_id in enumerate(ranked))
    ideal = sorted(qrel.values(), reverse=True)[:cutoff]
    idcg = sum((2 ** grade - 1) / math.log2(rank + 2)
               for rank, grade in enumerate(ideal))
    return dcg / idcg if idcg else 1.0


def evaluate(qrels: dict[str, dict[str, int]], runs: list[dict]) -> dict:
    grouped: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for run in runs:
        grouped[str(run["provider"])][str(run["query_id"])] = [
            str(value) for value in run.get("results", [])
        ]
    output = {}
    for provider, provider_runs in grouped.items():
        rows = []
        for query_id, qrel in qrels.items():
            results = provider_runs.get(query_id, [])
            relevant = {post_id for post_id, grade in qrel.items() if grade > 0}
            top5 = results[:5]
            top10 = results[:10]
            first = next((index + 1 for index, post_id in enumerate(top10)
                          if post_id in relevant), None)
            rows.append({
                "recall5": len(set(top5) & relevant) / len(relevant) if relevant else 1.0,
                "recall10": len(set(top10) & relevant) / len(relevant) if relevant else 1.0,
                "precision5": len(set(top5) & relevant) / 5,
                "mrr10": 1 / first if first else 0.0,
                "ndcg10": ndcg(results, qrel, 10),
            })
        output[provider] = {
            metric: round(sum(row[metric] for row in rows) / len(rows), 6)
            for metric in rows[0]
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()
    runs = [json.loads(line) for line in args.runs.read_text(encoding="utf-8").splitlines() if line]
    print(json.dumps(evaluate(load_qrels(args.qrels), runs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
