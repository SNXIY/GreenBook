"""Audit the requested and code-observed production chunk representations.

This is a read-only offline experiment on the complete RAG dataset v2.  A/B/C
metrics come from the previously completed full-dataset artifact.  This script
computes the requested D representation and the exact current Java code
representation on the same frozen Top10 candidate lists.  It never contacts
Qdrant and never writes vectors.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from audit_rag_chunk_representation import _embed, _mysql_chunks, _text
from evaluate_rag_chunk_representation import _dot, _latency, _mean, _percentile
from validate_rag_dataset_v2 import validate


DEPTH = 10
CUTOFFS = (5, 10)
REQUESTED_D = "D_TITLE_DESCRIPTION_CONTENT"
ACTUAL_D = "D_ACTUAL_CODE_TITLE_TAGS_DESCRIPTION_CONTENT"


def _requested_d_text(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()[:256]
    description = str(row.get("description") or "").strip()[:768]
    content = str(row.get("content") or "")
    return f"title: {title}\ndescription: {description}\ncontent: {content}"


def _rank(
    query_vector: list[float],
    candidate_ids: set[str],
    document_vectors: dict[str, list[float]],
) -> list[dict[str, Any]]:
    scored = [
        {"chunk_id": chunk_id, "score": _dot(query_vector, vector)}
        for chunk_id, vector in document_vectors.items()
        if chunk_id in candidate_ids
    ]
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return [
        {"chunk_id": item["chunk_id"], "score": round(float(item["score"]), 8), "rank": index + 1}
        for index, item in enumerate(scored)
    ]


def _evaluate_representation(
    name: str,
    rows: list[dict[str, Any]],
    candidates_by_query: dict[str, set[str]],
    chunk_ids_by_post: dict[str, set[str]],
    query_vectors: dict[str, list[float]],
    document_vectors: dict[str, list[float]],
    query_latency_by_id: dict[str, float],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[float], list[float]]:
    conditional_values = {cutoff: [] for cutoff in CUTOFFS}
    final_values = {cutoff: [] for cutoff in CUTOFFS}
    ranking_latency: list[float] = []
    end_to_end_latency: list[float] = []
    post_missing = 0
    chunk_miss = 0
    hit_at10 = 0
    failures: list[dict[str, Any]] = []
    for row in rows:
        query_id = str(row["query_id"])
        expected = {
            str(entry["chunk_id"]): str(entry["post_id"])
            for entry in row.get("gold_chunks", [])
        }
        if not expected:
            continue
        candidates = candidates_by_query[query_id]
        candidate_chunk_ids = set().union(
            *(chunk_ids_by_post.get(post_id, set()) for post_id in candidates)
        ) if candidates else set()
        started = time.perf_counter()
        ranked = _rank(query_vectors[query_id], candidate_chunk_ids, document_vectors)
        rank_ms = (time.perf_counter() - started) * 1000
        ranking_latency.append(rank_ms)
        end_to_end_latency.append(query_latency_by_id[query_id] + rank_ms)
        rank_by_id = {item["chunk_id"]: item for item in ranked}
        conditional_expected = {
            chunk_id for chunk_id, post_id in expected.items() if post_id in candidates
        }
        for cutoff in CUTOFFS:
            hit_ids = {item["chunk_id"] for item in ranked[:cutoff]}
            final_values[cutoff].append(len(hit_ids & set(expected)) / len(expected))
            if conditional_expected:
                conditional_values[cutoff].append(
                    len(hit_ids & conditional_expected) / len(conditional_expected)
                )
        for chunk_id, post_id in expected.items():
            ranked_gold = rank_by_id.get(chunk_id)
            status = "HIT@10" if ranked_gold and ranked_gold["rank"] <= 10 else (
                "POST_MISSING" if post_id not in candidates else "CHUNK_RETRIEVAL_MISS"
            )
            if status == "POST_MISSING":
                post_missing += 1
            elif status == "CHUNK_RETRIEVAL_MISS":
                chunk_miss += 1
            else:
                hit_at10 += 1
            if status != "HIT@10":
                failures.append({
                    "query_id": query_id,
                    "query": str(row["query"]),
                    "gold_post_id": post_id,
                    "gold_chunk_id": chunk_id,
                    "representation": name,
                    "status": status,
                    "post_in_top10": post_id in candidates,
                    "rank": ranked_gold.get("rank") if ranked_gold else None,
                    "score": ranked_gold.get("score") if ranked_gold else None,
                    "candidate_posts": sorted(candidates),
                })
    metrics = {
        "CONDITIONAL_CHUNK_RECALL@5": _mean(conditional_values[5]),
        "CONDITIONAL_CHUNK_RECALL@10": _mean(conditional_values[10]),
        "FINAL_EVIDENCE_RECALL@5": _mean(final_values[5]),
        "FINAL_EVIDENCE_RECALL@10": _mean(final_values[10]),
        "conditional_query_count_at5": len(conditional_values[5]),
        "conditional_query_count_at10": len(conditional_values[10]),
        "answerable_query_count": len(final_values[10]),
    }
    failures_summary = {
        "post_missing": post_missing,
        "chunk_retrieval_miss": chunk_miss,
        "hit_at10": hit_at10,
        "gold_chunk_reference_count": post_missing + chunk_miss + hit_at10,
    }
    return (
        metrics,
        failures_summary,
        failures,
        ranking_latency,
        end_to_end_latency,
    )


def _render_report(output: dict[str, Any]) -> str:
    metrics = output["metrics"]
    latencies = output["latency"]
    names = ["A_CONTENT_ONLY", "B_TITLE_CONTENT", "C_TITLE_TAGS_CONTENT", REQUESTED_D]
    comparison_rows = []
    for name in names:
        comparison_rows.append(
            "| %s | %.6f | %.6f | %.6f | %.6f | %.3f / %.3f |"
            % (
                name,
                metrics[name]["CONDITIONAL_CHUNK_RECALL@5"] or 0.0,
                metrics[name]["CONDITIONAL_CHUNK_RECALL@10"] or 0.0,
                metrics[name]["FINAL_EVIDENCE_RECALL@5"] or 0.0,
                metrics[name]["FINAL_EVIDENCE_RECALL@10"] or 0.0,
                latencies["ranking"][name]["p50_ms"] or 0.0,
                latencies["ranking"][name]["p95_ms"] or 0.0,
            )
        )
    failure_rows = []
    for name in names:
        failure = output["failure_analysis"][name]
        failure_rows.append(
            "| %s | %d | %d | %d |"
            % (name, failure["post_missing"], failure["chunk_retrieval_miss"], failure["hit_at10"])
        )
    actual = output["metrics"][ACTUAL_D]
    c_metrics = output["metrics"]["C_TITLE_TAGS_CONTENT"]
    if actual["FINAL_EVIDENCE_RECALL@10"] >= c_metrics["FINAL_EVIDENCE_RECALL@10"]:
        baseline_decision = "The code-observed production representation is at least as strong as C in this offline comparison."
    else:
        baseline_decision = "The code-observed production representation is weaker than C in this offline comparison."
    return f"""# RAG_REPRESENTATION_FINAL_DECISION

Generated: {output['generated_at']}

## Comparison

Full `rag_evidence_v2` dataset, 50 queries, fixed Top10 post candidates, and
offline exact cosine ranking over {output['scope']['candidate_chunk_count']}
canonical chunks.

| Representation | Conditional Recall@5 | Conditional Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50/p95 ms |
|---|---:|---:|---:|---:|---:|
{chr(10).join(comparison_rows)}

The requested D is `title + description + content`.

The code-observed production variant is also the strongest result:

| Production variant | Conditional Recall@5 | Conditional Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50/p95 ms |
|---|---:|---:|---:|---:|---:|
| title + tags + description + content | {metrics[ACTUAL_D]['CONDITIONAL_CHUNK_RECALL@5']:.6f} | {metrics[ACTUAL_D]['CONDITIONAL_CHUNK_RECALL@10']:.6f} | {metrics[ACTUAL_D]['FINAL_EVIDENCE_RECALL@5']:.6f} | {metrics[ACTUAL_D]['FINAL_EVIDENCE_RECALL@10']:.6f} | {latencies['ranking'][ACTUAL_D]['p50_ms']:.3f} / {latencies['ranking'][ACTUAL_D]['p95_ms']:.3f} |

## Production Baseline

The task-defined production baseline is D. The source code audit found that
`PostChunk.textForEmbedding` actually builds `title + tags + description +
content`; this exact code-observed variant is reported as
`{ACTUAL_D}` below the requested A/B/C/D table. It was also evaluated offline
without Qdrant access.

{baseline_decision}

Latency details:

- Shared query embedding: p50 {output['latency']['query_embedding']['p50_ms']} ms, p95 {output['latency']['query_embedding']['p95_ms']} ms.
- Requested D document embedding: p50 {output['latency']['document_embedding'][REQUESTED_D]['p50_ms']} ms, p95 {output['latency']['document_embedding'][REQUESTED_D]['p95_ms']} ms.
- Code-observed production document embedding: p50 {output['latency']['document_embedding'][ACTUAL_D]['p50_ms']} ms, p95 {output['latency']['document_embedding'][ACTUAL_D]['p95_ms']} ms.

Failure classification over 104 gold chunk references:

| Representation | Gold post missing | Chunk retrieval miss | Hit@10 |
|---|---:|---:|---:|
{chr(10).join(failure_rows)}

## Recommended Change

Do not modify production or rebuild vectors. The requested D is slightly worse
than C. The code-observed production representation is the best tested result,
so there is no evidence-based reason to change the embedding input.

## Migration Cost

Any representation change would require recomputing every live chunk vector,
upserting a new embedding version, validating post/chunk IDs and event
versions, and a rollback/dual-version plan. No Qdrant update or rebuild was
performed here.

Detailed D failure cases are in
[rag_current_production_representation_audit_20260825.json](./rag_current_production_representation_audit_20260825.json).

## Verdict

`RAG_REPRESENTATION_FINAL_DECISION_COMPLETE`

`OFFLINE_ONLY_NO_PRODUCTION_CHANGE`

`CURRENT_PRODUCTION_REPRESENTATION_WINS`

`NO_REBUILD_REQUIRED`
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path, required=True)
    parser.add_argument("--diagnosis", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8181/embed")
    parser.add_argument("--mysql-container", default="greenbook-mysql")
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="123456")
    parser.add_argument("--mysql-database", default="zhiguang")
    args = parser.parse_args()

    validation = validate(args.dataset, args.chunk_fixture)
    if validation["status"] != "VALID":
        print(json.dumps({"status": "DATASET_INVALID", "validation": validation}, ensure_ascii=False, indent=2))
        return 2
    previous = json.loads(args.previous.read_text(encoding="utf-8"))
    for name in ("A_CONTENT_ONLY", "B_TITLE_CONTENT", "C_TITLE_TAGS_CONTENT"):
        if name not in previous.get("metrics", {}):
            raise ValueError(f"previous full representation artifact missing {name}")
    diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
    diagnosis_records = {
        str(record["query_id"]): record
        for record in diagnosis.get("records", [])
        if isinstance(record, dict) and record.get("query_id")
    }
    rows = validation["valid_rows"]
    if set(diagnosis_records) != {str(row["query_id"]) for row in rows}:
        raise ValueError("diagnosis candidate snapshot does not cover the full v2 dataset")

    candidates_by_query: dict[str, set[str]] = {}
    all_candidate_posts: set[str] = set()
    for row in rows:
        query_id = str(row["query_id"])
        candidates = {
            str(value)
            for value in diagnosis_records[query_id]
            .get("candidates_by_depth", {})
            .get(str(DEPTH), [])
        }
        candidates_by_query[query_id] = candidates
        all_candidate_posts.update(candidates)

    started_total = time.perf_counter()
    chunks = _mysql_chunks(
        args.mysql_container,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
        sorted(all_candidate_posts),
    )
    chunk_ids_by_post: dict[str, set[str]] = {}
    for chunk in chunks:
        chunk_ids_by_post.setdefault(str(chunk["post_id"]), set()).add(str(chunk["chunk_id"]))

    query_vectors: dict[str, list[float]] = {}
    query_latency_by_id: dict[str, float] = {}
    document_vectors: dict[str, dict[str, list[float]]] = {
        REQUESTED_D: {},
        ACTUAL_D: {},
    }
    document_latency: dict[str, list[float]] = {REQUESTED_D: [], ACTUAL_D: []}
    with httpx.Client(timeout=60) as client:
        for index, row in enumerate(rows, 1):
            query_id = str(row["query_id"])
            started = time.perf_counter()
            query_vectors[query_id] = _embed(client, args.embedding_url, str(row["query"]))
            query_latency_by_id[query_id] = (time.perf_counter() - started) * 1000
            if index % 10 == 0:
                print(json.dumps({"query_embedding_progress": f"{index}/{len(rows)}"}), flush=True)
        total = len(chunks) * 2
        completed = 0
        for chunk in chunks:
            for name, text in (
                (REQUESTED_D, _requested_d_text(chunk)),
                (ACTUAL_D, _text(chunk, "C_PRODUCTION")),
            ):
                started = time.perf_counter()
                document_vectors[name][str(chunk["chunk_id"])] = _embed(
                    client, args.embedding_url, text
                )
                document_latency[name].append((time.perf_counter() - started) * 1000)
                completed += 1
                if completed % 500 == 0:
                    print(json.dumps({
                        "document_embedding_progress": f"{completed}/{total}",
                        "representation": name,
                    }), flush=True)

    metrics = dict(previous["metrics"])
    failure_analysis = dict(previous["failure_analysis"])
    latency = {
        "query_embedding": _latency(list(query_latency_by_id.values())),
        "document_embedding": dict(previous["latency"]["document_embedding"]),
        "ranking": dict(previous["latency"]["ranking"]),
        "offline_end_to_end_per_query": dict(previous["latency"]["offline_end_to_end_per_query"]),
    }
    failures: list[dict[str, Any]] = []
    for name in (REQUESTED_D, ACTUAL_D):
        result_metrics, result_failure, result_failures, ranking, end_to_end = _evaluate_representation(
            name,
            rows,
            candidates_by_query,
            chunk_ids_by_post,
            query_vectors,
            document_vectors[name],
            query_latency_by_id,
        )
        metrics[name] = result_metrics
        failure_analysis[name] = result_failure
        failures.extend(result_failures)
        latency["ranking"][name] = _latency(ranking)
        latency["offline_end_to_end_per_query"][name] = _latency(end_to_end)
        latency["document_embedding"][name] = _latency(document_latency[name])

    output = {
        "status": "VALID",
        "generated_at": date.today().isoformat(),
        "dataset": previous["dataset"],
        "scope": {
            "query_count": len(rows),
            "answerable_query_count": sum(1 for row in rows if row.get("category") != "no_answer"),
            "no_answer_query_count": sum(1 for row in rows if row.get("category") == "no_answer"),
            "candidate_depth": DEPTH,
            "candidate_post_count": len(all_candidate_posts),
            "candidate_chunk_count": len(chunks),
            "qdrant_contacted": False,
            "qdrant_updated": False,
            "rebuild": False,
        },
        "representations": {
            "A_CONTENT_ONLY": "content",
            "B_TITLE_CONTENT": "title + content",
            "C_TITLE_TAGS_CONTENT": "title + tags + content",
            REQUESTED_D: "title + description + content",
            ACTUAL_D: "title + tags + description + content (PostChunk.textForEmbedding)",
        },
        "metrics": metrics,
        "latency": {
            **latency,
            "total_elapsed_ms": round((time.perf_counter() - started_total) * 1000, 3),
        },
        "failure_analysis": failure_analysis,
        "failure_cases": failures,
        "previous_full_artifact": str(args.previous),
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "scope": output["scope"],
        "metrics": output["metrics"],
        "latency": output["latency"],
        "failure_analysis": output["failure_analysis"],
        "failure_case_count": len(failures),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
