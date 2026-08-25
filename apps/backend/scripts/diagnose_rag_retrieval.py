"""Diagnose the existing RAG retrieval chain without changing production code.

The runner uses the existing Search HTTP contract for post candidates, the
existing embedding sidecar, and a read-only Qdrant query for raw chunk hits.
The domain evidence endpoint is called separately to observe deterministic
selection. This is an evaluation tool, not a production capability.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from evaluate_rag_evidence import stable_chunk_id
from validate_rag_dataset import load_chunk_fixture, validate

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


def _post_id(item: dict[str, Any]) -> str:
    return str(item.get("postId", item.get("post_id", "")))


def _chunk_id(item: dict[str, Any]) -> str:
    return str(item.get("chunkId", item.get("chunk_id", "")))


def _post_ids(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [_post_id(item) for item in items if isinstance(item, dict) and _post_id(item)]


def _embed(client: httpx.Client, endpoint: str, question: str) -> list[float]:
    response = client.post(endpoint, json={"text": question, "input_type": "query"})
    response.raise_for_status()
    values = response.json().get("embedding")
    if not isinstance(values, list) or len(values) != 384:
        raise ValueError("embedding contract did not return 384 dimensions")
    return [float(value) for value in values]


def _raw_chunk_search(
    client: httpx.Client,
    qdrant_url: str,
    vector: list[float],
    post_ids: list[str],
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not post_ids:
        return []
    body = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "filter": {
            "must": [{"key": "post_id", "match": {"any": [int(value) for value in post_ids]}}]
        },
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


def _gold_rows(row: dict[str, Any], chunk_fixture: dict[str, set[int]]) -> tuple[list[dict[str, Any]], list[str]]:
    statuses = row.get("qrel_status", {})
    gold: list[dict[str, Any]] = []
    issues: list[str] = []
    for entry in row.get("gold_chunks", []):
        post_id = str(entry.get("post_id", ""))
        chunk_index = int(entry.get("chunk_index", -1))
        status = statuses.get(post_id, {})
        if not status.get("searchable", True):
            continue
        if chunk_index not in chunk_fixture.get(post_id, set()):
            issues.append(f"MISSING_CHUNK_FIXTURE:{post_id}:{chunk_index}")
            continue
        gold.append({
            "post_id": post_id,
            "chunk_index": chunk_index,
            "chunk_id": stable_chunk_id(post_id, chunk_index),
        })
    if gold and not row.get("gold_answer"):
        issues.append("GOLD_ANSWER_NOT_ANNOTATED")
    answer_chunk_ids = row.get("answer_evidence_chunk_ids")
    if gold and row.get("gold_answer") and not isinstance(answer_chunk_ids, list):
        issues.append("ANSWER_EVIDENCE_IDS_NOT_ANNOTATED")
    if gold and isinstance(answer_chunk_ids, list):
        valid_ids = {item["chunk_id"] for item in gold}
        if any(str(value) not in valid_ids for value in answer_chunk_ids):
            issues.append("ANSWER_EVIDENCE_ID_OUTSIDE_GOLD_CHUNKS")
    return gold, sorted(set(issues))


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _recall_rows(rows: list[dict[str, Any]], key: str, cutoff: int) -> list[float]:
    values: list[float] = []
    for row in rows:
        expected = row[key]
        if not expected:
            continue
        actual = set(row["hybrid_candidates"][:cutoff]) if key == "gold_posts" else set(
            item["chunk_id"] for item in row[key][:cutoff]
        )
        values.append(len(set(expected) & actual) / len(expected))
    return values


def _conditional_chunk_recall(rows: list[dict[str, Any]], cutoff: int) -> float | None:
    recalls: list[float] = []
    for row in rows:
        candidate_posts = set(row["hybrid_candidates"][:cutoff])
        expected = [item for item in row["gold_chunks"] if item["post_id"] in candidate_posts]
        if not expected:
            continue
        actual = {item["chunk_id"] for item in row["chunk_candidates"][:cutoff]}
        recalls.append(len({item["chunk_id"] for item in expected} & actual) / len(expected))
    return _mean(recalls)


def _classify(row: dict[str, Any], gold: dict[str, Any]) -> tuple[str, str]:
    post_id = gold["post_id"]
    chunk_id = gold["chunk_id"]
    if post_id not in row["hybrid_candidates"][:10]:
        return "RETRIEVAL_ISSUE", "post missing"
    raw = row["chunk_candidates"][:10]
    if chunk_id in {item["chunk_id"] for item in raw}:
        if chunk_id not in {item["chunk_id"] for item in row["selected_evidence"][:10]}:
            return "RETRIEVAL_ISSUE", "selection loss"
        return "NO_MISS", "hit"
    if any(item["post_id"] == post_id for item in raw):
        return "RETRIEVAL_ISSUE", "wrong chunk"
    return "RETRIEVAL_ISSUE", "embedding issue"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8181/embed")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    parser.add_argument("--top-posts", type=int, default=10)
    args = parser.parse_args()

    validation = validate(args.dataset, args.fixture, args.chunk_fixture)
    if validation["status"] != "VALID":
        print(json.dumps({"status": "DATASET_INVALID", "validation": validation}, ensure_ascii=False, indent=2))
        return 2
    rows = validation["valid_rows"]
    chunk_fixture = load_chunk_fixture(args.chunk_fixture)

    from run_rag_evidence_benchmark import login

    diagnostics: list[dict[str, Any]] = []
    with httpx.Client(timeout=60) as client:
        token = login(client, args.base_url.rstrip("/"))
        headers = {"Authorization": f"Bearer {token}"}
        for row in rows:
            question = str(row["question"])
            gold_chunks, dataset_issues = _gold_rows(row, chunk_fixture)
            gold_posts = sorted({item["post_id"] for item in gold_chunks})
            item: dict[str, Any] = {
                "query_id": row["query_id"],
                "question": question,
                "category": row.get("category"),
                "gold_posts": gold_posts,
                "gold_chunks": gold_chunks,
                "dataset_issues": dataset_issues,
                "hybrid_candidates": [],
                "chunk_candidates": [],
                "selected_evidence": [],
            }
            try:
                search = client.get(
                    f"{args.base_url.rstrip('/')}/api/v1/agent/posts/search",
                    headers=headers,
                    params={"query": question, "sort": "relevant", "page": 1, "size": args.top_posts},
                )
                search.raise_for_status()
                item["hybrid_candidates"] = _post_ids(search.json().get("items", []))
                vector = _embed(client, args.embedding_url, question)
                item["chunk_candidates"] = _raw_chunk_search(
                    client, args.qdrant_url, vector, item["hybrid_candidates"], limit=10
                )
                evidence = client.post(
                    f"{args.base_url.rstrip('/')}/api/v1/agent/community/knowledge/evidence",
                    headers=headers,
                    json={"question": question, "topPosts": args.top_posts, "topChunks": 10},
                )
                evidence.raise_for_status()
                item["selected_evidence"] = [
                    {
                        "chunk_id": _chunk_id(value),
                        "post_id": str(value.get("postId", value.get("post_id", ""))),
                        "score": value.get("score"),
                    }
                    for value in evidence.json().get("chunks", [])
                    if isinstance(value, dict) and _chunk_id(value)
                ]
            except (httpx.HTTPError, ValueError, KeyError) as error:
                item["runtime_error"] = str(error)[:500]

            item["gold_post_hit_at10"] = bool(set(gold_posts) & set(item["hybrid_candidates"][:10]))
            item["first_bad_state"] = "DATASET_ISSUE" if dataset_issues else "NONE"
            item["failure_categories"] = []
            for gold in gold_chunks:
                state, category = _classify(item, gold)
                item["failure_categories"].append({
                    "gold_post": gold["post_id"],
                    "gold_chunk": gold["chunk_id"],
                    "state": state,
                    "category": category,
                    "chunk_split_signal": (
                        category == "wrong chunk"
                        and any(hit["post_id"] == gold["post_id"] for hit in item["chunk_candidates"][:10])
                    ),
                })
                if state == "RETRIEVAL_ISSUE" and item["first_bad_state"] == "NONE":
                    item["first_bad_state"] = state
            diagnostics.append(item)
            print(json.dumps({
                "query_id": item["query_id"],
                "candidate_posts": len(item["hybrid_candidates"]),
                "raw_chunks": len(item["chunk_candidates"]),
                "selected_chunks": len(item["selected_evidence"]),
                "first_bad_state": item["first_bad_state"],
                "dataset_issues": item["dataset_issues"],
            }, ensure_ascii=False))

    post5 = _mean(_recall_rows(diagnostics, "gold_posts", 5))
    post10 = _mean(_recall_rows(diagnostics, "gold_posts", 10))

    def final_recall(cutoff: int) -> float | None:
        values: list[float] = []
        for item in diagnostics:
            expected = {gold["chunk_id"] for gold in item["gold_chunks"]}
            if not expected:
                continue
            actual = {_chunk_id(hit) for hit in item["selected_evidence"][:cutoff]}
            values.append(len(expected & actual) / len(expected))
        return _mean(values)

    def raw_recall(cutoff: int) -> float | None:
        values: list[float] = []
        for item in diagnostics:
            expected = {gold["chunk_id"] for gold in item["gold_chunks"]}
            if not expected:
                continue
            actual = {hit["chunk_id"] for hit in item["chunk_candidates"][:cutoff]}
            values.append(len(expected & actual) / len(expected))
        return _mean(values)

    selection_losses = 0
    dataset_issue_queries = 0
    retrieval_issue_queries = 0
    for item in diagnostics:
        if item["dataset_issues"]:
            dataset_issue_queries += 1
        if any(value["state"] == "RETRIEVAL_ISSUE" for value in item["failure_categories"]):
            retrieval_issue_queries += 1
        raw_ids = {value["chunk_id"] for value in item["chunk_candidates"][:10]}
        final_ids = {_chunk_id(value) for value in item["selected_evidence"][:10]}
        expected = {value["chunk_id"] for value in item["gold_chunks"]}
        selection_losses += len((raw_ids & expected) - final_ids)

    failures: list[dict[str, Any]] = []
    for item in diagnostics:
        for failure in item["failure_categories"]:
            if failure["state"] != "NO_MISS":
                failures.append({
                    "query": item["question"],
                    "query_id": item["query_id"],
                    "gold_post": failure["gold_post"],
                    "gold_chunk": failure["gold_chunk"],
                    "hybrid_candidates": item["hybrid_candidates"],
                    "chunk_candidates": item["chunk_candidates"],
                    "selected_evidence": item["selected_evidence"],
                    "first_bad_state": "DATASET_ISSUE" if item["dataset_issues"] else failure["state"],
                    "category": failure["category"],
                    "chunk_split_signal": failure["chunk_split_signal"],
                    "dataset_issues": item["dataset_issues"],
                })
    failures = failures[: max(20, min(len(failures), 50))]

    failure_category_counts = Counter(
        failure["category"]
        for item in diagnostics
        for failure in item["failure_categories"]
        if failure["state"] != "NO_MISS"
    )
    chunk_split_signal_count = sum(
        1
        for item in diagnostics
        for failure in item["failure_categories"]
        if failure["state"] != "NO_MISS" and failure["chunk_split_signal"]
    )
    dataset_issue_types = Counter(
        issue
        for item in diagnostics
        for issue in item["dataset_issues"]
    )

    output = {
        "status": "VALID",
        "dataset_validation": {
            key: validation[key]
            for key in (
                "VALID_QUERY_COUNT", "VALID_QREL_COUNT", "REMOVED_INVALID_COUNT",
                "MISSING_FIXTURE_COUNT", "MISSING_CHUNK_FIXTURE_COUNT",
                "NON_SEARCHABLE_QREL_COUNT",
            )
        },
        "gold_chunk_validation": {
            "status": "DATASET_ISSUE" if dataset_issue_queries else "VALIDATED",
            "dataset_issue_query_count": dataset_issue_queries,
            "dataset_issue_gold_chunk_count": sum(
                len(item["gold_chunks"]) for item in diagnostics if item["dataset_issues"]
            ),
            "answer_containing_evidence": "NOT_ANNOTATED",
            "note": "Current gold chunks are post-level qrel -> chunk_index 0 mappings; no gold_answer/evidence annotation exists.",
        },
        "metrics": {
            "POST_RECALL@5": post5,
            "POST_RECALL@10": post10,
            "CONDITIONAL_CHUNK_RECALL@5": _conditional_chunk_recall(diagnostics, 5),
            "CONDITIONAL_CHUNK_RECALL@10": _conditional_chunk_recall(diagnostics, 10),
            "RAW_CHUNK_RECALL@5": raw_recall(5),
            "RAW_CHUNK_RECALL@10": raw_recall(10),
            "FINAL_EVIDENCE_RECALL@5": final_recall(5),
            "FINAL_EVIDENCE_RECALL@10": final_recall(10),
            "selection_loss_count_at10": selection_losses,
        },
        "issue_counts": {
            "DATASET_ISSUE_QUERIES": dataset_issue_queries,
            "RETRIEVAL_ISSUE_QUERIES": retrieval_issue_queries,
            "DATASET_ISSUE_TYPES": dict(dataset_issue_types),
            "RETRIEVAL_FAILURE_CATEGORIES": dict(failure_category_counts),
            "CHUNK_SPLIT_SIGNAL_COUNT": chunk_split_signal_count,
            "FAILURE_CASE_COUNT": len(failures),
        },
        "failure_cases": failures,
        "diagnostics": diagnostics,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("dataset_validation", "gold_chunk_validation", "metrics", "issue_counts")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
