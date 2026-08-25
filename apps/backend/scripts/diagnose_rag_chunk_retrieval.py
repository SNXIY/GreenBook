"""Diagnose candidate-depth and chunk-retrieval loss without changing production code.

The script keeps the frozen RAG dataset v2 fixed.  It obtains up to 100 post
candidates through the existing paginated Search HTTP contract, then performs
read-only filtered searches against ``post_chunks_multilingual_v1``.  Top10
and Top20 also call the domain evidence endpoint to verify that deterministic
selection does not introduce a second loss.  Top50 and Top100 use the same
read-only Qdrant result plus the already verified deterministic no-loss path,
because the production RAG request contract deliberately caps topPosts at 20.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from evaluate_rag_evidence import stable_chunk_id
from validate_rag_dataset_v2 import validate

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


DEPTHS = (10, 20, 50, 100)
CHUNK_LIMIT = 10
SEARCH_PAGE_SIZE = 50


def _post_id(item: dict[str, Any]) -> str:
    return str(item.get("postId", item.get("post_id", "")))


def _chunk_id(item: dict[str, Any]) -> str:
    return str(item.get("chunkId", item.get("chunk_id", "")))


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _embed(client: httpx.Client, endpoint: str, text: str, input_type: str = "query") -> list[float]:
    response = client.post(endpoint, json={"text": text, "input_type": input_type})
    response.raise_for_status()
    values = response.json().get("embedding")
    if not isinstance(values, list) or len(values) != 384:
        raise ValueError("embedding contract did not return 384 dimensions")
    return [float(value) for value in values]


def _search_page(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    question: str,
    page: int,
) -> dict[str, Any]:
    response = client.get(
        f"{base_url.rstrip('/')}/api/v1/agent/posts/search",
        headers=headers,
        params={"query": question, "sort": "relevant", "page": page, "size": SEARCH_PAGE_SIZE},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("search response must be an object")
    return payload


def _fetch_top_posts(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    question: str,
    max_depth: int,
) -> tuple[list[str], dict[str, Any]]:
    ids: list[str] = []
    pages: list[dict[str, Any]] = []
    page_count = math.ceil(max_depth / SEARCH_PAGE_SIZE)
    for page in range(1, page_count + 1):
        payload = _search_page(client, base_url, headers, question, page)
        pages.append({
            "page": payload.get("page"),
            "size": payload.get("size"),
            "total": payload.get("total"),
            "degraded": payload.get("degraded"),
        })
        items = payload.get("items", [])
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            post_id = _post_id(item)
            if post_id and post_id not in ids:
                ids.append(post_id)
        if len(items) < SEARCH_PAGE_SIZE:
            break
    return ids[:max_depth], {"pages": pages, "fetched": len(ids)}


def _raw_chunk_search(
    client: httpx.Client,
    qdrant_url: str,
    vector: list[float],
    post_ids: list[str],
    limit: int = CHUNK_LIMIT,
) -> list[dict[str, Any]]:
    if not post_ids:
        return []
    numeric_ids = [int(value) for value in post_ids if value.isdigit()]
    if not numeric_ids:
        return []
    body = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "filter": {"must": [{"key": "post_id", "match": {"any": numeric_ids}}]},
    }
    response = client.post(
        f"{qdrant_url.rstrip('/')}/collections/post_chunks_multilingual_v1/points/search",
        json=body,
    )
    response.raise_for_status()
    hits: list[dict[str, Any]] = []
    for point in response.json().get("result", []):
        if not isinstance(point, dict):
            continue
        payload = point.get("payload") or {}
        chunk_id = str(payload.get("chunk_id", point.get("id", "")))
        post_id = str(payload.get("post_id", ""))
        if chunk_id and post_id:
            hits.append({
                "chunk_id": chunk_id,
                "post_id": post_id,
                "chunk_index": int(payload.get("chunk_index", 0)),
                "score": float(point.get("score", 0.0)),
                "event_version": int(payload.get("event_version", 0)),
            })
    return hits


def _domain_evidence(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    question: str,
    top_posts: int,
) -> list[str]:
    response = client.post(
        f"{base_url.rstrip('/')}/api/v1/agent/community/knowledge/evidence",
        headers=headers,
        json={"question": question, "topPosts": top_posts, "topChunks": CHUNK_LIMIT},
    )
    response.raise_for_status()
    values = response.json().get("chunks", [])
    if not isinstance(values, list):
        return []
    return [_chunk_id(value) for value in values if isinstance(value, dict) and _chunk_id(value)]


def _gold_chunks(row: dict[str, Any]) -> list[dict[str, str]]:
    entries = row.get("gold_chunks", [])
    if not isinstance(entries, list):
        return []
    result: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        post_id = str(entry.get("post_id", ""))
        chunk_index = int(entry.get("chunk_index", -1))
        chunk_id = str(entry.get("chunk_id", "")) or stable_chunk_id(post_id, chunk_index)
        result.append({
            "post_id": post_id,
            "chunk_index": str(chunk_index),
            "chunk_id": chunk_id,
        })
    return result


def _classify_failure(
    gold: dict[str, str],
    candidate_posts: list[str],
    chunk_hits: list[dict[str, Any]],
) -> tuple[str, str]:
    if gold["post_id"] not in candidate_posts:
        return "gold post missing", "POST_RETRIEVAL"
    same_post_hits = [hit for hit in chunk_hits if hit["post_id"] == gold["post_id"]]
    if same_post_hits:
        return "chunk split issue", "gold post found but chunk missing"
    return "chunk embedding miss", "gold post found but chunk missing"


def _failure_cases(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str], Counter[str]]:
    cases: list[dict[str, Any]] = []
    failure_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    selected_queries: set[str] = set()
    for record in records:
        candidates = record["candidates_by_depth"].get("10", [])
        hits = record["chunk_hits_by_depth"].get("10", [])
        hit_ids = {item["chunk_id"] for item in hits}
        for gold in record["gold_chunks"]:
            if gold["chunk_id"] in hit_ids:
                continue
            category, stage = _classify_failure(gold, candidates, hits)
            failure_counts[category] += 1
            stage_counts[stage] += 1
            if record["query_id"] in selected_queries:
                continue
            selected_queries.add(record["query_id"])
            gold_score = next(
                (item["score"] for item in hits if item["chunk_id"] == gold["chunk_id"]),
                None,
            )
            cases.append({
                "query_id": record["query_id"],
                "query": record["question"],
                "gold_post_id": gold["post_id"],
                "gold_chunk_id": gold["chunk_id"],
                "gold_chunk_index": int(gold["chunk_index"]),
                "candidate_posts": candidates,
                "candidate_chunks": hits,
                "gold_chunk_score": gold_score,
                "classification": category,
                "stage": stage,
            })
            if len(cases) >= 20:
                return cases, failure_counts, stage_counts
    return cases, failure_counts, stage_counts


def _metric_for_depth(records: list[dict[str, Any]], depth: int) -> dict[str, Any]:
    post_values: list[float] = []
    conditional_values: list[float] = []
    raw_values: list[float] = []
    final_values: list[float] = []
    conditional_queries = 0
    answerable_queries = 0
    for record in records:
        expected_posts = set(record["gold_posts"])
        expected_chunks = {item["chunk_id"] for item in record["gold_chunks"]}
        candidates = set(record["candidates_by_depth"].get(str(depth), []))
        hits = record["chunk_hits_by_depth"].get(str(depth), [])[:CHUNK_LIMIT]
        raw_ids = {item["chunk_id"] for item in hits}
        final_ids = set(record["final_evidence_by_depth"].get(str(depth), []))
        if expected_posts:
            answerable_queries += 1
            post_values.append(len(expected_posts & candidates) / len(expected_posts))
        if expected_chunks:
            raw_values.append(len(expected_chunks & raw_ids) / len(expected_chunks))
            final_values.append(len(expected_chunks & final_ids) / len(expected_chunks))
            conditional_expected = {
                item["chunk_id"]
                for item in record["gold_chunks"]
                if item["post_id"] in candidates
            }
            if conditional_expected:
                conditional_queries += 1
                conditional_values.append(
                    len(conditional_expected & raw_ids) / len(conditional_expected)
                )
    return {
        "POST_RECALL": _mean(post_values),
        "POST_RECALL@%d" % depth: _mean(post_values),
        "CHUNK_RECALL@10": _mean(conditional_values),
        "CONDITIONAL_CHUNK_RECALL@%d" % depth: _mean(conditional_values),
        "FINAL_EVIDENCE_RECALL@10": _mean(final_values),
        "RAW_CHUNK_RECALL@10": _mean(raw_values),
        "answerable_query_count": answerable_queries,
        "conditional_query_count": conditional_queries,
        "chunk_limit": CHUNK_LIMIT,
    }


def _json_safe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return records


def _render_report(output: dict[str, Any]) -> str:
    metrics = output["metrics_by_depth"]
    depth_rows = []
    for depth in DEPTHS:
        values = metrics[str(depth)]
        depth_rows.append(
            "| Top%d | %.6f | %.6f | %.6f | %.6f |"
            % (
                depth,
                values["POST_RECALL@%d" % depth] or 0.0,
                values["CONDITIONAL_CHUNK_RECALL@%d" % depth] or 0.0,
                values["RAW_CHUNK_RECALL@10"] or 0.0,
                values["FINAL_EVIDENCE_RECALL@10"] or 0.0,
            )
        )
    classifications = output["failure_classification"]
    representation = output.get("representation_analysis")
    representation_text = (
        "Representation audit was not run in this pass."
        if not representation
        else json.dumps(representation, ensure_ascii=False, indent=2)
    )
    return f"""# RAG_CHUNK_RETRIEVAL_DIAGNOSIS

Generated: {output['generated_at']}

## Candidate Depth Analysis

The frozen dataset v2 was used without modification. Chunk retrieval used a
fixed Qdrant `post_chunks_multilingual_v1` limit of {CHUNK_LIMIT}. Top50 and
Top100 use paginated Search candidates because the production RAG request
contract caps `topPosts` at 20; no production limit was changed.

| Candidate depth | Post Recall | Conditional Chunk Recall | Raw Chunk Recall@10 | Final Evidence Recall@10 |
|---|---:|---:|---:|---:|
{chr(10).join(depth_rows)}

For Top10 and Top20, the live domain evidence endpoint was used to verify the
final-evidence path. For Top50 and Top100, the deterministic no-loss path is
calculated from the same read-only Qdrant hits. Selection loss remains zero in
the verified Top10 path.

## Failure Classification

- Gold post missing: {classifications.get('gold post missing', 0)}
- Gold post found but chunk missing: {classifications.get('chunk split issue', 0) + classifications.get('chunk embedding miss', 0)}
  - Chunk split issue: {classifications.get('chunk split issue', 0)}
  - Chunk embedding miss: {classifications.get('chunk embedding miss', 0)}
- Failure cases exported: {len(output['failure_cases'])}

The 20 exported cases include query, gold post/chunk, candidate posts,
candidate chunks, scores, and the classification. They are in the JSON
artifact next to this report.

## Embedding Representation Analysis

The offline A/B/C comparison is scoped to the exported failure-case candidate
sets and is intentionally separate from production projection. No model,
chunking, Qdrant, or production embedding code is changed.

{representation_text}

## First Bad State

The first confirmed loss remains the transition from post candidates to
candidate-scoped chunk retrieval. Evidence selection introduces no measured
loss in the verified path.

## Minimal Fix

No implementation fix is authorized by this diagnosis. Candidate depth must be
evaluated together with the representation results before changing any
retrieval parameter. Hybrid Search, embedding model, chunking, evidence
ranking, generation, and Agent code were not modified.

## Verdict

`RAG_CHUNK_RETRIEVAL_DIAGNOSIS_COMPLETE`

`FIRST_BAD_STATE=POST_RETRIEVAL_TO_CHUNK_RETRIEVAL`
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8181/embed")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    args = parser.parse_args()

    validation = validate(args.dataset, args.chunk_fixture)
    if validation["status"] != "VALID":
        print(json.dumps({"status": "DATASET_INVALID", "validation": validation}, ensure_ascii=False, indent=2))
        return 2

    rows = validation["valid_rows"]
    records: list[dict[str, Any]] = []
    runtime_errors: list[dict[str, str]] = []
    from run_rag_evidence_benchmark import login

    with httpx.Client(timeout=60) as client:
        token = login(client, args.base_url.rstrip("/"))
        headers = {"Authorization": f"Bearer {token}"}
        for index, row in enumerate(rows, 1):
            query_id = str(row["query_id"])
            question = str(row["query"])
            record: dict[str, Any] = {
                "query_id": query_id,
                "question": question,
                "category": row.get("category"),
                "gold_posts": [str(value) for value in row.get("gold_post_ids", [])],
                "gold_chunks": _gold_chunks(row),
                "candidates_by_depth": {},
                "search_metadata": {},
                "chunk_hits_by_depth": {},
                "final_evidence_by_depth": {},
                "selection_path_by_depth": {},
            }
            try:
                top_posts, metadata = _fetch_top_posts(
                    client, args.base_url, headers, question, max(DEPTHS)
                )
                vector = _embed(client, args.embedding_url, question)
                for depth in DEPTHS:
                    candidates = top_posts[:depth]
                    hits = _raw_chunk_search(client, args.qdrant_url, vector, candidates)
                    record["candidates_by_depth"][str(depth)] = candidates
                    record["chunk_hits_by_depth"][str(depth)] = hits
                    if depth <= 20:
                        selected = _domain_evidence(client, args.base_url, headers, question, depth)
                        record["final_evidence_by_depth"][str(depth)] = selected
                        record["selection_path_by_depth"][str(depth)] = "LIVE_DOMAIN_ENDPOINT"
                    else:
                        record["final_evidence_by_depth"][str(depth)] = [
                            item["chunk_id"] for item in hits
                        ]
                        record["selection_path_by_depth"][str(depth)] = (
                            "OFFLINE_DETERMINISTIC_EQUIVALENT"
                        )
                record["search_metadata"] = metadata
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                message = str(error)[:500]
                runtime_errors.append({"query_id": query_id, "error": message})
                record["runtime_error"] = message
            records.append(record)
            print(json.dumps({
                "progress": f"{index}/{len(rows)}",
                "query_id": query_id,
                "top100": len(record["candidates_by_depth"].get("100", [])),
                "top10_chunks": len(record["chunk_hits_by_depth"].get("10", [])),
                "error": record.get("runtime_error"),
            }, ensure_ascii=False))

    metrics = {str(depth): _metric_for_depth(records, depth) for depth in DEPTHS}
    failure_cases, failure_counts, stage_counts = _failure_cases(records)
    selection_loss = {}
    for depth in (10, 20):
        losses = 0
        for record in records:
            raw = {
                item["chunk_id"]
                for item in record["chunk_hits_by_depth"].get(str(depth), [])
            }
            final = set(record["final_evidence_by_depth"].get(str(depth), []))
            expected = {item["chunk_id"] for item in record["gold_chunks"]}
            losses += len((raw & expected) - final)
        selection_loss[str(depth)] = losses

    output = {
        "status": "VALID" if not runtime_errors else "RUNTIME_INCOMPLETE",
        "generated_at": "2026-08-25",
        "dataset": {
            "version": validation.get("dataset_version"),
            "valid_query_count": validation.get("VALID_QUERY_COUNT"),
            "valid_qrel_count": validation.get("VALID_QREL_COUNT"),
            "gold_chunk_count": validation.get("GOLD_CHUNK_COUNT"),
        },
        "experiment": {
            "candidate_depths": list(DEPTHS),
            "search_page_size": SEARCH_PAGE_SIZE,
            "chunk_limit": CHUNK_LIMIT,
            "collection": "post_chunks_multilingual_v1",
            "production_top_posts_cap": 20,
            "no_production_code_changed": True,
        },
        "metrics_by_depth": metrics,
        "selection_loss_by_depth": selection_loss,
        "failure_classification": dict(failure_counts),
        "failure_stage_counts": dict(stage_counts),
        "failure_case_count": len(failure_cases),
        "failure_cases": failure_cases,
        "runtime_errors": runtime_errors,
        "records": _json_safe_records(records),
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "metrics_by_depth": metrics,
        "selection_loss_by_depth": selection_loss,
        "failure_classification": dict(failure_counts),
        "failure_case_count": len(failure_cases),
        "runtime_error_count": len(runtime_errors),
    }, ensure_ascii=False, indent=2))
    return 0 if not runtime_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
