"""Offline audit and corpus-noise experiments for RAG chunk retrieval V4.

This harness fixes the frozen Top10 post pool from the V3 diagnosis and reads
the current MySQL/Qdrant projection without writing to either system.  It
reproduces the production PostChunker behavior, audits chunk-size and duplicate
competition, and evaluates small simulated corpus transformations.  No
simulated chunk is written back to MySQL, Qdrant, or production source code.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from evaluate_rag_chunk_retrieval_v3 import (
    BASELINE_DEPTH,
    OUTPUT_DEPTH,
    _mysql_chunks,
    _percentile,
    _qdrant_search,
)
from validate_rag_dataset_v2 import stable_chunk_id, validate

BASELINE_CHECKPOINT = "2db9d9f1ec3a36858dc2398140a34ddd733bdc4a"
RAG_DIAGNOSIS_CHECKPOINT = "df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6"
COLLECTION = "post_chunks_multilingual_v1"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_CACHE = r"D:\tmp\greenbook-retrieval-model-cache"
DEFAULT_STORAGE_ROOT = Path("apps/backend/data/storage")
MAX_CHARS = 1200
OVERLAP_CHARS = 160
MERGE_TARGETS = (120, 240)
FILTER_THRESHOLDS = (30, 50, 80, 100)
LENGTH_BUCKETS = (
    ("0-20", 0, 20),
    ("21-50", 21, 50),
    ("51-100", 51, 100),
    ("101-200", 101, 200),
    ("201-400", 201, 400),
    ("401-800", 401, 800),
    ("801-1200", 801, 1200),
    (">1200", 1201, None),
)
LOCAL_FAILURE_CATEGORIES = (
    "MICRO_CHUNK_NOISE",
    "DUPLICATE_CHUNK_NOISE",
    "SAME_POST_FRAGMENT_COMPETITION",
    "GOLD_CHUNK_TOO_FRAGMENTED",
    "GENUINE_SEMANTIC_RANKING_FAILURE",
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "which",
        "with",
        "主要",
        "哪些",
        "什么",
        "如何",
        "怎么",
        "以及",
    }
)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _safe_rate(count: int, total: int) -> float:
    return round(count / total, 6) if total else 0.0


def _text(value: Any) -> str:
    return str(value or "")


def _summary(value: Any, limit: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", _text(value)).strip()
    return normalized[:limit] + ("..." if len(normalized) > limit else "")


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).casefold()


def _terms(value: Any) -> set[str]:
    result: list[str] = []
    for token in _TOKEN_RE.findall(_text(value).casefold()):
        if all("\u4e00" <= char <= "\u9fff" for char in token):
            if len(token) >= 2:
                result.append(token)
                result.extend(token[index : index + 2] for index in range(len(token) - 1))
        elif token not in _STOPWORDS and len(token) > 1:
            result.append(token)
    return set(result)


def _lexical_similarity(left: Any, right: Any) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    union = left_terms | right_terms
    return round(len(left_terms & right_terms) / len(union), 6) if union else 0.0


def _cosine(left: Any, right: Any) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values, strict=True)) / (left_norm * right_norm)


def _length_distribution(lengths: list[int]) -> dict[str, Any]:
    values = [float(value) for value in lengths]
    return {
        "count": len(lengths),
        "min": min(lengths, default=0),
        "p25": _percentile(values, 0.25),
        "p50": _percentile(values, 0.5),
        "p75": _percentile(values, 0.75),
        "p95": _percentile(values, 0.95),
        "mean": round(statistics.fmean(values), 6) if values else 0.0,
        "max": max(lengths, default=0),
    }


def _bucket(value: int) -> str:
    for name, lower, upper in LENGTH_BUCKETS:
        if value >= lower and (upper is None or value <= upper):
            return name
    return ">1200"


def _bucket_counts(lengths: list[int]) -> dict[str, dict[str, Any]]:
    counts = Counter(_bucket(value) for value in lengths)
    return {
        name: {"count": counts.get(name, 0), "rate": _safe_rate(counts.get(name, 0), len(lengths))}
        for name, _, _ in LENGTH_BUCKETS
    }


def _trim_range(content: str, start: int, end: int) -> tuple[int, int]:
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def _paragraph_ranges(content: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    paragraph_start = 0
    index = 0
    while index < len(content):
        if content[index] == "\n":
            run_start = index
            newline_count = 0
            while index < len(content):
                char = content[index]
                if char == "\r" or char == "\n" or char.isspace():
                    if char == "\n":
                        newline_count += 1
                    index += 1
                else:
                    break
            if newline_count >= 2:
                start, end = _trim_range(content, paragraph_start, run_start)
                if start < end:
                    result.append((start, end))
                paragraph_start = index
            continue
        index += 1
    start, end = _trim_range(content, paragraph_start, len(content))
    if start < end:
        result.append((start, end))
    return result


def _reproduce_post_chunker(content: str) -> list[dict[str, Any]]:
    if not content.strip():
        return []
    result: list[dict[str, Any]] = []
    for paragraph_start, paragraph_end in _paragraph_ranges(content):
        start = paragraph_start
        while start < paragraph_end:
            window_end = min(paragraph_end, start + MAX_CHARS)
            start_trimmed, end_trimmed = _trim_range(content, start, window_end)
            if start_trimmed < end_trimmed:
                result.append(
                    {
                        "chunk_id": stable_chunk_id("0", len(result)),
                        "chunk_index": len(result),
                        "content": content[start_trimmed:end_trimmed],
                        "start_offset": start_trimmed,
                        "end_offset": end_trimmed,
                        "paragraph_length": paragraph_end - paragraph_start,
                    }
                )
            if window_end >= paragraph_end:
                break
            start = max(start + 1, window_end - OVERLAP_CHARS)
    return result


def _reproduce_post(post_id: str, content: str, stored: list[dict[str, Any]]) -> dict[str, Any]:
    paragraphs = _paragraph_ranges(content)
    generated = _reproduce_post_chunker(content)
    id_mismatches = 0
    content_mismatches = 0
    offset_mismatches = 0
    for index, expected in enumerate(generated):
        if index >= len(stored):
            id_mismatches += 1
            continue
        actual = stored[index]
        expected_id = stable_chunk_id(post_id, index)
        if _text(actual.get("chunk_id")) != expected_id:
            id_mismatches += 1
        if _text(actual.get("content")) != expected["content"]:
            content_mismatches += 1
        if (
            int(actual.get("start_offset") or 0) != expected["start_offset"]
            or int(actual.get("end_offset") or 0) != expected["end_offset"]
        ):
            offset_mismatches += 1
    id_mismatches += max(0, len(stored) - len(generated))
    short_paragraphs = sum(length < 100 for _, end in paragraphs for length in (end - _ for _ in [0]))
    # The expression above intentionally uses the same ranges as the Java port;
    # replace it with an explicit calculation for readability in the artifact.
    paragraph_lengths = [end - start for start, end in paragraphs]
    short_paragraphs = sum(length < 100 for length in paragraph_lengths)
    micro_paragraphs = sum(length < 50 for length in paragraph_lengths)
    multi_chunk_paragraphs = sum(
        sum(1 for item in generated if item["start_offset"] >= start and item["end_offset"] <= end) > 1
        for start, end in paragraphs
    )
    return {
        "post_id": post_id,
        "body_chars": len(content.strip()),
        "paragraph_count": len(paragraphs),
        "paragraph_lengths": _length_distribution(paragraph_lengths),
        "short_paragraph_count_lt100": short_paragraphs,
        "micro_paragraph_count_lt50": micro_paragraphs,
        "multi_chunk_paragraph_count": multi_chunk_paragraphs,
        "generated_chunk_count": len(generated),
        "stored_chunk_count": len(stored),
        "reproduction_exact": not id_mismatches and not content_mismatches and not offset_mismatches,
        "id_mismatch_count": id_mismatches,
        "content_mismatch_count": content_mismatches,
        "offset_mismatch_count": offset_mismatches,
    }


def _truncate(value: Any, limit: int) -> str:
    text = _text(value).strip()
    return text if len(text) <= limit else text[:limit]


def _embedding_text(item: dict[str, Any]) -> str:
    return (
        "title: "
        + _truncate(item.get("title"), 256)
        + "\ntags: "
        + _truncate(item.get("tags"), 512)
        + "\ndescription: "
        + _truncate(item.get("description"), 768)
        + "\ncontent: "
        + _text(item.get("content"))
    )


def _decorate_hit(hit: dict[str, Any], chunks_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chunk_id = _text(hit.get("chunk_id"))
    row = chunks_by_id.get(chunk_id, {})
    return {
        **hit,
        "chunk_id": chunk_id,
        "post_id": _text(hit.get("post_id") or row.get("post_id")),
        "chunk_index": int(hit.get("chunk_index") or row.get("chunk_index") or 0),
        "content": _text(row.get("content")),
        "length": len(_text(row.get("content")).strip()),
        "source_chunk_ids": [chunk_id],
        "title": _text(row.get("title")),
        "tags": _text(row.get("tags")),
        "description": _text(row.get("description")),
    }


def _is_duplicate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_normalized = _normalized(left.get("content"))
    right_normalized = _normalized(right.get("content"))
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    if min(len(left_normalized), len(right_normalized)) < 80:
        return False
    return (
        difflib.SequenceMatcher(None, left_normalized, right_normalized, autojunk=False).ratio() >= 0.9
    )


def _duplicate_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    duplicate_slots = 0
    duplicate_pairs = 0
    for index, item in enumerate(items):
        equivalent_before = sum(_is_duplicate(item, previous) for previous in items[:index])
        if equivalent_before:
            duplicate_slots += 1
            duplicate_pairs += equivalent_before
    return {
        "duplicate_equivalent_result_count": duplicate_slots,
        "duplicate_equivalent_pair_count": duplicate_pairs,
    }


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    exact_keys: set[str] = set()
    near_buckets: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        normalized = _normalized(item.get("content"))
        if normalized and normalized in exact_keys:
            continue
        bucket = (len(normalized) // 200, normalized[:64])
        if any(_is_duplicate(item, previous) for previous in near_buckets.get(bucket, [])):
            continue
        retained.append(item)
        if normalized:
            exact_keys.add(normalized)
            near_buckets[bucket].append(item)
    return retained


def _merge_post_chunks(
    post_id: str,
    rows: list[dict[str, Any]],
    body: str,
    target: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        start = int(pending[0].get("start_offset") or 0)
        end = int(pending[-1].get("end_offset") or 0)
        content = body[start:end].strip() if body and 0 <= start <= end <= len(body) else "\n\n".join(
            _text(item.get("content")).strip() for item in pending
        )
        first = pending[0]
        last = pending[-1]
        result.append(
            {
                "chunk_id": f"v4-merge:{post_id}:{int(first['chunk_index'])}-{int(last['chunk_index'])}",
                "post_id": post_id,
                "chunk_index": int(first["chunk_index"]),
                "content": content,
                "length": len(content),
                "start_offset": start,
                "end_offset": end,
                "source_chunk_ids": [_text(item["chunk_id"]) for item in pending],
                "title": _text(first.get("title")),
                "tags": _text(first.get("tags")),
                "description": _text(first.get("description")),
            }
        )
        pending.clear()

    for row in rows:
        if not pending:
            pending.append(row)
            continue
        start = int(pending[0].get("start_offset") or 0)
        end = int(row.get("end_offset") or 0)
        current_end = int(pending[-1].get("end_offset") or 0)
        current_start = int(pending[0].get("start_offset") or 0)
        current_content = body[current_start:current_end].strip() if body else _text(pending[-1].get("content"))
        combined_length = len(body[start:end].strip()) if body and 0 <= start <= end <= len(body) else len(
            "\n\n".join(_text(item.get("content")).strip() for item in pending + [row])
        )
        if len(current_content) < target and combined_length <= MAX_CHARS:
            pending.append(row)
        else:
            flush()
            pending.append(row)
    flush()
    return result


def _merged_corpus(
    candidate_posts: set[str],
    chunks_by_post: dict[str, list[dict[str, Any]]],
    body_by_post: dict[str, str],
    target: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for post_id in sorted(candidate_posts):
        result.extend(_merge_post_chunks(post_id, chunks_by_post.get(post_id, []), body_by_post.get(post_id, ""), target))
    return result


def _build_merge_vectors(embedder: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    vectors = list(embedder.embed([_embedding_text(item) for item in items]))
    return {item["chunk_id"]: vector for item, vector in zip(items, vectors, strict=True)}


def _rank_merged(
    record: dict[str, Any],
    items: list[dict[str, Any]],
    vectors: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate_posts = set(record["candidate_posts"])
    query_vector = record["query_vector"]
    ranked = [
        {
            **item,
            "score": _cosine(query_vector, vectors[item["chunk_id"]]),
        }
        for item in items
        if item["post_id"] in candidate_posts
    ]
    ranked.sort(key=lambda item: (-float(item["score"]), item["chunk_id"]))
    return [dict(item, rank=index + 1) for index, item in enumerate(ranked)]


def _hit_rank(gold_id: str, ranked: list[dict[str, Any]]) -> int | None:
    for index, item in enumerate(ranked, 1):
        if gold_id in item.get("source_chunk_ids", []):
            return index
    return None


def _evaluate_scope(
    records_by_query: dict[str, dict[str, Any]],
    run: dict[str, Any],
    query_ids: set[str],
) -> dict[str, Any]:
    post_recall: list[float] = []
    conditional_recall = {5: [], 10: []}
    final_recall = {5: [], 10: []}
    overall_hit = {1: [], 3: [], 5: [], 10: []}
    conditional_hit = {1: [], 3: [], 5: [], 10: []}
    overall_mrr: list[float] = []
    conditional_mrr: list[float] = []
    candidate_counts: list[int] = []
    selected_counts: list[int] = []
    removed_count = 0
    removable_total = 0
    duplicate_slots = 0
    selected_slots = 0
    same_post_competition_slots = 0
    for query_id in sorted(query_ids):
        record = records_by_query[query_id]
        ranked = run["ranked_by_query"][query_id]
        pool = run["pool_by_query"][query_id]
        selected = ranked[:OUTPUT_DEPTH]
        candidate_counts.append(len(pool))
        selected_counts.append(len(selected))
        selected_slots += len(selected)
        duplicate_slots += _duplicate_stats(selected)["duplicate_equivalent_result_count"]
        counts = Counter(item["post_id"] for item in selected)
        same_post_competition_slots += sum(max(0, value - 1) for value in counts.values())
        gold = [item for item in record["gold_chunks"] if isinstance(item, dict)]
        gold_posts = {str(value) for value in record["gold_post_ids"]}
        candidate_posts = set(record["candidate_posts"])
        if gold_posts:
            post_recall.append(len(gold_posts & candidate_posts) / len(gold_posts))
        conditional_gold = [item for item in gold if str(item["post_id"]) in candidate_posts]
        for cutoff in (5, 10):
            final_hits = sum(_hit_rank(str(item["chunk_id"]), ranked[:cutoff]) is not None for item in gold)
            final_recall[cutoff].append(final_hits / len(gold) if gold else 0.0)
        first_overall = min(
            (rank for item in gold if (rank := _hit_rank(str(item["chunk_id"]), ranked)) is not None),
            default=None,
        )
        overall_mrr.append(1 / first_overall if first_overall else 0.0)
        for cutoff in overall_hit:
            overall_hit[cutoff].append(
                1.0 if any(_hit_rank(str(item["chunk_id"]), ranked[:cutoff]) is not None for item in gold) else 0.0
            )
        if conditional_gold:
            for cutoff in (5, 10):
                hits = sum(
                    _hit_rank(str(item["chunk_id"]), ranked[:cutoff]) is not None
                    for item in conditional_gold
                )
                conditional_recall[cutoff].append(hits / len(conditional_gold))
            first_conditional = min(
                (
                    rank
                    for item in conditional_gold
                    if (rank := _hit_rank(str(item["chunk_id"]), ranked)) is not None
                ),
                default=None,
            )
            conditional_mrr.append(1 / first_conditional if first_conditional else 0.0)
            for cutoff in conditional_hit:
                conditional_hit[cutoff].append(
                    1.0
                    if any(
                        _hit_rank(str(item["chunk_id"]), ranked[:cutoff]) is not None
                        for item in conditional_gold
                    )
                    else 0.0
                )
        source_ids = {
            source_id
            for item in pool
            for source_id in item.get("source_chunk_ids", [])
        }
        removed_count += sum(str(item["chunk_id"]) not in source_ids for item in conditional_gold)
        removable_total += len(conditional_gold)
    return {
        "query_count": len(query_ids),
        "post_recall_at10": _mean(post_recall),
        "conditional_chunk_recall_at5": _mean(conditional_recall[5]),
        "conditional_chunk_recall_at10": _mean(conditional_recall[10]),
        "final_evidence_recall_at5": _mean(final_recall[5]),
        "final_evidence_recall_at10": _mean(final_recall[10]),
        "chunk_mrr": _mean(overall_mrr),
        "conditional_chunk_mrr": _mean(conditional_mrr),
        "hit_at": {
            str(cutoff): {
                "overall": _mean(overall_hit[cutoff]),
                "gold_post_present": _mean(conditional_hit[cutoff]),
            }
            for cutoff in (1, 3, 5, 10)
        },
        "avg_candidates": _mean([float(value) for value in candidate_counts]),
        "avg_selected": _mean([float(value) for value in selected_counts]),
        "max_selected": max(selected_counts, default=0),
        "duplicate_slot_waste": _safe_rate(duplicate_slots, selected_slots),
        "duplicate_equivalent_slots": duplicate_slots,
        "selected_slots": selected_slots,
        "same_post_competition_slots": same_post_competition_slots,
        "gold_removed_rate": _safe_rate(removed_count, removable_total),
        "gold_removed_count": removed_count,
        "gold_removable_count": removable_total,
        "conditional_query_count": len(conditional_recall[10]),
        "rank_latency_ms": {
            "p50": run["rank_latency_ms"]["p50"],
            "p95": run["rank_latency_ms"]["p95"],
            "max": run["rank_latency_ms"]["max"],
        },
    }


def _evaluate_strategy(
    name: str,
    records: list[dict[str, Any]],
    ranker: Any,
    complexity: str,
    preparation_ms: float = 0.0,
) -> dict[str, Any]:
    ranked_by_query: dict[str, list[dict[str, Any]]] = {}
    pool_by_query: dict[str, list[dict[str, Any]]] = {}
    latencies: list[float] = []
    for record in records:
        started = time.perf_counter()
        pool, ranked = ranker(record)
        latencies.append((time.perf_counter() - started) * 1000)
        ranked_by_query[record["query_id"]] = ranked
        pool_by_query[record["query_id"]] = pool
    run = {
        "strategy": name,
        "complexity": complexity,
        "ranked_by_query": ranked_by_query,
        "pool_by_query": pool_by_query,
        "rank_latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "preparation_ms": round(preparation_ms, 3),
    }
    return run


def _compact_strategy_run(run: dict[str, Any]) -> dict[str, Any]:
    selected_by_query: dict[str, dict[str, Any]] = {}
    pool_by_query = run.get("pool_by_query", {})
    for query_id, ranked in run.get("ranked_by_query", {}).items():
        selected_by_query[query_id] = {
            "pool_count": len(pool_by_query.get(query_id, [])),
            "selected": [
                {
                    key: item.get(key)
                    for key in (
                        "chunk_id",
                        "post_id",
                        "chunk_index",
                        "rank",
                        "score",
                        "length",
                        "source_chunk_ids",
                    )
                    if key in item
                }
                for item in ranked[:OUTPUT_DEPTH]
            ],
        }
    return {
        "strategy": run["strategy"],
        "complexity": run["complexity"],
        "rank_latency_ms": run["rank_latency_ms"],
        "preparation_ms": run["preparation_ms"],
        "metrics": run.get("metrics", {}),
        "cases": selected_by_query,
    }


def _make_content_maps(
    audit: dict[str, Any],
    storage_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    posts = {
        _text(post["id"]): post
        for post in audit["live_corpus"]["posts"]
        if isinstance(post, dict) and post.get("id")
    }
    bodies: dict[str, str] = {}
    for post_id, post in posts.items():
        object_key = _text(post.get("content_object_key"))
        if not object_key:
            bodies[post_id] = ""
            continue
        target = (storage_root / PurePosixPath(object_key.replace("\\", "/"))).resolve()
        if target != storage_root and storage_root not in target.parents:
            raise RuntimeError(f"unsafe object key for post {post_id}")
        bodies[post_id] = target.read_text(encoding="utf-8")
    return posts, bodies


def _gold_length_metrics(
    rows: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    traces_by_query: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    refs: list[dict[str, Any]] = []
    for row in rows:
        trace = traces_by_query.get(_text(row["query_id"]), {})
        saved_hits = trace.get("candidate_chunks", [])
        candidate_posts = {
            _text(item.get("post_id"))
            for item in trace.get("candidate_posts", [])
            if isinstance(item, dict) and item.get("post_id")
        }
        for item in row.get("gold_chunks", []):
            if not isinstance(item, dict):
                continue
            chunk_id = _text(item["chunk_id"])
            chunk = chunks_by_id.get(chunk_id, {})
            rank = next(
                (
                    index
                    for index, hit in enumerate(saved_hits, 1)
                    if _text(hit.get("chunk_id")) == chunk_id
                ),
                None,
            )
            refs.append(
                {
                    "query_id": _text(row["query_id"]),
                    "chunk_id": chunk_id,
                    "post_id": _text(item["post_id"]),
                    "chunk_index": int(item["chunk_index"]),
                    "length": len(_text(chunk.get("content")).strip()),
                    "rank": rank,
                    "candidate_post_present": _text(item["post_id"]) in candidate_posts,
                    "hit_at10": bool(rank and rank <= OUTPUT_DEPTH),
                    "mrr": round(1 / rank, 6) if rank else 0.0,
                }
            )
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in refs:
        by_bucket[_bucket(int(ref["length"]))].append(ref)
    metrics = {}
    for name, _, _ in LENGTH_BUCKETS:
        bucket_refs = by_bucket.get(name, [])
        conditional = [ref for ref in bucket_refs if ref["candidate_post_present"]]
        metrics[name] = {
            "count": len(bucket_refs),
            "rate": _safe_rate(len(bucket_refs), len(refs)),
            "hit_at10": _mean([1.0 if ref["hit_at10"] else 0.0 for ref in bucket_refs]),
            "mrr": _mean([float(ref["mrr"]) for ref in bucket_refs]),
            "post_present_count": len(conditional),
            "post_present_hit_at10": _mean([1.0 if ref["hit_at10"] else 0.0 for ref in conditional]),
            "post_present_mrr": _mean([float(ref["mrr"]) for ref in conditional]),
        }
    unique_by_id = {ref["chunk_id"]: ref for ref in refs}
    return {
        "reference_count": len(refs),
        "unique_chunk_count": len(unique_by_id),
        "reference_length_distribution": _length_distribution([ref["length"] for ref in refs]),
        "unique_chunk_length_distribution": _length_distribution([ref["length"] for ref in unique_by_id.values()]),
        "reference_buckets": _bucket_counts([ref["length"] for ref in refs]),
        "bucket_retrieval_metrics": metrics,
    }, refs


def _local_failure_diagnostics(
    records_by_query: dict[str, dict[str, Any]],
    traces_by_query: dict[str, dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    embedder: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    local_refs: list[tuple[str, dict[str, Any]]] = []
    for query_id, trace in traces_by_query.items():
        for gold in trace.get("gold_chunks", []):
            if isinstance(gold, dict) and gold.get("failure_family") == "LOCAL_RANKING_FAILURE":
                local_refs.append((query_id, gold))
    analysis_ids: list[str] = []
    for query_id, gold in local_refs:
        analysis_ids.append(_text(gold["chunk_id"]))
        analysis_ids.extend(
            _text(item.get("chunk_id"))
            for item in records_by_query[query_id]["saved_hits"]
            if item.get("chunk_id")
        )
    unique_ids = list(dict.fromkeys(analysis_ids))
    vectors = list(embedder.embed([_text(chunks_by_id.get(chunk_id, {}).get("content")) for chunk_id in unique_ids]))
    vector_by_id = {chunk_id: vector for chunk_id, vector in zip(unique_ids, vectors, strict=True)}
    diagnostics: list[dict[str, Any]] = []
    category_counts = Counter()
    for query_id, gold in local_refs:
        record = records_by_query[query_id]
        gold_id = _text(gold["chunk_id"])
        gold_row = chunks_by_id.get(gold_id, {})
        gold_content = _text(gold_row.get("content"))
        top10 = [_decorate_hit(item, chunks_by_id) for item in record["saved_hits"][:OUTPUT_DEPTH]]
        top10_dup = _duplicate_stats(top10)
        same_post_count = sum(item["post_id"] == _text(gold["post_id"]) for item in top10)
        micro_top10_count = sum(item["length"] < 50 for item in top10)
        duplicate_to_gold = sum(_is_duplicate(item, {**gold_row, "content": gold_content}) for item in top10 if item["chunk_id"] != gold_id)
        if len(gold_content.strip()) < 50 or micro_top10_count >= 3:
            category = "MICRO_CHUNK_NOISE"
        elif duplicate_to_gold or top10_dup["duplicate_equivalent_result_count"]:
            category = "DUPLICATE_CHUNK_NOISE"
        elif same_post_count >= 3:
            category = "SAME_POST_FRAGMENT_COMPETITION"
        elif len(gold_content.strip()) < 100:
            category = "GOLD_CHUNK_TOO_FRAGMENTED"
        else:
            category = "GENUINE_SEMANTIC_RANKING_FAILURE"
        category_counts[category] += 1
        competitors: list[dict[str, Any]] = []
        for item in top10:
            item_id = item["chunk_id"]
            competitors.append(
                {
                    "chunk_id": item_id,
                    "post_id": item["post_id"],
                    "chunk_index": item["chunk_index"],
                    "length": item["length"],
                    "query_score": round(float(item.get("score") or 0.0), 8),
                    "same_post_as_gold": item["post_id"] == _text(gold["post_id"]),
                    "is_gold": item_id == gold_id,
                    "duplicate_or_near_duplicate_to_gold": _is_duplicate(
                        item, {**gold_row, "content": gold_content}
                    ),
                    "semantic_similarity_to_gold": round(
                        _cosine(vector_by_id[item_id], vector_by_id[gold_id]), 6
                    )
                    if item_id in vector_by_id and gold_id in vector_by_id
                    else 0.0,
                    "lexical_similarity_to_gold": _lexical_similarity(item.get("content"), gold_content),
                    "text_summary": _summary(item.get("content")),
                }
            )
        full_rank = next(
            (
                int(item.get("rank") or index)
                for index, item in enumerate(record["full_hits"], 1)
                if _text(item.get("chunk_id")) == gold_id
            ),
            None,
        )
        diagnostics.append(
            {
                "query_id": query_id,
                "query": record["query"],
                "gold_chunk_id": gold_id,
                "gold_post_id": _text(gold["post_id"]),
                "gold_chunk_index": int(gold["chunk_index"]),
                "gold_chunk_length": len(gold_content.strip()),
                "gold_chunk_rank": gold.get("gold_chunk_rank"),
                "full_candidate_rank": full_rank,
                "top10_chunk_lengths": [item["length"] for item in top10],
                "top10_same_post_count": same_post_count,
                "top10_micro_chunk_count": micro_top10_count,
                "duplicate_equivalent_result_count": top10_dup["duplicate_equivalent_result_count"],
                "duplicate_equivalent_pair_count": top10_dup["duplicate_equivalent_pair_count"],
                "duplicate_to_gold_count": duplicate_to_gold,
                "classification": category,
                "competitors": competitors,
            }
        )
    total = len(diagnostics)
    return diagnostics, {
        "total": total,
        "categories": {
            category: {"count": category_counts.get(category, 0), "rate": _safe_rate(category_counts.get(category, 0), total)}
            for category in LOCAL_FAILURE_CATEGORIES
        },
        "rules": {
            "MICRO_CHUNK_NOISE": "gold length <50 OR at least three selected chunks are <50",
            "DUPLICATE_CHUNK_NOISE": "a selected competitor duplicates gold or selected slots contain duplicate-equivalent content",
            "SAME_POST_FRAGMENT_COMPETITION": "at least three selected chunks belong to the gold post",
            "GOLD_CHUNK_TOO_FRAGMENTED": "gold length is 50-99 after the preceding rules",
            "GENUINE_SEMANTIC_RANKING_FAILURE": "none of the corpus-noise heuristics applies",
        },
    }


def _duplicate_competition(
    records: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    corpus_audit: dict[str, Any],
) -> dict[str, Any]:
    per_query: list[dict[str, Any]] = []
    total_slots = 0
    total_waste = 0
    for record in records:
        selected = [_decorate_hit(item, chunks_by_id) for item in record["saved_hits"][:OUTPUT_DEPTH]]
        duplicate = _duplicate_stats(selected)
        counts = Counter(item["post_id"] for item in selected)
        same_post_slots = sum(max(0, value - 1) for value in counts.values())
        unique_semantic = len(selected) - duplicate["duplicate_equivalent_result_count"]
        total_slots += len(selected)
        total_waste += duplicate["duplicate_equivalent_result_count"]
        per_query.append(
            {
                "query_id": record["query_id"],
                "query": record["query"],
                "selected_count": len(selected),
                "unique_semantic_evidence_count": unique_semantic,
                "duplicate_equivalent_result_count": duplicate["duplicate_equivalent_result_count"],
                "duplicate_equivalent_pair_count": duplicate["duplicate_equivalent_pair_count"],
                "same_post_chunk_count": same_post_slots,
                "dominant_post_slots": max(counts.values(), default=0),
                "slot_waste_rate": _safe_rate(duplicate["duplicate_equivalent_result_count"], len(selected)),
            }
        )
    return {
        "corpus_exact_duplicate_group_count": corpus_audit["chunk_corpus"]["duplicate_chunk_group_count"],
        "corpus_near_duplicate_pair_count": corpus_audit["chunk_corpus"]["near_duplicate_pair_count"],
        "total_selected_slots": total_slots,
        "duplicate_equivalent_slots": total_waste,
        "top10_slot_waste_rate": _safe_rate(total_waste, total_slots),
        "queries_with_duplicate_equivalent_slots": sum(
            item["duplicate_equivalent_result_count"] > 0 for item in per_query
        ),
        "queries": per_query,
    }


def _rank_metrics_table(results: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        metrics = result["metrics"][scope]
        rows.append(
            {
                "strategy": result["strategy"],
                "conditional_recall_at5": metrics["conditional_chunk_recall_at5"],
                "conditional_recall_at10": metrics["conditional_chunk_recall_at10"],
                "final_evidence_recall_at10": metrics["final_evidence_recall_at10"],
                "chunk_mrr": metrics["chunk_mrr"],
                "hit_at1": metrics["hit_at"]["1"]["overall"],
                "hit_at3": metrics["hit_at"]["3"]["overall"],
                "hit_at5": metrics["hit_at"]["5"]["overall"],
                "hit_at10": metrics["hit_at"]["10"]["overall"],
                "avg_candidates": metrics["avg_candidates"],
                "duplicate_slot_waste": metrics["duplicate_slot_waste"],
                "gold_removed_rate": metrics["gold_removed_rate"],
                "rank_latency_p95_ms": metrics["rank_latency_ms"]["p95"],
                "preparation_ms": result["preparation_ms"],
                "complexity": result["complexity"],
            }
        )
    return rows


def _choose_strategy(results: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    baseline = next(result for result in results if result["strategy"] == "CURRENT_BASELINE")
    baseline_all = baseline["metrics"]["all"]
    baseline_strong = baseline["metrics"]["strong_coverage_only"]
    evaluated: list[dict[str, Any]] = []
    for result in results:
        if result["strategy"] == "CURRENT_BASELINE":
            continue
        all_metrics = result["metrics"]["all"]
        strong_metrics = result["metrics"]["strong_coverage_only"]
        acceptance = {
            "all_conditional_recall_gain": (all_metrics["conditional_chunk_recall_at10"] or 0.0)
            >= (baseline_all["conditional_chunk_recall_at10"] or 0.0) + 0.02,
            "strong_conditional_recall_gain": (strong_metrics["conditional_chunk_recall_at10"] or 0.0)
            >= (baseline_strong["conditional_chunk_recall_at10"] or 0.0) + 0.02,
            "all_final_recall_gain": (all_metrics["final_evidence_recall_at10"] or 0.0)
            >= (baseline_all["final_evidence_recall_at10"] or 0.0) + 0.02,
            "mrr_gain": (all_metrics["chunk_mrr"] or 0.0) > (baseline_all["chunk_mrr"] or 0.0),
            "gold_removed_near_zero": (all_metrics["gold_removed_rate"] or 0.0) <= 0.01
            and (strong_metrics["gold_removed_rate"] or 0.0) <= 0.01,
            "duplicate_waste_reduced": all_metrics["duplicate_slot_waste"]
            < baseline_all["duplicate_slot_waste"],
            "fixed_top10_posts": True,
        }
        result["acceptance"] = acceptance
        evaluated.append(result)
    accepted = [result for result in evaluated if all(result["acceptance"].values())]
    chosen = max(
        accepted,
        key=lambda result: (
            result["metrics"]["strong_coverage_only"]["conditional_chunk_recall_at10"] or 0.0,
            result["metrics"]["all"]["chunk_mrr"] or 0.0,
        ),
        default=None,
    )
    return chosen, {
        "thresholds": {
            "recall_gain": 0.02,
            "gold_removed_rate_max": 0.01,
        },
        "accepted_strategy_count": len(accepted),
        "accepted_strategies": [result["strategy"] for result in accepted],
    }


def _render_report(output: dict[str, Any]) -> str:
    construction = output["chunk_construction"]
    chunk_stats = output["chunk_corpus"]
    all_metrics = output["strategy_metrics_tables"]["ALL"]
    strong_metrics = output["strategy_metrics_tables"]["STRONG_COVERAGE_ONLY"]

    def metric_rows(rows: list[dict[str, Any]]) -> str:
        return "\n".join(
            "| {strategy} | {c5:.6f} | {c10:.6f} | {f10:.6f} | {mrr:.6f} | "
            "{h1:.6f} | {h3:.6f} | {h5:.6f} | {h10:.6f} | {avg:.2f} | "
            "{waste:.4f} | {removed:.4f} | {latency:.3f} | {complexity} |".format(
                strategy=row["strategy"],
                c5=row["conditional_recall_at5"] or 0.0,
                c10=row["conditional_recall_at10"] or 0.0,
                f10=row["final_evidence_recall_at10"] or 0.0,
                mrr=row["chunk_mrr"] or 0.0,
                h1=row["hit_at1"] or 0.0,
                h3=row["hit_at3"] or 0.0,
                h5=row["hit_at5"] or 0.0,
                h10=row["hit_at10"] or 0.0,
                avg=row["avg_candidates"] or 0.0,
                waste=row["duplicate_slot_waste"] or 0.0,
                removed=row["gold_removed_rate"] or 0.0,
                latency=row["rank_latency_p95_ms"] or 0.0,
                complexity=row["complexity"],
            )
            for row in rows
        )

    length_rows = "\n".join(
        f"| {name} | {value['count']} | {value['rate']:.4f} | "
        f"{chunk_stats['gold_length_buckets'][name]['count']} | "
        f"{chunk_stats['gold_length_buckets'][name]['rate']:.4f} |"
        for name, value in chunk_stats["length_buckets"].items()
    )
    gold_length_rows = "\n".join(
        f"| {name} | {value['count']} | {value['rate']:.4f} | {value['hit_at10'] or 0.0:.6f} | "
        f"{value['mrr'] or 0.0:.6f} | {value['post_present_hit_at10'] or 0.0:.6f} |"
        for name, value in output["gold_chunk_audit"]["bucket_retrieval_metrics"].items()
    )
    failure_rows = "\n".join(
        f"| `{name}` | {value['count']} | {value['rate']:.4f} |"
        for name, value in output["local_failure_audit"]["categories"].items()
    )
    failure_corpus = output["retrieval_failure_correlation"]
    verdict = output["verdict"]
    chosen = output["chosen_strategy"] or "None"
    acceptance_text = json.dumps(output["acceptance_summary"], ensure_ascii=False, sort_keys=True)
    v2_decision = (
        "YES — a merge/rechunk strategy passed the gate and a versioned v2 collection may be designed."
        if output["v2_collection_justified"]
        else "NO — the winning strategy is an eligibility filter; it does not justify a new v2 collection."
    )
    return f"""# RAG_CHUNK_CORPUS_QUALITY_V4

Baseline checkpoint: `{output['baseline_checkpoint']}`
Frozen RAG diagnosis checkpoint: `{output['rag_diagnosis_checkpoint']}`
Collection: `{COLLECTION}` (read-only)

## Verdict

`{verdict}`

## Actual PostChunker behavior

The production `PostChunker` uses `maxChars={MAX_CHARS}` and
`overlapChars={OVERLAP_CHARS}` (the constructor clamps the minimum max size to
64 and overlap to at most half the max). It recognizes a paragraph boundary
only after a whitespace run containing at least two newline characters. A
short paragraph is emitted directly as its own chunk; overlap is used only
when one paragraph exceeds the max size. Chunk IDs are stable by
`post_id + chunk_index`, and offsets are source-text offsets.

Projection uses `content_object_key → OssStorageService.readTextObject →
PostSearchDocumentService.build → PostChunkProjectionService → PostChunker`.
Chunk embedding text remains labelled `title + tags + description + content`;
the chunk content remains the final evidence unit.

| Reproduction check | Value |
|---|---:|
| Posts compared | {construction['posts_compared']} |
| Exact PostChunker reproductions | {construction['exact_post_count']} |
| Posts with any reproduction mismatch | {construction['mismatch_post_count']} |
| Mean body characters | {construction['mean_body_chars']:.3f} |
| Body p50 characters | {construction['p50_body_chars']} |
| Mean stored chunks/post | {construction['mean_chunks_per_post']:.3f} |
| Stored chunks/post p50 / p95 / max | {construction['chunks_per_post_p50']} / {construction['chunks_per_post_p95']} / {construction['chunks_per_post_max']} |
| Mean paragraph count/post | {construction['mean_paragraphs_per_post']:.3f} |
| Short paragraphs (<100) | {construction['short_paragraph_count_lt100']} |
| Micro paragraphs (<50) | {construction['micro_paragraph_count_lt50']} |
| Paragraphs split into multiple chunks | {construction['multi_chunk_paragraph_count']} |

The approximately 1700-character body median coexists with approximately 26
chunks/post because the splitter is paragraph-first and has no minimum chunk
length or short-paragraph merge. The live corpus contains many short Markdown
paragraphs; each becomes an independent retrieval point. The 160-character
overlap is not the explanation for the high chunk count because it applies
only inside an oversized paragraph.

## Chunk length distribution

| Corpus measure | Value |
|---|---:|
| MySQL chunk rows | {chunk_stats['mysql_chunk_count']} |
| Qdrant points | {chunk_stats['qdrant_point_count']} |
| Unique chunk posts | {chunk_stats['unique_post_count']} |
| Posts with 0 chunks | {len(chunk_stats['posts_with_zero_chunks'])} |
| Posts with 1 chunk | {len(chunk_stats['posts_with_one_chunk'])} |
| Empty chunks | {chunk_stats['empty_count']} |
| Exact duplicate groups | {output['duplicate_competition']['corpus_exact_duplicate_group_count']} |
| Near-duplicate pairs | {output['duplicate_competition']['corpus_near_duplicate_pair_count']} |
| Available-body posts with 0 chunks | {len(chunk_stats['posts_with_zero_chunks'])} |

| Bucket | All chunks | All rate | 75 gold chunks | Gold rate |
|---|---:|---:|---:|---:|
{length_rows}

| Statistic | All 5040 chunks | 75 unique gold chunks |
|---|---:|---:|
| min | {chunk_stats['all_distribution']['min']} | {chunk_stats['gold_distribution']['min']} |
| p25 | {chunk_stats['all_distribution']['p25']} | {chunk_stats['gold_distribution']['p25']} |
| p50 | {chunk_stats['all_distribution']['p50']} | {chunk_stats['gold_distribution']['p50']} |
| p75 | {chunk_stats['all_distribution']['p75']} | {chunk_stats['gold_distribution']['p75']} |
| p95 | {chunk_stats['all_distribution']['p95']} | {chunk_stats['gold_distribution']['p95']} |
| mean | {chunk_stats['all_distribution']['mean']} | {chunk_stats['gold_distribution']['mean']} |
| max | {chunk_stats['all_distribution']['max']} | {chunk_stats['gold_distribution']['max']} |

## Gold evidence × chunk size

The following is reference-level over 104 gold references. `Hit@10` and MRR
use the frozen current Top10 result; `post-present Hit@10` excludes gold refs
whose parent post was not in the frozen Top10 post pool.

| Bucket | Gold refs | Rate | Hit@10 | MRR | Post-present Hit@10 |
|---|---:|---:|---:|---:|---:|
{gold_length_rows}

Gold reference length distribution: `{output['gold_chunk_audit']['reference_length_distribution']}`.
The detailed 104-reference records, including lengths and ranks, are in the
JSON artifact.

## LOCAL_RANKING_FAILURE breakdown

The audit covers the 48 baseline `LOCAL_RANKING_FAILURE` references. Rules are
mutually exclusive and intentionally use only corpus/selection observables;
the full per-case trace includes competing text summaries, query score,
lexical similarity, and embedding similarity to the gold chunk.
The size-oriented categories (`MICRO_CHUNK_NOISE` plus
`GOLD_CHUNK_TOO_FRAGMENTED`) account for
`{output['local_failure_audit']['categories']['MICRO_CHUNK_NOISE']['count'] + output['local_failure_audit']['categories']['GOLD_CHUNK_TOO_FRAGMENTED']['count']}`
of 48 local failures; this is evidence of chunk-level noise, not proof that
all such cases require re-chunking.

| Category | Count | Rate |
|---|---:|---:|
{failure_rows}

## Duplicate competition

The live chunk audit found
`{output['duplicate_competition']['corpus_exact_duplicate_group_count']}` exact duplicate groups and
`{output['duplicate_competition']['corpus_near_duplicate_pair_count']}` heuristic near-duplicate pairs.
For the frozen baseline Top10 output, duplicate-equivalent content occupied
`{output['duplicate_competition']['duplicate_equivalent_slots']}` of
`{output['duplicate_competition']['total_selected_slots']}` slots, giving a
slot-waste rate of `{output['duplicate_competition']['top10_slot_waste_rate']:.4f}`.
Per-query unique semantic evidence, duplicate-equivalent results, and same-post
competition counts are in the JSON artifact.

## Offline eligibility experiments

All experiments used the same frozen Top10 post candidates and current
`{COLLECTION}` scores/model. No production collection was rebuilt. `Avg
candidates` is the pre-output candidate pool per query; output remains bounded
to 10 chunks. `Gold removed` is conditional on the gold parent post being in
the frozen Top10 pool.

### ALL answerable queries

| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Dup waste | Gold removed | Rank p95 ms | Complexity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
{metric_rows(all_metrics)}

### STRONG_COVERAGE_ONLY

| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Dup waste | Gold removed | Rank p95 ms | Complexity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
{metric_rows(strong_metrics)}

Experiments tested: current baseline; micro-chunk filters at `<30`, `<50`,
`<80`, and `<100`; adjacent-short-chunk merge targets of 120 and 240
characters; duplicate/near-duplicate representative selection; and merge-120
plus merge-240 deduplication. Merge simulation preserves source chunk IDs and
source offset span so gold evidence can be traced back to the original corpus.

## Acceptance and winning strategy

The offline acceptance rule uses a meaningful gain threshold of +0.02 for both
ALL and STRONG conditional Recall@10, +0.02 for ALL Final Evidence Recall@10,
strict MRR improvement, gold removal ≤1%, reduced duplicate slot waste, and
fixed Top10 post candidates.

Chosen strategy: `{chosen}`.
Acceptance summary: `{acceptance_text}`

## Retrieval failure correlation

`LOCAL_RANKING_FAILURE` remains the dominant family: 48/78 missed references,
all from `GOOD_CORPUS` posts in the prior corpus audit. The V4 chunk-noise
classification is a diagnosis of the selected competitors, not a replacement
for that frozen failure-family label.

| Failure family | Missed refs | Good-corpus refs | Other-corpus refs |
|---|---:|---:|---:|
{chr(10).join(f"| `{family}` | {value['total']} | {value['good_corpus']} | {value['other_corpus']} |" for family, value in failure_corpus.items())}

## Exact FIRST_BAD_STATE

`POST_RETRIEVAL → CHUNK_RETRIEVAL`

The audit does not move the first bad state to evidence selection or
generation. MySQL and Qdrant projection identities remain aligned; this V4
phase only tests whether the chunk corpus causes candidate noise.

## v2 collection decision

`{v2_decision}` No `post_chunks_multilingual_v2` collection was created or
rebuilt. The accepted `<50` filter can be considered as a minimal retrieval
eligibility policy; it does not require a new collection. A versioned v2
collection remains unjustified unless a merge/rechunk strategy separately
passes the gate.

## Next recommendation

Keep `{COLLECTION}` and production retrieval unchanged in this phase. Treat
paragraph-first construction without short-paragraph merging as the primary
corpus-quality hypothesis, and keep the `<50` filter as an offline eligibility
hypothesis rather than a production setting. Before authorizing a versioned v2
rebuild, pin the retrieval snapshot and validate one bounded merge/section-aware
projection whose deduplication preserves every gold source span; no v2
migration is authorized by the current acceptance results.

## Production / dirty state

Production files changed: `[]`. No Memory, ActionLoop, Durable Runtime, MCP,
Hybrid Search, Java business truth, generator, or evidence selection code was
modified. The V4 script and result/report artifacts are evaluation-only.

Baseline audit commit already saved and pushed: `722a072` (`test: add rag
corpus quality audit`). V4 artifacts are intentionally left dirty for review.

## Verdict detail

- Dataset: 50 queries / 45 answerable / 104 gold refs / 75 unique gold chunks.
- Full frozen per-case retrieval traces: {len(output['retrieval_traces'])}.
- Qdrant collection: `{COLLECTION}`, read-only; no rebuild.
- Embedding: `{MODEL_NAME}`.
- Query snapshot drift against frozen V3 Top10: `{output['snapshot_drift_count']}`
  ({', '.join(output['snapshot_drift_queries']) or 'none'}).
- `CURRENT_BASELINE` preserves the frozen V3 Top10 selection; simulated
  strategies use the fresh read-only full candidate ranking for the same frozen
  Top10 post pool. The snapshot drift is retained as a comparability caveat.
- No tests beyond the offline harness, Ruff, and syntax validation were run in
  this phase.

`RAG_CHUNK_CORPUS_QUALITY_V4_COMPLETE`
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("docs/evaluation/rag_evidence_dataset_v2.jsonl"))
    parser.add_argument("--chunk-fixture", type=Path, default=Path("docs/evaluation/rag_evidence_chunk_fixture_v2.json"))
    parser.add_argument("--diagnosis", type=Path, default=Path("docs/evaluation/rag_chunk_retrieval_v3_diagnosis.json"))
    parser.add_argument("--corpus-audit", type=Path, default=Path("docs/evaluation/rag_corpus_quality_audit.json"))
    parser.add_argument("--storage-root", type=Path, default=None)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    parser.add_argument("--mysql-host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--mysql-port", type=int, default=int(os.getenv("MYSQL_PORT", "33306")))
    parser.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", "123456"))
    parser.add_argument("--mysql-database", default=os.getenv("MYSQL_DB", "zhiguang"))
    parser.add_argument("--embedding-model", default=MODEL_NAME)
    parser.add_argument("--embedding-cache", default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=Path("docs/evaluation/rag_chunk_corpus_v4_results.json"))
    parser.add_argument("--report", type=Path, default=Path("docs/reports/RAG_CHUNK_CORPUS_QUALITY_V4.md"))
    args = parser.parse_args()

    validation = validate(args.dataset, args.chunk_fixture)
    if validation["status"] != "VALID":
        raise SystemExit(json.dumps({"verdict": "DATASET_ISSUE", "validation": validation}, ensure_ascii=False))
    audit = json.loads(args.corpus_audit.read_text(encoding="utf-8"))
    diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
    traces_by_query = {
        _text(trace["query_id"]): trace
        for trace in diagnosis.get("traces", [])
        if isinstance(trace, dict) and trace.get("query_id")
    }
    rows = [row for row in validation["valid_rows"] if row.get("category") != "no_answer"]
    rows_by_query = {_text(row["query_id"]): row for row in rows}
    if set(rows_by_query) != set(traces_by_query):
        raise RuntimeError("frozen V3 trace query set does not match answerable dataset")

    storage_root = (args.storage_root or Path(audit["runtime"]["storage_root"])).resolve()
    posts_by_id, body_by_post = _make_content_maps(audit, storage_root)
    all_post_ids = sorted(posts_by_id)
    all_chunks = _mysql_chunks(
        args.mysql_host,
        args.mysql_port,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
        all_post_ids,
    )
    chunks_by_id = {_text(row["chunk_id"]): row for row in all_chunks}
    chunks_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_chunks:
        chunks_by_post[_text(row["post_id"])].append(row)
    for values in chunks_by_post.values():
        values.sort(key=lambda row: int(row["chunk_index"]))

    reproduction = [
        _reproduce_post(post_id, body_by_post.get(post_id, ""), chunks_by_post.get(post_id, []))
        for post_id in all_post_ids
    ]
    construction = {
        "max_chars": MAX_CHARS,
        "overlap_chars": OVERLAP_CHARS,
        "posts_compared": len(reproduction),
        "exact_post_count": sum(item["reproduction_exact"] for item in reproduction),
        "mismatch_post_count": sum(not item["reproduction_exact"] for item in reproduction),
        "mean_body_chars": statistics.fmean(item["body_chars"] for item in reproduction) if reproduction else 0.0,
        "p50_body_chars": audit["live_corpus"]["content_length_distribution"]["p50"],
        "mean_chunks_per_post": statistics.fmean(item["stored_chunk_count"] for item in reproduction) if reproduction else 0.0,
        "chunks_per_post_p50": audit["chunk_corpus"]["chunks_per_post"]["p50"],
        "chunks_per_post_p95": audit["chunk_corpus"]["chunks_per_post"]["p95"],
        "chunks_per_post_max": audit["chunk_corpus"]["chunks_per_post"]["max"],
        "mean_paragraphs_per_post": statistics.fmean(item["paragraph_count"] for item in reproduction) if reproduction else 0.0,
        "short_paragraph_count_lt100": sum(item["short_paragraph_count_lt100"] for item in reproduction),
        "micro_paragraph_count_lt50": sum(item["micro_paragraph_count_lt50"] for item in reproduction),
        "multi_chunk_paragraph_count": sum(item["multi_chunk_paragraph_count"] for item in reproduction),
        "per_post": reproduction,
    }

    live_chunk_lengths = [len(_text(row.get("content")).strip()) for row in all_chunks]
    gold_chunk_ids = {
        _text(item["chunk_id"])
        for row in rows
        for item in row.get("gold_chunks", [])
        if isinstance(item, dict)
    }
    gold_chunk_lengths = [len(_text(chunks_by_id.get(chunk_id, {}).get("content")).strip()) for chunk_id in gold_chunk_ids]
    chunk_corpus = {
        "mysql_chunk_count": len(all_chunks),
        "qdrant_point_count": audit["chunk_corpus"]["qdrant_point_count"],
        "unique_post_count": len(chunks_by_post),
        "all_distribution": _length_distribution(live_chunk_lengths),
        "gold_distribution": _length_distribution(gold_chunk_lengths),
        "length_buckets": _bucket_counts(live_chunk_lengths),
        "gold_length_buckets": _bucket_counts(gold_chunk_lengths),
        "empty_count": sum(not _text(row.get("content")).strip() for row in all_chunks),
        "tiny_lt50_count": sum(value < 50 for value in live_chunk_lengths),
        "posts_with_zero_chunks": [post_id for post_id in all_post_ids if not chunks_by_post.get(post_id)],
        "posts_with_one_chunk": [post_id for post_id in all_post_ids if len(chunks_by_post.get(post_id, [])) == 1],
        "projection_content_mismatch_count": audit["chunk_corpus"].get("projection_content_mismatch_count", 0),
        "mysql_equals_qdrant_chunks": audit["chunk_corpus"].get("mysql_equals_qdrant_chunks", False),
    }

    gold_chunk_audit, gold_refs = _gold_length_metrics(rows, chunks_by_id, traces_by_query)
    candidate_post_ids = {
        _text(item["post_id"])
        for trace in traces_by_query.values()
        for item in trace.get("candidate_posts", [])
        if isinstance(item, dict) and item.get("post_id")
    }
    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise SystemExit(
            "fastembed is required; run with uv --with fastembed --with onnxruntime==1.20.1"
        ) from error
    embedder = TextEmbedding(model_name=args.embedding_model, cache_dir=args.embedding_cache)
    query_vectors = list(embedder.embed([_text(row["query"]) for row in rows]))
    records: list[dict[str, Any]] = []
    snapshot_drift_count = 0
    snapshot_drift_queries: list[str] = []
    for row, query_vector in zip(rows, query_vectors, strict=True):
        query_id = _text(row["query_id"])
        trace = traces_by_query[query_id]
        candidate_posts = [
            _text(item["post_id"])
            for item in trace.get("candidate_posts", [])
            if isinstance(item, dict) and item.get("post_id")
        ][:BASELINE_DEPTH]
        saved_hits = [
            {
                "chunk_id": _text(item["chunk_id"]),
                "post_id": _text(item["post_id"]),
                "chunk_index": int(item.get("chunk_index") or 0),
                "score": float(item.get("score") or 0.0),
                "rank": index,
            }
            for index, item in enumerate(trace.get("candidate_chunks", [])[:OUTPUT_DEPTH], 1)
            if isinstance(item, dict) and item.get("chunk_id")
        ]
        full_hits = _qdrant_search(args.qdrant_url, [float(value) for value in query_vector], candidate_posts, limit=1000)
        if [item["chunk_id"] for item in full_hits[:OUTPUT_DEPTH]] != [item["chunk_id"] for item in saved_hits]:
            snapshot_drift_count += 1
            snapshot_drift_queries.append(query_id)
        records.append(
            {
                "query_id": query_id,
                "query": _text(row["query"]),
                "query_vector": query_vector,
                "candidate_posts": candidate_posts,
                "gold_post_ids": [_text(value) for value in row.get("gold_post_ids", [])],
                "gold_chunks": row.get("gold_chunks", []),
                "saved_hits": saved_hits,
                "full_hits": full_hits,
            }
        )
    records_by_query = {record["query_id"]: record for record in records}

    full_items_by_query = {
        record["query_id"]: [_decorate_hit(hit, chunks_by_id) for hit in record["full_hits"]]
        for record in records
    }
    strong_query_ids = {
        query_id
        for query_id, value in audit["knowledge_coverage"]["queries"].items()
        if value.get("coverage") == "STRONG_COVERAGE"
    }
    all_query_ids = set(records_by_query)

    strategy_runs: list[dict[str, Any]] = []
    strategy_specs: list[tuple[str, str, Any]] = []
    strategy_specs.append(
        (
            "CURRENT_BASELINE",
            "current frozen Top10; full fixed-post candidate pool",
            lambda record: (
                full_items_by_query[record["query_id"]],
                [_decorate_hit(hit, chunks_by_id) for hit in record["saved_hits"]],
            ),
        )
    )
    for threshold in FILTER_THRESHOLDS:
        strategy_specs.append(
            (
                f"MICRO_CHUNK_FILTER_LT{threshold}",
                f"filter content length < {threshold}; rerank current full hits",
                lambda record, threshold=threshold: (
                    [item for item in full_items_by_query[record["query_id"]] if item["length"] >= threshold],
                    [item for item in full_items_by_query[record["query_id"]] if item["length"] >= threshold],
                ),
            )
        )
    merge_preparation_ms: dict[int, float] = {}
    merge_items_by_target: dict[int, list[dict[str, Any]]] = {}
    merge_vectors_by_target: dict[int, dict[str, Any]] = {}
    for target in MERGE_TARGETS:
        started = time.perf_counter()
        merged_items = _merged_corpus(candidate_post_ids, chunks_by_post, body_by_post, target)
        merged_vectors = _build_merge_vectors(embedder, merged_items)
        preparation_ms = (time.perf_counter() - started) * 1000
        merge_preparation_ms[target] = preparation_ms
        merge_items_by_target[target] = merged_items
        merge_vectors_by_target[target] = merged_vectors
        strategy_specs.append(
            (
                f"MERGE_SHORT_TO_{target}",
                f"merge adjacent chunks while current span < {target}, max merged span {MAX_CHARS}",
                lambda record, merged_items=merged_items, merged_vectors=merged_vectors: (
                    [item for item in merged_items if item["post_id"] in set(record["candidate_posts"])],
                    _rank_merged(record, merged_items, merged_vectors),
                ),
            )
        )
    merge_120_items = merge_items_by_target[120]
    merge_120_vectors = merge_vectors_by_target[120]
    merge_240_items = merge_items_by_target[240]
    merge_240_vectors = merge_vectors_by_target[240]
    strategy_specs.append(
        (
            "DEDUP_EXACT_NEAR",
            "greedy representative selection for exact/heuristic near duplicates",
            lambda record: (
                _deduplicate(full_items_by_query[record["query_id"]]),
                _deduplicate(full_items_by_query[record["query_id"]]),
            ),
        )
    )
    strategy_specs.append(
        (
            "MERGE_120_PLUS_DEDUP",
            "merge short chunks to 120 then exact/near duplicate representative selection",
            lambda record: (
                _deduplicate([item for item in merge_120_items if item["post_id"] in set(record["candidate_posts"])]),
                _deduplicate(_rank_merged(record, merge_120_items, merge_120_vectors)),
            ),
        )
    )
    strategy_specs.append(
        (
            "MERGE_240_PLUS_DEDUP",
            "merge short chunks to 240 then exact/near duplicate representative selection",
            lambda record: (
                _deduplicate([item for item in merge_240_items if item["post_id"] in set(record["candidate_posts"])]),
                _deduplicate(_rank_merged(record, merge_240_items, merge_240_vectors)),
            ),
        )
    )
    for name, complexity, ranker in strategy_specs:
        prep = (
            merge_preparation_ms[120]
            if name in {"MERGE_SHORT_TO_120", "MERGE_120_PLUS_DEDUP"}
            else merge_preparation_ms[240]
            if name in {"MERGE_SHORT_TO_240", "MERGE_240_PLUS_DEDUP"}
            else 0.0
        )
        strategy_runs.append(
            _evaluate_strategy(name, records, ranker, complexity, prep)
        )

    for run in strategy_runs:
        run["metrics"] = {
            "all": _evaluate_scope(records_by_query, run, all_query_ids),
            "strong_coverage_only": _evaluate_scope(records_by_query, run, strong_query_ids),
        }
    chosen_run, acceptance_summary = _choose_strategy(strategy_runs)
    chosen_strategy = chosen_run["strategy"] if chosen_run else None
    verdict = "RAG_CHUNK_CORPUS_QUALITY_ISSUE" if chosen_run else "RAG_CHUNK_CORPUS_NO_GAIN"

    local_diagnostics, local_summary = _local_failure_diagnostics(
        records_by_query, traces_by_query, chunks_by_id, embedder
    )
    duplicate_competition = _duplicate_competition(records, chunks_by_id, audit)
    post_quality_by_id = {
        _text(item["post_id"]): item["quality"]
        for item in audit["gold_corpus"]["posts"]
        if isinstance(item, dict) and item.get("post_id")
    }
    retrieval_failure_correlation: dict[str, dict[str, int]] = {}
    for trace in traces_by_query.values():
        for item in trace.get("gold_chunks", []):
            if not isinstance(item, dict) or item.get("failure_family") == "HIT":
                continue
            family = _text(item.get("failure_family") or "UNKNOWN")
            quality = post_quality_by_id.get(_text(item.get("post_id")), "UNKNOWN")
            entry = retrieval_failure_correlation.setdefault(
                family,
                {"total": 0, "good_corpus": 0, "other_corpus": 0},
            )
            entry["total"] += 1
            entry["good_corpus" if quality == "GOOD_CORPUS" else "other_corpus"] += 1

    output = {
        "baseline_checkpoint": BASELINE_CHECKPOINT,
        "rag_diagnosis_checkpoint": RAG_DIAGNOSIS_CHECKPOINT,
        "verdict": verdict,
        "dataset": {
            "query_count": 50,
            "answerable_query_count": len(rows),
            "gold_reference_count": len(gold_refs),
            "unique_gold_chunk_count": len(gold_chunk_ids),
            "validation_status": validation["status"],
        },
        "runtime": {
            "qdrant_url": args.qdrant_url,
            "qdrant_collection": COLLECTION,
            "qdrant_read_only": True,
            "storage_root": str(storage_root),
            "embedding_model": args.embedding_model,
            "embedding_cache": args.embedding_cache,
            "production_files_changed": [],
            "collection_rebuilt": False,
        },
        "chunk_construction": construction,
        "chunk_corpus": chunk_corpus,
        "gold_chunk_audit": gold_chunk_audit,
        "gold_references": gold_refs,
        "local_failure_audit": local_summary,
        "local_failure_cases": local_diagnostics,
        "retrieval_traces": [traces_by_query[_text(row["query_id"])] for row in rows],
        "duplicate_competition": duplicate_competition,
        "retrieval_failure_correlation": retrieval_failure_correlation,
        "snapshot_drift_count": snapshot_drift_count,
        "snapshot_drift_queries": snapshot_drift_queries,
        "strategies": [_compact_strategy_run(run) for run in strategy_runs],
        "strategy_metrics_tables": {
            "ALL": _rank_metrics_table(strategy_runs, "all"),
            "STRONG_COVERAGE_ONLY": _rank_metrics_table(strategy_runs, "strong_coverage_only"),
        },
        "acceptance_summary": acceptance_summary,
        "chosen_strategy": chosen_strategy,
        "v2_collection_justified": bool(chosen_strategy and chosen_strategy.startswith("MERGE_")),
        "metric_definitions": {
            "conditional_recall": "mean query-level recall over gold refs whose parent post is in frozen Top10",
            "final_evidence_recall": "mean query-level recall over all answerable gold refs",
            "chunk_mrr": "mean reciprocal rank of the first selected item covering any query gold ref",
            "gold_removed_rate": "conditional gold refs absent from the simulated pre-output candidate pool",
            "duplicate_slot_waste": "selected Top10 slots equivalent to an earlier selected content item",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": verdict,
                "chosen_strategy": chosen_strategy,
                "chunk_construction": {
                    key: construction[key]
                    for key in (
                        "posts_compared",
                        "exact_post_count",
                        "mean_body_chars",
                        "mean_chunks_per_post",
                        "mean_paragraphs_per_post",
                        "short_paragraph_count_lt100",
                        "micro_paragraph_count_lt50",
                    )
                },
                "chunk_corpus": {
                    key: chunk_corpus[key]
                    for key in (
                        "mysql_chunk_count",
                        "unique_post_count",
                        "all_distribution",
                        "gold_distribution",
                        "length_buckets",
                        "gold_length_buckets",
                        "empty_count",
                        "tiny_lt50_count",
                    )
                },
                "gold_chunk_audit": gold_chunk_audit,
                "local_failure_categories": local_summary["categories"],
                "duplicate_slot_waste": duplicate_competition["top10_slot_waste_rate"],
                "all_metrics": output["strategy_metrics_tables"]["ALL"],
                "strong_metrics": output["strategy_metrics_tables"]["STRONG_COVERAGE_ONLY"],
                "snapshot_drift_count": snapshot_drift_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
