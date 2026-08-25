"""Evaluate A/B/C chunk representations on the complete RAG evidence v2 set.

This is an offline experiment only.  The post candidate lists are frozen from
the existing Top10 chunk-retrieval diagnosis, so Hybrid Search is not rerun or
changed.  Canonical chunks are read from MySQL and document vectors are
computed through the existing embedding sidecar.  Qdrant is not contacted and
no vector is written or rebuilt.
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

from audit_rag_chunk_representation import (
    REPRESENTATIONS,
    _dot,
    _embed,
    _mysql_chunks,
    _text,
)
from validate_rag_dataset_v2 import validate


DEPTH = 10
CUTOFFS = (5, 10)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


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


def _latency(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "total_ms": round(sum(values), 3),
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
    }


def _render_report(output: dict[str, Any]) -> str:
    metrics = output["metrics"]
    latency = output["latency"]
    rows: list[str] = []
    for name in REPRESENTATIONS:
        value = metrics[name]
        rows.append(
            "| %s | %.6f | %.6f | %.6f | %.6f | %.3f | %.3f |"
            % (
                name,
                value["CONDITIONAL_CHUNK_RECALL@5"] or 0.0,
                value["CONDITIONAL_CHUNK_RECALL@10"] or 0.0,
                value["FINAL_EVIDENCE_RECALL@5"] or 0.0,
                value["FINAL_EVIDENCE_RECALL@10"] or 0.0,
                latency["ranking"][name]["p50_ms"] or 0.0,
                latency["ranking"][name]["p95_ms"] or 0.0,
            )
        )
    failure_rows = []
    for name in REPRESENTATIONS:
        value = output["failure_analysis"][name]
        failure_rows.append(
            "| %s | %d | %d | %d |"
            % (name, value["post_missing"], value["chunk_retrieval_miss"], value["hit_at10"])
        )
    return f"""# RAG_CHUNK_REPRESENTATION_REPORT

Generated: {output['generated_at']}

## Experiment Setup

- Dataset: `rag_evidence_v2`, all {output['scope']['query_count']} queries.
- Answerable queries: {output['scope']['answerable_query_count']}; no-answer queries: {output['scope']['no_answer_query_count']}.
- Post candidates: frozen Top10 lists from the completed chunk-retrieval diagnosis; no Search rerun.
- Candidate posts: {output['scope']['candidate_post_count']} unique posts.
- Canonical chunks: {output['scope']['candidate_chunk_count']} public/published MySQL chunks.
- Ranking: offline exact cosine over chunks belonging to each query's frozen candidate posts.
- A: content only; B: title + content; C: title + tags + content.
- Query/document encoder: existing multilingual embedding sidecar, 384 dimensions, normalized vectors.
- Qdrant: not contacted. No Qdrant update, rebuild, production code, chunking, model, or prompt change.

## Metrics

| Representation | Conditional Chunk Recall@5 | Conditional Chunk Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50 ms | Rank p95 ms |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

Conditional metrics count only gold chunks whose gold post is present in the
fixed Top10 candidate list. Final evidence metrics count all gold chunks, so
post-retrieval misses remain visible. Deterministic evidence selection is
modeled as the same ranked top-k list; the prior live diagnosis measured zero
selection loss.

## Comparison

B is compared on the complete v2 query set, not only the earlier 20-case
diagnostic subset. The representation ranking is an offline controlled
experiment; it does not imply a production embedding change by itself.

## Failure Analysis

| Representation | Gold post missing | Chunk retrieval miss with post present | Gold chunks hit@10 |
|---|---:|---:|---:|
{chr(10).join(failure_rows)}

Detailed per-query and per-gold-chunk ranks are in
[rag_chunk_representation_full_20260825.json](./rag_chunk_representation_full_20260825.json).

## Recommendation

Do not change production yet. If B is materially better on both conditional
and final recall, the smallest candidate production proposal is to change only
the document text representation used when projecting chunk embeddings to
`title + content`, while keeping the current model, 384 dimensions,
normalization contract, collection, chunk IDs, event guards, and retrieval
API unchanged. This proposal requires a separate approval and rebuild plan;
none was performed here.

## Verdict

`RAG_CHUNK_REPRESENTATION_EVALUATION_COMPLETE`

`OFFLINE_ONLY_NO_PRODUCTION_CHANGE`
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path, required=True)
    parser.add_argument("--diagnosis", type=Path, required=True)
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
    diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
    diagnosis_records = {
        str(record["query_id"]): record
        for record in diagnosis.get("records", [])
        if isinstance(record, dict) and record.get("query_id")
    }
    if len(diagnosis_records) != len(validation["valid_rows"]):
        raise ValueError("diagnosis must contain one frozen candidate record for every v2 query")

    candidate_posts_by_query: dict[str, set[str]] = {}
    all_candidate_posts: set[str] = set()
    for row in validation["valid_rows"]:
        query_id = str(row["query_id"])
        candidates = {
            str(value)
            for value in diagnosis_records[query_id]
            .get("candidates_by_depth", {})
            .get(str(DEPTH), [])
        }
        candidate_posts_by_query[query_id] = candidates
        all_candidate_posts.update(candidates)

    started_total = time.perf_counter()
    chunks = _mysql_chunks(
        args.mysql_container,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
        sorted(all_candidate_posts),
    )
    chunk_by_id = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    chunk_ids_by_post: dict[str, set[str]] = {}
    for chunk in chunks:
        chunk_ids_by_post.setdefault(str(chunk["post_id"]), set()).add(str(chunk["chunk_id"]))

    document_vectors: dict[str, dict[str, list[float]]] = {
        name: {} for name in REPRESENTATIONS
    }
    document_latency: dict[str, list[float]] = {name: [] for name in REPRESENTATIONS}
    query_latency: list[float] = []
    query_vectors: dict[str, list[float]] = {}
    with httpx.Client(timeout=60) as client:
        for index, row in enumerate(validation["valid_rows"], 1):
            query_started = time.perf_counter()
            query_vectors[str(row["query_id"])] = _embed(
                client, args.embedding_url, str(row["query"])
            )
            query_latency.append((time.perf_counter() - query_started) * 1000)
            if index % 10 == 0:
                print(json.dumps({"query_embedding_progress": f"{index}/{len(validation['valid_rows'])}"}), flush=True)
        total_embeddings = len(chunks) * len(REPRESENTATIONS)
        completed = 0
        for name, label in REPRESENTATIONS.items():
            for chunk in chunks:
                embedding_started = time.perf_counter()
                vector = _embed(client, args.embedding_url, _text(chunk, label))
                document_latency[name].append((time.perf_counter() - embedding_started) * 1000)
                document_vectors[name][str(chunk["chunk_id"])] = vector
                completed += 1
                if completed % 500 == 0:
                    print(json.dumps({
                        "document_embedding_progress": f"{completed}/{total_embeddings}",
                        "representation": name,
                    }), flush=True)

    metrics: dict[str, dict[str, Any]] = {}
    ranking_latency: dict[str, list[float]] = {name: [] for name in REPRESENTATIONS}
    end_to_end_latency: dict[str, list[float]] = {name: [] for name in REPRESENTATIONS}
    failure_analysis: dict[str, dict[str, Any]] = {}
    failure_cases: list[dict[str, Any]] = []

    for name in REPRESENTATIONS:
        conditional_values = {cutoff: [] for cutoff in CUTOFFS}
        final_values = {cutoff: [] for cutoff in CUTOFFS}
        post_missing = 0
        chunk_miss = 0
        hit_at10 = 0
        for row in validation["valid_rows"]:
            query_id = str(row["query_id"])
            expected = {
                str(entry["chunk_id"]): str(entry["post_id"])
                for entry in row.get("gold_chunks", [])
            }
            if not expected:
                continue
            candidates = candidate_posts_by_query[query_id]
            candidate_chunk_ids = set().union(
                *(chunk_ids_by_post.get(post_id, set()) for post_id in candidates)
            ) if candidates else set()
            rank_started = time.perf_counter()
            ranked = _rank(query_vectors[query_id], candidate_chunk_ids, document_vectors[name])
            rank_ms = (time.perf_counter() - rank_started) * 1000
            ranking_latency[name].append(rank_ms)
            end_to_end_latency[name].append(query_latency[validation["valid_rows"].index(row)] + rank_ms)
            rank_by_id = {item["chunk_id"]: item for item in ranked}
            expected_conditional = {
                chunk_id for chunk_id, post_id in expected.items() if post_id in candidates
            }
            for cutoff in CUTOFFS:
                hit_ids = {item["chunk_id"] for item in ranked[:cutoff]}
                final_values[cutoff].append(len(hit_ids & set(expected)) / len(expected))
                if expected_conditional:
                    conditional_values[cutoff].append(
                        len(hit_ids & expected_conditional) / len(expected_conditional)
                    )
            for chunk_id, post_id in expected.items():
                status = "HIT@10" if chunk_id in rank_by_id and rank_by_id[chunk_id]["rank"] <= 10 else (
                    "POST_MISSING" if post_id not in candidates else "CHUNK_RETRIEVAL_MISS"
                )
                if status == "POST_MISSING":
                    post_missing += 1
                elif status == "CHUNK_RETRIEVAL_MISS":
                    chunk_miss += 1
                else:
                    hit_at10 += 1
                if status != "HIT@10":
                    failure_cases.append({
                        "query_id": query_id,
                        "query": str(row["query"]),
                        "gold_post_id": post_id,
                        "gold_chunk_id": chunk_id,
                        "representation": name,
                        "status": status,
                        "post_in_top10": post_id in candidates,
                        "rank": rank_by_id.get(chunk_id, {}).get("rank"),
                        "score": rank_by_id.get(chunk_id, {}).get("score"),
                        "candidate_posts": sorted(candidates),
                    })
        metrics[name] = {
            "CONDITIONAL_CHUNK_RECALL@5": _mean(conditional_values[5]),
            "CONDITIONAL_CHUNK_RECALL@10": _mean(conditional_values[10]),
            "FINAL_EVIDENCE_RECALL@5": _mean(final_values[5]),
            "FINAL_EVIDENCE_RECALL@10": _mean(final_values[10]),
            "conditional_query_count_at5": len(conditional_values[5]),
            "conditional_query_count_at10": len(conditional_values[10]),
            "answerable_query_count": len(final_values[10]),
        }
        failure_analysis[name] = {
            "post_missing": post_missing,
            "chunk_retrieval_miss": chunk_miss,
            "hit_at10": hit_at10,
            "gold_chunk_reference_count": post_missing + chunk_miss + hit_at10,
        }

    output = {
        "status": "VALID",
        "generated_at": date.today().isoformat(),
        "dataset": {
            "version": validation["dataset_version"],
            "valid_query_count": validation["VALID_QUERY_COUNT"],
            "valid_qrel_count": validation["VALID_QREL_COUNT"],
            "gold_chunk_count": validation["GOLD_CHUNK_COUNT"],
        },
        "scope": {
            "query_count": len(validation["valid_rows"]),
            "answerable_query_count": sum(
                1 for row in validation["valid_rows"] if row.get("category") != "no_answer"
            ),
            "no_answer_query_count": sum(
                1 for row in validation["valid_rows"] if row.get("category") == "no_answer"
            ),
            "candidate_depth": DEPTH,
            "candidate_post_count": len(all_candidate_posts),
            "candidate_chunk_count": len(chunks),
            "ranking": "offline exact cosine over fixed Top10 candidate-post chunks",
            "qdrant_contacted": False,
            "qdrant_updated": False,
            "rebuild": False,
        },
        "representations": {
            "A_CONTENT_ONLY": "content",
            "B_TITLE_CONTENT": "title + content",
            "C_TITLE_TAGS_CONTENT": "title + tags + content",
        },
        "metrics": metrics,
        "latency": {
            "query_embedding": _latency(query_latency),
            "document_embedding": {
                name: _latency(values) for name, values in document_latency.items()
            },
            "ranking": {
                name: _latency(values) for name, values in ranking_latency.items()
            },
            "offline_end_to_end_per_query": {
                name: _latency(values) for name, values in end_to_end_latency.items()
            },
            "total_elapsed_ms": round((time.perf_counter() - started_total) * 1000, 3),
        },
        "failure_analysis": failure_analysis,
        "failure_cases": failure_cases,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "scope": output["scope"],
        "metrics": output["metrics"],
        "latency": output["latency"],
        "failure_analysis": output["failure_analysis"],
        "failure_case_count": len(failure_cases),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
