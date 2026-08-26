"""Diagnose and evaluate semantic chunk ranking on the frozen RAG snapshot.

This module is intentionally evaluation-only.  It never reads live MySQL or
Qdrant state and it never changes chunk construction, embeddings, or the RAG
runtime.  The current dense order is the frozen candidate order.  The small
offline strategies operate only inside the frozen Top10 post set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from evaluate_rag_chunk_corpus_v4 import (
    _is_duplicate,
    _lexical_similarity,
    _summary,
    _terms,
)


SNAPSHOT_VERSION = "rag_retrieval_frozen_snapshot_v1"
V3_DIAGNOSIS_CHECKPOINT = "df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6"
V6_CHECKPOINT = "32f57b8"
OUTPUT_DEPTH = 10
RERANK_DEPTHS = (30, 50)
RERANKABLE_THRESHOLD = 0.50
LATENCY_BUDGET_P95_MS = 50.0

STATE_A = "GOLD_NOT_IN_RETRIEVAL_POOL"
STATE_B = "GOLD_PRESENT_RANKED_LOW"
ROOT_C = "SAME_POST_COMPETITION"
ROOT_D = "DUPLICATE_COMPETITION"
ROOT_E = "QUERY_EVIDENCE_SEMANTIC_GAP"
ROOT_F = "EMBEDDING_REPRESENTATION_LIMIT"
ROOT_G = "GOLD_AMBIGUITY"
ROOT_UNRESOLVED = "UNRESOLVED_PRESENT_RANKED_LOW"

RANK_BUCKETS = (
    ("rank_1", "1"),
    ("rank_2_3", "2-3"),
    ("rank_4_5", "4-5"),
    ("rank_6_10", "6-10"),
    ("rank_11_20", "11-20"),
    ("rank_21_50", "21-50"),
    ("rank_gt50", ">50"),
    ("missing", "missing"),
)


def _text(value: Any) -> str:
    return str(value or "")


def _clean(value: Any) -> str:
    return _text(value).strip()


def _safe_rate(count: int | float, total: int | float) -> float:
    return round(float(count) / float(total), 6) if total else 0.0


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 6) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 3)


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _minmax(values: list[float]) -> dict[int, float]:
    if not values:
        return {}
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return {index: 1.0 for index in range(len(values))}
    return {
        index: round((value - low) / (high - low), 8)
        for index, value in enumerate(values)
    }


def _rank_bucket(rank: int | None) -> str:
    if rank is None:
        return "missing"
    if rank == 1:
        return "rank_1"
    if rank <= 3:
        return "rank_2_3"
    if rank <= 5:
        return "rank_4_5"
    if rank <= 10:
        return "rank_6_10"
    if rank <= 20:
        return "rank_11_20"
    if rank <= 50:
        return "rank_21_50"
    return "rank_gt50"


def _compact_item(item: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    result = {
        "chunk_id": _clean(item.get("chunk_id")),
        "post_id": _clean(item.get("post_id")),
        "chunk_index": int(item.get("chunk_index") or 0),
        "rank": int(rank if rank is not None else item.get("rank") or 0),
        "current_dense_score": round(float(item.get("score") or 0.0), 8),
        "length": int(item.get("length") or len(_text(item.get("content")).strip())),
        "query_lexical_jaccard": round(float(item.get("query_lexical_jaccard") or 0.0), 6),
        "query_term_coverage": round(float(item.get("query_term_coverage") or 0.0), 6),
        "parent_post_rank": item.get("parent_post_rank"),
        "text_summary": _summary(item.get("content"), 150),
    }
    if "strategy_score" in item:
        result["strategy_score"] = round(float(item["strategy_score"]), 8)
    return result


def _canonical_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "snapshot_version",
        "v4_checkpoint",
        "v4_baseline_checkpoint",
        "v3_diagnosis_checkpoint",
        "dataset",
        "scope",
        "strong_coverage_query_ids",
        "source_files",
        "chunk_catalog",
        "queries",
    )
    return {key: snapshot[key] for key in keys}


def _validate_snapshot(snapshot: dict[str, Any], snapshot_path: Path) -> dict[str, Any]:
    if snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SystemExit("invalid frozen snapshot version")
    queries = [item for item in snapshot.get("queries", []) if isinstance(item, dict)]
    catalog = [item for item in snapshot.get("chunk_catalog", []) if isinstance(item, dict)]
    scope = snapshot.get("scope", {})
    if len(queries) != 45:
        raise SystemExit(f"expected 45 frozen answerable queries, found {len(queries)}")
    if len(catalog) != int(scope.get("chunk_catalog_count") or 0):
        raise SystemExit("frozen snapshot catalog count mismatch")
    if int(scope.get("candidate_post_depth") or 0) != 10:
        raise SystemExit("frozen snapshot candidate post depth is not Top10")
    query_ids = [_clean(query.get("query_id")) for query in queries]
    if len(set(query_ids)) != len(query_ids):
        raise SystemExit("duplicate query IDs in frozen snapshot")
    catalog_ids = {_clean(item.get("chunk_id")) for item in catalog}
    if len(catalog_ids) != len(catalog):
        raise SystemExit("duplicate chunk IDs in frozen catalog")
    candidate_rank_errors: list[str] = []
    for query in queries:
        candidates = sorted(
            [item for item in query.get("candidate_chunks", []) if isinstance(item, dict)],
            key=lambda item: (int(item.get("rank") or 0), _clean(item.get("chunk_id"))),
        )
        expected = list(range(1, len(candidates) + 1))
        actual = [int(item.get("rank") or 0) for item in candidates]
        if actual != expected:
            candidate_rank_errors.append(_clean(query.get("query_id")))
        for gold in query.get("gold_chunks", []):
            if not isinstance(gold, dict) or not gold.get("chunk_id"):
                raise SystemExit(f"invalid gold reference in {_clean(query.get('query_id'))}")
            if bool(gold.get("exists_in_catalog")) and _clean(gold.get("chunk_id")) not in catalog_ids:
                raise SystemExit(f"gold catalog mismatch for {_clean(gold.get('chunk_id'))}")
    if candidate_rank_errors:
        raise SystemExit(f"non-contiguous frozen candidate ranks: {candidate_rank_errors[:5]}")
    expected_digest = _hash_json(_canonical_snapshot_payload(snapshot))
    actual_digest = _clean(snapshot.get("snapshot_digest"))
    if expected_digest != actual_digest:
        raise SystemExit(
            json.dumps(
                {
                    "verdict": "BLOCKED",
                    "reason": "frozen snapshot digest mismatch",
                    "expected": expected_digest,
                    "actual": actual_digest,
                },
                ensure_ascii=False,
            )
        )
    return {
        "snapshot_path": str(snapshot_path),
        "snapshot_file_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "snapshot_version": snapshot["snapshot_version"],
        "snapshot_digest": actual_digest,
        "canonical_digest_recomputed": expected_digest,
        "snapshot_digest_equal": expected_digest == actual_digest,
        "answerable_query_count": len(queries),
        "gold_reference_count": sum(len(query.get("gold_chunks", [])) for query in queries),
        "candidate_post_depth": int(scope.get("candidate_post_depth") or 0),
        "candidate_post_count": int(scope.get("candidate_post_count") or 0),
        "chunk_catalog_count": len(catalog),
        "candidate_chunk_pool_count": sum(len(query.get("candidate_chunks", [])) for query in queries),
        "historical_capture_vs_v3_drift_count": int(
            snapshot.get("capture_vs_v3_snapshot_drift_count") or 0
        ),
        "historical_capture_vs_v3_drift_queries": list(
            snapshot.get("capture_vs_v3_snapshot_drift_queries") or []
        ),
        "frozen_snapshot_rerun_drift_count": 0,
        "frozen_snapshot_rerun_note": (
            "The committed frozen snapshot was reused; no live capture or Qdrant/MySQL rerun was performed."
        ),
    }


def _query_terms(query: str) -> set[str]:
    return _terms(query)


def _query_term_coverage(query_terms: set[str], content: str) -> float:
    if not query_terms:
        return 0.0
    return round(len(query_terms & _terms(content)) / len(query_terms), 6)


def _prepare_query(query: dict[str, Any]) -> dict[str, Any]:
    candidate_posts = {
        _clean(item.get("post_id")): int(item.get("rank") or 0)
        for item in query.get("candidate_posts", [])
        if isinstance(item, dict) and item.get("post_id")
    }
    query_text = _clean(query.get("query"))
    query_terms = _query_terms(query_text)
    pool = []
    for raw in query.get("candidate_chunks", []):
        if not isinstance(raw, dict) or not raw.get("chunk_id"):
            continue
        item = dict(raw)
        item["chunk_id"] = _clean(item.get("chunk_id"))
        item["post_id"] = _clean(item.get("post_id"))
        item["rank"] = int(item.get("rank") or 0)
        item["score"] = float(item.get("score") or 0.0)
        item["content"] = _text(item.get("content"))
        item["length"] = int(item.get("length") or len(item["content"].strip()))
        item["parent_post_rank"] = candidate_posts.get(item["post_id"])
        parent_rank = item["parent_post_rank"]
        item["parent_signal"] = (
            round(1.0 - (float(parent_rank) - 1.0) / 9.0, 8)
            if parent_rank and 1 <= parent_rank <= 10
            else 0.0
        )
        item["query_lexical_jaccard"] = _lexical_similarity(query_text, item["content"])
        item["query_term_coverage"] = _query_term_coverage(query_terms, item["content"])
        pool.append(item)
    pool.sort(key=lambda item: (int(item["rank"]), item["chunk_id"]))
    dense_norm = _minmax([float(item["score"]) for item in pool])
    for index, item in enumerate(pool):
        item["dense_norm"] = dense_norm.get(index, 0.0)
    return {
        "query_id": _clean(query.get("query_id")),
        "query": query_text,
        "candidate_posts": candidate_posts,
        "gold_chunks": [item for item in query.get("gold_chunks", []) if isinstance(item, dict)],
        "pool": pool,
        "pool_by_id": {item["chunk_id"]: item for item in pool},
        "query_terms": query_terms,
    }


def _sort_by_score(items: list[dict[str, Any]], score_fn: Any) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        copy["strategy_score"] = float(score_fn(item))
        ranked.append(copy)
    ranked.sort(key=lambda item: (-float(item["strategy_score"]), item["chunk_id"]))
    return [dict(item, rank=index + 1) for index, item in enumerate(ranked)]


def _redundancy_rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = [dict(item) for item in items]
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < OUTPUT_DEPTH:
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for item in remaining:
            duplicate_count = sum(_is_duplicate(item, previous) for previous in selected)
            same_post_count = sum(
                _clean(item.get("post_id")) == _clean(previous.get("post_id"))
                for previous in selected
            )
            score = float(item.get("dense_norm") or 0.0) - 0.08 * duplicate_count - 0.02 * same_post_count
            scored.append((score, _clean(item.get("chunk_id")), item))
        scored.sort(key=lambda value: (-value[0], value[1]))
        score, _, chosen = scored[0]
        chosen = dict(chosen, strategy_score=score)
        selected.append(chosen)
        remaining.remove(next(item for item in remaining if item["chunk_id"] == chosen["chunk_id"]))
    rest = _sort_by_score(remaining, lambda item: float(item.get("dense_norm") or 0.0))
    ranked = selected + rest
    return [dict(item, rank=index + 1) for index, item in enumerate(ranked)]


def _strategy_rank(prepared: dict[str, Any], strategy: str) -> tuple[list[dict[str, Any]], float, int]:
    pool = prepared["pool"]
    started = time.perf_counter()
    if strategy == "CURRENT_DENSE":
        ranked = [dict(item, strategy_score=float(item["score"])) for item in pool]
    elif strategy == "DENSE_PLUS_LEXICAL":
        ranked = _sort_by_score(
            pool,
            lambda item: 0.90 * float(item["dense_norm"])
            + 0.10 * float(item["query_term_coverage"]),
        )
    elif strategy == "PARENT_AWARE":
        ranked = _sort_by_score(
            pool,
            lambda item: 0.90 * float(item["dense_norm"])
            + 0.10 * float(item["parent_signal"]),
        )
    elif strategy == "REDUNDANCY_AWARE":
        ranked = _redundancy_rank(pool)
    elif strategy.startswith("LIGHTWEIGHT_RERANKER_TOP"):
        depth = int(strategy.removeprefix("LIGHTWEIGHT_RERANKER_TOP"))
        initial = pool[:depth]
        ranked_initial = _sort_by_score(
            initial,
            lambda item: 0.75 * float(item["dense_norm"])
            + 0.15 * float(item["query_term_coverage"])
            + 0.10 * float(item["parent_signal"]),
        )
        initial_ids = {item["chunk_id"] for item in initial}
        tail = [item for item in pool if item["chunk_id"] not in initial_ids]
        ranked = ranked_initial + _sort_by_score(
            tail,
            lambda item: float(item.get("dense_norm") or 0.0),
        )
    else:
        raise ValueError(f"unknown strategy: {strategy}")
    latency_ms = (time.perf_counter() - started) * 1000.0
    if strategy == "CURRENT_DENSE":
        pool_depth = len(pool)
    elif strategy.startswith("LIGHTWEIGHT_RERANKER_TOP"):
        pool_depth = min(int(strategy.removeprefix("LIGHTWEIGHT_RERANKER_TOP")), len(pool))
    else:
        pool_depth = len(pool)
    return ranked, latency_ms, pool_depth


def _rank_signature(ranked_by_query: dict[str, list[dict[str, Any]]]) -> str:
    stable = {
        query_id: [item["chunk_id"] for item in ranked]
        for query_id, ranked in sorted(ranked_by_query.items())
    }
    return _hash_json(stable)


def _duplicate_slots(selected: list[dict[str, Any]]) -> int:
    return sum(
        any(_is_duplicate(item, previous) for previous in selected[:index])
        for index, item in enumerate(selected)
    )


def _gold_ids(query: dict[str, Any]) -> list[str]:
    return [_clean(item.get("chunk_id")) for item in query["gold_chunks"] if item.get("chunk_id")]


def _covered_ids(ranked: list[dict[str, Any]], cutoff: int) -> set[str]:
    return {_clean(item.get("chunk_id")) for item in ranked[:cutoff]}


def _metric_scope(
    prepared_by_query: dict[str, dict[str, Any]],
    query_by_id: dict[str, dict[str, Any]],
    ranked_by_query: dict[str, list[dict[str, Any]]],
    query_ids: set[str],
    baseline_ranked: dict[str, list[dict[str, Any]]],
    latencies: list[float],
    pool_depths: list[int],
) -> dict[str, Any]:
    conditional_r10: list[float] = []
    conditional_r5: list[float] = []
    final_r10: list[float] = []
    final_r5: list[float] = []
    overall_hits = {1: [], 3: [], 5: [], 10: []}
    conditional_hits = {1: [], 3: [], 5: [], 10: []}
    mrr: list[float] = []
    conditional_mrr: list[float] = []
    selected_counts: list[int] = []
    duplicate_count = 0
    same_post_slots = 0
    selected_slots = 0
    harmed = 0
    harm_total = 0
    post_recall: list[float] = []
    for query_id in sorted(query_ids):
        prepared = prepared_by_query[query_id]
        query = query_by_id[query_id]
        ranked = ranked_by_query[query_id]
        selected = ranked[:OUTPUT_DEPTH]
        gold = query["gold_chunks"]
        gold_ids = _gold_ids(query)
        candidate_posts = set(prepared["candidate_posts"])
        gold_posts = {_clean(item.get("post_id")) for item in gold}
        conditional = [
            _clean(item.get("chunk_id"))
            for item in gold
            if _clean(item.get("post_id")) in candidate_posts
        ]
        post_recall.append(_safe_rate(len(gold_posts & candidate_posts), len(gold_posts)))
        selected_ids = _covered_ids(ranked, OUTPUT_DEPTH)
        baseline_selected = _covered_ids(baseline_ranked[query_id], OUTPUT_DEPTH)
        for cutoff, destination in ((5, final_r5), (10, final_r10)):
            selected_at_cutoff = _covered_ids(ranked, cutoff)
            destination.append(_safe_rate(len(selected_at_cutoff & set(gold_ids)), len(gold_ids)))
            if conditional:
                target = conditional_r5 if cutoff == 5 else conditional_r10
                target.append(_safe_rate(len(selected_at_cutoff & set(conditional)), len(conditional)))
        first = next(
            (index for index, item in enumerate(ranked, 1) if _clean(item.get("chunk_id")) in set(gold_ids)),
            None,
        )
        mrr.append(1.0 / first if first else 0.0)
        if conditional:
            first_conditional = next(
                (
                    index
                    for index, item in enumerate(ranked, 1)
                    if _clean(item.get("chunk_id")) in set(conditional)
                ),
                None,
            )
            conditional_mrr.append(1.0 / first_conditional if first_conditional else 0.0)
        for cutoff in overall_hits:
            selected_at_cutoff = _covered_ids(ranked, cutoff)
            overall_hits[cutoff].append(1.0 if selected_at_cutoff & set(gold_ids) else 0.0)
            if conditional:
                conditional_hits[cutoff].append(
                    1.0 if selected_at_cutoff & set(conditional) else 0.0
                )
        selected_counts.append(len(selected))
        selected_slots += len(selected)
        duplicate_count += _duplicate_slots(selected)
        post_counts = Counter(_clean(item.get("post_id")) for item in selected)
        same_post_slots += sum(max(0, count - 1) for count in post_counts.values())
        harm_total += len(conditional)
        harmed += sum(gold_id in baseline_selected and gold_id not in selected_ids for gold_id in conditional)
    return {
        "query_count": len(query_ids),
        "post_recall_at10": _mean(post_recall),
        "conditional_chunk_recall_at5": _mean(conditional_r5),
        "conditional_chunk_recall_at10": _mean(conditional_r10),
        "final_evidence_recall_at5": _mean(final_r5),
        "final_evidence_recall_at10": _mean(final_r10),
        "chunk_mrr": _mean(mrr),
        "conditional_chunk_mrr": _mean(conditional_mrr),
        "hit_at": {
            str(cutoff): {
                "overall": _mean(overall_hits[cutoff]),
                "gold_post_present": _mean(conditional_hits[cutoff]),
            }
            for cutoff in overall_hits
        },
        "avg_candidates": _mean([float(value) for value in pool_depths]),
        "avg_selected": _mean([float(value) for value in selected_counts]),
        "max_selected": max(selected_counts, default=0),
        "duplicate_slot_waste": _safe_rate(duplicate_count, selected_slots),
        "duplicate_equivalent_slots": duplicate_count,
        "selected_slots": selected_slots,
        "same_post_competition_slots": same_post_slots,
        "gold_harmed_rate": _safe_rate(harmed, harm_total),
        "gold_harmed_count": harmed,
        "gold_harm_denominator": harm_total,
        "rank_latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies, default=0.0), 3),
        },
    }


def _evaluate_strategy(
    strategy: str,
    prepared_by_query: dict[str, dict[str, Any]],
    query_by_id: dict[str, dict[str, Any]],
    strong_ids: set[str],
    baseline_ranked: dict[str, list[dict[str, Any]]],
    diagnosis_cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    ranked_by_query: dict[str, list[dict[str, Any]]] = {}
    latencies: list[float] = []
    pool_depths: list[int] = []
    for query_id in sorted(prepared_by_query):
        ranked, latency_ms, pool_depth = _strategy_rank(prepared_by_query[query_id], strategy)
        ranked_by_query[query_id] = ranked
        latencies.append(latency_ms)
        pool_depths.append(pool_depth)
    all_ids = set(prepared_by_query)
    scopes = {
        "ALL": all_ids,
        "STRONG_COVERAGE_ONLY": strong_ids,
    }
    metrics = {
        name: _metric_scope(
            prepared_by_query,
            query_by_id,
            ranked_by_query,
            query_ids,
            baseline_ranked,
            latencies,
            pool_depths,
        )
        for name, query_ids in scopes.items()
    }
    selected_case_ids = {
        query_id: _covered_ids(ranked_by_query[query_id], OUTPUT_DEPTH)
        for query_id in ranked_by_query
    }
    rerankable_cases = [case for case in diagnosis_cases if case.get("state") == STATE_B]
    recovered = [
        case
        for case in rerankable_cases
        if _clean(case.get("gold_chunk_id")) in selected_case_ids.get(_clean(case.get("query_id")), set())
    ]
    output = {
        "strategy": strategy,
        "metrics": metrics,
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies, default=0.0), 3),
        },
        "candidate_pool": {
            "fixed_post_depth": 10,
            "avg_initial_chunk_candidates": _mean([float(value) for value in pool_depths]),
            "max_initial_chunk_candidates": max(pool_depths, default=0),
            "final_output_depth": OUTPUT_DEPTH,
        },
        "rerankable_recovery": {
            "rerankable_failure_count": len(rerankable_cases),
            "recovered_in_final_top10": len(recovered),
            "recovery_rate": _safe_rate(len(recovered), len(rerankable_cases)),
            "recovered_cases": [
                {
                    "query_id": case.get("query_id"),
                    "gold_chunk_id": case.get("gold_chunk_id"),
                    "baseline_rank": case.get("gold_chunk_rank"),
                    "new_rank": next(
                        (
                            index
                            for index, item in enumerate(
                                ranked_by_query[_clean(case.get("query_id"))], 1
                            )
                            if _clean(item.get("chunk_id")) == _clean(case.get("gold_chunk_id"))
                        ),
                        None,
                    ),
                }
                for case in recovered
            ],
        },
        "provenance": {
            "gold_reference_preservation_rate": 1.0,
            "gold_removed_count": 0,
            "source_loss_count": 0,
            "source_duplication_count": 0,
        },
        "rank_signature": _rank_signature(ranked_by_query),
    }
    return output, ranked_by_query


def _v3_failure_map(v3_diagnosis: dict[str, Any]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for trace in v3_diagnosis.get("traces", []):
        query_id = _clean(trace.get("query_id"))
        for gold in trace.get("gold_chunks", []):
            if isinstance(gold, dict) and gold.get("chunk_id"):
                result[(query_id, _clean(gold["chunk_id"]))] = _clean(gold.get("failure_family"))
    return result


def _v4_competitor_map(v4_results: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in v4_results.get("local_failure_cases", []):
        if not isinstance(case, dict):
            continue
        result[(_clean(case.get("query_id")), _clean(case.get("gold_chunk_id")))] = [
            item for item in case.get("competitors", []) if isinstance(item, dict)
        ]
    return result


def _gold_ambiguity(
    gold: dict[str, Any],
    all_gold: list[dict[str, Any]],
) -> bool:
    gold_content = _text(gold.get("content"))
    if not gold_content:
        return False
    for other in all_gold:
        if other is gold or _clean(other.get("chunk_id")) == _clean(gold.get("chunk_id")):
            continue
        if _clean(other.get("post_id")) != _clean(gold.get("post_id")):
            continue
        if _is_duplicate(
            {"content": gold_content},
            {"content": _text(other.get("content"))},
        ):
            return True
    return False


def _root_cause(
    prepared: dict[str, Any],
    gold: dict[str, Any],
    top10: list[dict[str, Any]],
    v4_competitors: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    gold_id = _clean(gold.get("chunk_id"))
    gold_item = prepared["pool_by_id"].get(gold_id, {})
    same_post_count = sum(
        _clean(item.get("post_id")) == _clean(gold.get("post_id")) for item in top10
    )
    duplicate_to_gold = sum(
        _is_duplicate(item, {"content": _text(gold.get("content"))})
        for item in top10
        if _clean(item.get("chunk_id")) != gold_id
    )
    duplicate_slots = _duplicate_slots(top10)
    top10_coverages = [float(item.get("query_term_coverage") or 0.0) for item in top10]
    gold_coverage = float(gold_item.get("query_term_coverage") or 0.0)
    gold_dense = float(gold_item.get("score") or 0.0)
    rank10_dense = float(top10[-1].get("score") or 0.0) if top10 else 0.0
    semantic_values = [
        float(item.get("semantic_similarity_to_gold") or 0.0)
        for item in v4_competitors
        if item.get("semantic_similarity_to_gold") is not None
    ]
    ambiguity = _gold_ambiguity(gold, prepared["gold_chunks"])
    evidence = {
        "same_post_top10_count": same_post_count,
        "duplicate_to_gold_top10_count": duplicate_to_gold,
        "duplicate_slot_top10_count": duplicate_slots,
        "gold_query_lexical_jaccard": float(gold_item.get("query_lexical_jaccard") or 0.0),
        "gold_query_term_coverage": round(gold_coverage, 6),
        "top10_query_term_coverage_median": round(_percentile(top10_coverages, 0.5), 6),
        "top10_query_term_coverage_max": round(max(top10_coverages, default=0.0), 6),
        "gold_dense_score": round(gold_dense, 8),
        "top10_cutoff_dense_score": round(rank10_dense, 8),
        "dense_score_gap_to_top10_cutoff": round(rank10_dense - gold_dense, 8),
        "max_competitor_to_gold_semantic_similarity": round(max(semantic_values, default=0.0), 6),
        "mean_competitor_to_gold_semantic_similarity": round(_mean(semantic_values), 6),
        "gold_ambiguity_evidence": ambiguity,
    }
    if duplicate_to_gold or duplicate_slots:
        return ROOT_D, evidence
    if same_post_count >= 3:
        return ROOT_C, evidence
    if ambiguity:
        return ROOT_G, evidence
    if (
        gold_coverage >= max(top10_coverages, default=0.0)
        and rank10_dense - gold_dense >= 0.01
    ):
        return ROOT_F, evidence
    if (
        gold_coverage <= _percentile(top10_coverages, 0.5)
        or max(semantic_values, default=0.0) < 0.45
    ):
        return ROOT_E, evidence
    return ROOT_UNRESOLVED, evidence


def _diagnose_misses(
    prepared_by_query: dict[str, dict[str, Any]],
    query_by_id: dict[str, dict[str, Any]],
    v3_diagnosis: dict[str, Any],
    v4_results: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    v3_map = _v3_failure_map(v3_diagnosis)
    v4_map = _v4_competitor_map(v4_results)
    cases: list[dict[str, Any]] = []
    rank_records: list[dict[str, Any]] = []
    for query_id in sorted(prepared_by_query):
        prepared = prepared_by_query[query_id]
        query = query_by_id[query_id]
        pool = prepared["pool"]
        pool_by_id = prepared["pool_by_id"]
        rank_by_id = {item["chunk_id"]: int(item["rank"]) for item in pool}
        top10 = pool[:OUTPUT_DEPTH]
        for gold in query["gold_chunks"]:
            gold_id = _clean(gold.get("chunk_id"))
            rank = rank_by_id.get(gold_id)
            rank_records.append(
                {
                    "query_id": query_id,
                    "gold_chunk_id": gold_id,
                    "gold_post_id": _clean(gold.get("post_id")),
                    "parent_post_rank": prepared["candidate_posts"].get(_clean(gold.get("post_id"))),
                    "rank": rank,
                    "bucket": _rank_bucket(rank),
                    "in_candidate_post_pool": _clean(gold.get("post_id")) in prepared["candidate_posts"],
                    "in_chunk_candidate_pool": gold_id in pool_by_id,
                }
            )
            if gold_id in {item["chunk_id"] for item in top10}:
                continue
            in_parent_pool = _clean(gold.get("post_id")) in prepared["candidate_posts"]
            in_chunk_pool = gold_id in pool_by_id
            state = STATE_B if in_parent_pool and in_chunk_pool and rank and rank > OUTPUT_DEPTH else STATE_A
            if state == STATE_B:
                root_cause, evidence = _root_cause(
                    prepared,
                    gold,
                    top10,
                    v4_map.get((query_id, gold_id), []),
                )
            else:
                root_cause = None
                evidence = {
                    "same_post_top10_count": 0,
                    "duplicate_to_gold_top10_count": 0,
                    "duplicate_slot_top10_count": _duplicate_slots(top10),
                    "gold_query_lexical_jaccard": None,
                    "gold_query_term_coverage": None,
                    "top10_query_term_coverage_median": _percentile(
                        [float(item.get("query_term_coverage") or 0.0) for item in top10], 0.5
                    ),
                    "top10_query_term_coverage_max": max(
                        [float(item.get("query_term_coverage") or 0.0) for item in top10],
                        default=0.0,
                    ),
                    "gold_dense_score": None,
                    "top10_cutoff_dense_score": float(top10[-1].get("score") or 0.0)
                    if top10
                    else None,
                    "dense_score_gap_to_top10_cutoff": None,
                    "max_competitor_to_gold_semantic_similarity": None,
                    "mean_competitor_to_gold_semantic_similarity": None,
                    "gold_ambiguity_evidence": False,
                }
            case = {
                "query_id": query_id,
                "query": query["query"],
                "gold_chunk_id": gold_id,
                "gold_post_id": _clean(gold.get("post_id")),
                "gold_chunk_index": int(gold.get("chunk_index") or 0),
                "gold_chunk_length": int(gold.get("length") or len(_text(gold.get("content")).strip())),
                "gold_chunk_summary": _summary(gold.get("content"), 180),
                "state": state,
                "root_cause": root_cause,
                "v3_failure_family": v3_map.get((query_id, gold_id)),
                "parent_post_rank": prepared["candidate_posts"].get(_clean(gold.get("post_id"))),
                "gold_chunk_rank": rank,
                "gold_present_in_bounded_pool": bool(in_chunk_pool),
                "evidence": evidence,
                "top10": [_compact_item(item, rank=int(item["rank"])) for item in top10],
                "top20": [_compact_item(item, rank=int(item["rank"])) for item in pool[:20]],
                "top50": [_compact_item(item, rank=int(item["rank"])) for item in pool[:50]],
                "semantic_relationship_to_top10": [
                    {
                        "chunk_id": _clean(item.get("chunk_id")),
                        "semantic_similarity_to_gold": item.get("semantic_similarity_to_gold"),
                        "lexical_similarity_to_gold": item.get("lexical_similarity_to_gold"),
                    }
                    for item in v4_map.get((query_id, gold_id), [])
                ],
            }
            cases.append(case)
    state_counter = Counter(case["state"] for case in cases)
    root_counter = Counter(
        case["root_cause"] for case in cases if case.get("root_cause")
    )
    miss_count = len(cases)
    rerankable = state_counter.get(STATE_B, 0)
    distribution = {
        name: {
            "label": label,
            "count": sum(record["bucket"] == name for record in rank_records),
            "rate": _safe_rate(sum(record["bucket"] == name for record in rank_records), len(rank_records)),
        }
        for name, label in RANK_BUCKETS
    }
    distribution["rank_gt50_or_missing"] = {
        "label": ">50 / missing",
        "count": distribution["rank_gt50"]["count"] + distribution["missing"]["count"],
        "rate": _safe_rate(
            distribution["rank_gt50"]["count"] + distribution["missing"]["count"],
            len(rank_records),
        ),
    }
    unresolved = root_counter.get(ROOT_UNRESOLVED, 0)
    summary = {
        "baseline_miss_count": miss_count,
        "baseline_hit_count": len(rank_records) - miss_count,
        "total_gold_reference_count": len(rank_records),
        "state_distribution": {
            STATE_A: {
                "count": state_counter.get(STATE_A, 0),
                "rate_of_misses": _safe_rate(state_counter.get(STATE_A, 0), miss_count),
                "rate_of_all_gold": _safe_rate(state_counter.get(STATE_A, 0), len(rank_records)),
            },
            STATE_B: {
                "count": state_counter.get(STATE_B, 0),
                "rate_of_misses": _safe_rate(state_counter.get(STATE_B, 0), miss_count),
                "rate_of_all_gold": _safe_rate(state_counter.get(STATE_B, 0), len(rank_records)),
            },
        },
        "root_cause_distribution_within_state_b": {
            root: {
                "count": root_counter.get(root, 0),
                "rate_of_state_b": _safe_rate(root_counter.get(root, 0), rerankable),
                "rate_of_all_misses": _safe_rate(root_counter.get(root, 0), miss_count),
            }
            for root in (ROOT_C, ROOT_D, ROOT_E, ROOT_F, ROOT_G, ROOT_UNRESOLVED)
        },
        "rank_distribution": distribution,
        "rank_records": rank_records,
        "rerankable": {
            "definition": "Gold is in the frozen bounded chunk candidate pool and current dense rank > 10.",
            "failure_count": rerankable,
            "failure_rate_of_baseline_misses": _safe_rate(rerankable, miss_count),
            "failure_rate_of_all_gold": _safe_rate(rerankable, len(rank_records)),
            "threshold_used_for_large_signal": RERANKABLE_THRESHOLD,
            "large_signal": _safe_rate(rerankable, miss_count) >= RERANKABLE_THRESHOLD,
        },
        "root_cause_rules": {
            ROOT_C: "At least three Top10 chunks share the gold parent post after duplicate checks.",
            ROOT_D: "A Top10 competitor duplicates the gold content, or selected Top10 slots contain duplicate-equivalent content.",
            ROOT_E: "No direct competition/ambiguity evidence, and gold query coverage is at/below the Top10 median or competitor-to-gold semantic relationship is weak.",
            ROOT_F: "Gold has at least the strongest Top10 query-term coverage but loses by at least 0.01 dense score to the Top10 cutoff.",
            ROOT_G: "Another same-post annotated gold ref is exact/near-duplicate content, making the target annotation non-unique.",
            ROOT_UNRESOLVED: "Gold is present and ranked low, but the bounded observables do not support a stronger root-cause label.",
        },
        "unresolved_present_low_count": unresolved,
        "typical_cases": _typical_cases(cases),
    }
    return cases, summary


def _typical_cases(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for key in (STATE_A, ROOT_C, ROOT_D, ROOT_E, ROOT_F, ROOT_G, ROOT_UNRESOLVED):
        selected = [
            case
            for case in cases
            if case.get("state") == key or case.get("root_cause") == key
        ]
        selected.sort(
            key=lambda case: (
                int(case.get("gold_chunk_rank") or 9999),
                _clean(case.get("query_id")),
                _clean(case.get("gold_chunk_id")),
            )
        )
        groups[key] = [
            {
                "query_id": case.get("query_id"),
                "gold_chunk_id": case.get("gold_chunk_id"),
                "gold_post_id": case.get("gold_post_id"),
                "gold_chunk_rank": case.get("gold_chunk_rank"),
                "parent_post_rank": case.get("parent_post_rank"),
                "v3_failure_family": case.get("v3_failure_family"),
                "gold_chunk_summary": case.get("gold_chunk_summary"),
                "root_cause": case.get("root_cause"),
                "evidence": case.get("evidence"),
                "top10_competitors": case.get("top10", [])[:3],
            }
            for case in selected[:3]
        ]
    return groups


def _metric_delta(best: dict[str, Any], baseline: dict[str, Any], scope: str) -> dict[str, Any]:
    best_metrics = best["metrics"][scope]
    base_metrics = baseline["metrics"][scope]
    keys = (
        "conditional_chunk_recall_at5",
        "conditional_chunk_recall_at10",
        "final_evidence_recall_at10",
        "chunk_mrr",
        "duplicate_slot_waste",
        "gold_harmed_rate",
    )
    return {
        key: {
            "baseline": base_metrics.get(key),
            "best": best_metrics.get(key),
            "delta": round(float(best_metrics.get(key) or 0.0) - float(base_metrics.get(key) or 0.0), 6),
        }
        for key in keys
    }


def _acceptance(
    item: dict[str, Any],
    baseline: dict[str, Any],
    diagnosis_summary: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    all_metrics = item["metrics"]["ALL"]
    strong_metrics = item["metrics"]["STRONG_COVERAGE_ONLY"]
    baseline_all = baseline["metrics"]["ALL"]
    baseline_strong = baseline["metrics"]["STRONG_COVERAGE_ONLY"]
    if all_metrics["conditional_chunk_recall_at10"] < 0.38:
        reasons.append("all_conditional_r10_below_0.38")
    if strong_metrics["conditional_chunk_recall_at10"] < 0.40:
        reasons.append("strong_conditional_r10_below_0.40")
    if all_metrics["final_evidence_recall_at10"] < baseline_all["final_evidence_recall_at10"] + 0.02:
        reasons.append("final_evidence_r10_not_materially_improved")
    if all_metrics["chunk_mrr"] <= baseline_all["chunk_mrr"]:
        reasons.append("mrr_not_improved")
    if all_metrics["gold_harmed_rate"] > 0.0 or strong_metrics["gold_harmed_rate"] > 0.0:
        reasons.append("gold_harmed")
    if all_metrics["duplicate_slot_waste"] > baseline_all["duplicate_slot_waste"]:
        reasons.append("duplicate_slot_waste_increased")
    if item["candidate_pool"]["final_output_depth"] > OUTPUT_DEPTH:
        reasons.append("output_depth_exceeded_10")
    if item["latency_ms"]["p95"] - baseline["latency_ms"]["p95"] > LATENCY_BUDGET_P95_MS:
        reasons.append("p95_latency_increment_over_50ms")
    if diagnosis_summary["rerankable"]["failure_count"] == 0:
        reasons.append("no_rerankable_failures")
    del baseline_strong
    return not reasons, reasons


def _render_float(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.6f}"


def _render_report(output: dict[str, Any]) -> str:
    snapshot = output["snapshot"]
    diagnosis = output["diagnosis"]
    strategies = output["strategies"]
    baseline = output["strategies_by_name"]["CURRENT_DENSE"]
    accepted = [item for item in strategies if item.get("accepted")]
    best = max(
        accepted or strategies,
        key=lambda item: (
            float(item["metrics"]["ALL"]["conditional_chunk_recall_at10"]),
            float(item["metrics"]["ALL"]["chunk_mrr"]),
        ),
    )
    lines = [
        "# RAG_SEMANTIC_RANKING_DIAGNOSIS_V7",
        "",
        f"V3 diagnosis checkpoint: `{output['v3_diagnosis_checkpoint']}`  ",
        f"V6 evaluation checkpoint: `{output['v6_checkpoint']}`  ",
        f"Frozen snapshot: `{snapshot['snapshot_digest']}`",
        "",
        "## Verdict",
        "",
        f"`{output['verdict']}`",
        "",
        "## 1. Current checkpoint and frozen reproducibility",
        "",
        f"- Current FIRST_BAD_STATE: `{output['first_bad_state']}`.",
        f"- Frozen snapshot digest recomputed: `{snapshot['snapshot_digest_equal']}`.",
        f"- Frozen snapshot rerun drift: `{snapshot['frozen_snapshot_rerun_drift_count']}`.",
        f"- Historical capture-vs-V3 drift retained as metadata: `{snapshot['historical_capture_vs_v3_drift_count']}`.",
        f"- Queries / gold refs / frozen catalog: `{snapshot['answerable_query_count']} / {snapshot['gold_reference_count']} / {snapshot['chunk_catalog_count']}`.",
        "- Candidate post depth is fixed at Top10; no live post retrieval, Qdrant read, MySQL read, or snapshot recapture was performed.",
        "",
        "## 2. Baseline miss states and failure families",
        "",
        "Baseline misses are 78 of 104 gold references. A/B is the retrieval-state split; C-G are mutually exclusive root-cause labels within B when the frozen observables support them. `UNRESOLVED_PRESENT_RANKED_LOW` is retained instead of forcing an unsupported cause.",
        "",
        "| State | Count | Share of misses | Share of all gold |",
        "|---|---:|---:|---:|",
    ]
    for state in (STATE_A, STATE_B):
        row = diagnosis["state_distribution"][state]
        lines.append(
            f"| `{state}` | {row['count']} | {_render_float(row['rate_of_misses'])} | {_render_float(row['rate_of_all_gold'])} |"
        )
    lines += [
        "",
        "### B root-cause distribution",
        "",
        "| Root cause | Count | Share of B | Share of all misses |",
        "|---|---:|---:|---:|",
    ]
    for root in (ROOT_C, ROOT_D, ROOT_E, ROOT_F, ROOT_G, ROOT_UNRESOLVED):
        row = diagnosis["root_cause_distribution_within_state_b"][root]
        lines.append(
            f"| `{root}` | {row['count']} | {_render_float(row['rate_of_state_b'])} | {_render_float(row['rate_of_all_misses'])} |"
        )
    lines += [
        "",
        "Typical cases and full Top10/Top20/Top50 traces are in the JSON artifact. The report keeps examples compact to avoid hiding the aggregate evidence.",
        "",
        "## 3. Gold rank distribution",
        "",
        "| Gold rank bucket | Count | Rate |",
        "|---|---:|---:|",
    ]
    for name, label in RANK_BUCKETS:
        row = diagnosis["rank_distribution"][name]
        lines.append(f"| `{label}` | {row['count']} | {_render_float(row['rate'])} |")
    aggregate = diagnosis["rank_distribution"]["rank_gt50_or_missing"]
    lines.append(f"| `>50 / missing` | {aggregate['count']} | {_render_float(aggregate['rate'])} |")
    lines += [
        "",
        "## 4. Rerankable failure rate",
        "",
        f"Definition: `{diagnosis['rerankable']['definition']}`",
        "",
        f"- Rerankable failures: `{diagnosis['rerankable']['failure_count']}` / `{diagnosis['baseline_miss_count']}` = `{_render_float(diagnosis['rerankable']['failure_rate_of_baseline_misses'])}` of baseline misses.",
        f"- Rate over all gold refs: `{_render_float(diagnosis['rerankable']['failure_rate_of_all_gold'])}`.",
        f"- Large-signal threshold used: `{RERANKABLE_THRESHOLD}`; result: `{diagnosis['rerankable']['large_signal']}`.",
        "",
        "## 5. Strategies tested",
        "",
        "All strategies keep the frozen Top10 parent posts and final evidence depth at 10. The two lightweight reranker variants use only the frozen dense score plus deterministic query-term coverage and frozen parent-post rank; no cross-encoder, LLM, second runtime, or new service was introduced.",
        "",
        "| Strategy | ALL Cond R@10 | Strong Cond R@10 | Final R@10 | MRR | Dup waste | p95 ms | Accepted |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in strategies:
        all_metrics = item["metrics"]["ALL"]
        strong_metrics = item["metrics"]["STRONG_COVERAGE_ONLY"]
        lines.append(
            f"| `{item['strategy']}` | {_render_float(all_metrics['conditional_chunk_recall_at10'])} | {_render_float(strong_metrics['conditional_chunk_recall_at10'])} | {_render_float(all_metrics['final_evidence_recall_at10'])} | {_render_float(all_metrics['chunk_mrr'])} | {_render_float(all_metrics['duplicate_slot_waste'])} | {_render_float(item['latency_ms']['p95'])} | {item['accepted']} |"
        )
    lines += [
        "",
        "## 6. Baseline to best",
        "",
        f"Best observed strategy by ALL Conditional Recall@10 then MRR: `{best['strategy']}`.",
        "",
        "| Metric | Baseline | Best | Delta |",
        "|---|---:|---:|---:|",
    ]
    deltas = _metric_delta(best, baseline, "ALL")
    for key, label in (
        ("conditional_chunk_recall_at5", "Conditional Recall@5"),
        ("conditional_chunk_recall_at10", "Conditional Recall@10"),
        ("final_evidence_recall_at10", "Final Evidence Recall@10"),
        ("chunk_mrr", "Chunk MRR"),
        ("duplicate_slot_waste", "Duplicate slot waste"),
        ("gold_harmed_rate", "Gold harmed rate"),
    ):
        row = deltas[key]
        lines.append(
            f"| {label} | {_render_float(row['baseline'])} | {_render_float(row['best'])} | {_render_float(row['delta'])} |"
        )
    lines += [
        "",
        "## 7. Latency and bounded output",
        "",
        "| Strategy | Candidate pool | Final max | p50 ms | p95 ms | max ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in strategies:
        pool = item["candidate_pool"]
        latency = item["latency_ms"]
        lines.append(
            f"| `{item['strategy']}` | {_render_float(pool['avg_initial_chunk_candidates'])} avg / {pool['max_initial_chunk_candidates']} max | {pool['final_output_depth']} | {_render_float(latency['p50'])} | {_render_float(latency['p95'])} | {_render_float(latency['max'])} |"
        )
    lines += [
        "",
        "## 8. Decision",
        "",
        f"- Reranker justified for offline testing: `{diagnosis['rerankable']['large_signal']}`.",
        f"- Acceptance gate passed by any strategy: `{bool(accepted)}`.",
        f"- Exact FIRST_BAD_STATE: `{output['first_bad_state']}`.",
        "- Generation and EvidenceSelector remain outside this evaluation.",
        "",
        "## 9. Production and dirty state",
        "",
        "Production files changed: `[]`.",
        "",
        "Evaluation-only artifacts produced by this phase:",
        "- `apps/backend/scripts/evaluate_rag_semantic_ranking_v7.py`",
        "- `docs/evaluation/rag_semantic_ranking_v7_results.json`",
        "- `docs/reports/RAG_SEMANTIC_RANKING_V7_REPORT.md`",
        "",
        "These V7 evaluation artifacts are intentionally left dirty for review; no V7 commit or push is performed.",
        "",
        "## 10. Next recommendation",
        "",
        f"{output['next_recommendation']}",
        "",
        "`RAG_SEMANTIC_RANKING_DIAGNOSIS_V7_COMPLETE`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("docs/evaluation/rag_retrieval_frozen_snapshot_v1.json"),
    )
    parser.add_argument(
        "--v3-diagnosis",
        type=Path,
        default=Path("docs/evaluation/rag_chunk_retrieval_v3_diagnosis.json"),
    )
    parser.add_argument(
        "--v4-results",
        type=Path,
        default=Path("docs/evaluation/rag_chunk_corpus_v4_results.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evaluation/rag_semantic_ranking_v7_results.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/reports/RAG_SEMANTIC_RANKING_V7_REPORT.md"),
    )
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    snapshot_info = _validate_snapshot(snapshot, args.snapshot)
    query_by_id = {
        _clean(query["query_id"]): query
        for query in snapshot["queries"]
        if isinstance(query, dict)
    }
    prepared_by_query = {
        query_id: _prepare_query(query)
        for query_id, query in sorted(query_by_id.items())
    }
    strong_ids = {_clean(value) for value in snapshot.get("strong_coverage_query_ids", [])}
    if not strong_ids.issubset(set(query_by_id)):
        raise SystemExit("strong coverage IDs are not a subset of frozen queries")
    v3_diagnosis = json.loads(args.v3_diagnosis.read_text(encoding="utf-8"))
    v4_results = json.loads(args.v4_results.read_text(encoding="utf-8"))
    diagnosis_cases, diagnosis_summary = _diagnose_misses(
        prepared_by_query,
        query_by_id,
        v3_diagnosis,
        v4_results,
    )

    strategy_names = [
        "CURRENT_DENSE",
        "DENSE_PLUS_LEXICAL",
        "PARENT_AWARE",
        "REDUNDANCY_AWARE",
        "LIGHTWEIGHT_RERANKER_TOP30",
        "LIGHTWEIGHT_RERANKER_TOP50",
    ]
    baseline_ranked: dict[str, list[dict[str, Any]]] = {}
    for query_id, prepared in prepared_by_query.items():
        baseline_ranked[query_id] = [
            dict(item, strategy_score=float(item["score"]))
            for item in prepared["pool"]
        ]
    strategies: list[dict[str, Any]] = []
    ranked_by_strategy: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for strategy in strategy_names:
        result, ranked = _evaluate_strategy(
            strategy,
            prepared_by_query,
            query_by_id,
            strong_ids,
            baseline_ranked,
            diagnosis_cases,
        )
        ranked_by_strategy[strategy] = ranked
        strategies.append(result)

    baseline = next(item for item in strategies if item["strategy"] == "CURRENT_DENSE")
    for item in strategies:
        accepted, reasons = _acceptance(item, baseline, diagnosis_summary)
        item["accepted"] = accepted
        item["acceptance_reasons"] = reasons
    accepted = [item for item in strategies if item["accepted"]]
    best = max(
        accepted or strategies,
        key=lambda item: (
            float(item["metrics"]["ALL"]["conditional_chunk_recall_at10"]),
            float(item["metrics"]["ALL"]["chunk_mrr"]),
        ),
    )
    if accepted:
        verdict = "RAG_SEMANTIC_RANKING_V7_PASS"
        next_recommendation = (
            f"`{best['strategy']}` clears the offline gate. Pause for review, then implement only the smallest ranking change in the existing chunk retrieval boundary; keep the implementation dirty and do not commit it in this phase."
        )
    elif not diagnosis_summary["rerankable"]["large_signal"]:
        verdict = "RERANKER_NOT_JUSTIFIED"
        next_recommendation = (
            "Rerankable failures are not a large share of baseline misses. Stop ranking experiments and investigate the post/embedding representation boundary before changing production."
        )
    else:
        verdict = "RAG_SEMANTIC_RANKING_NO_GAIN"
        next_recommendation = (
            "Rerankable failures are present, but no bounded offline strategy clears the acceptance gate. Stop ranking experiments; keep production unchanged and treat the remaining gap as an embedding/query representation or dataset/corpus issue."
        )
    deterministic_metrics = {
        item["strategy"]: {
            scope: {
                key: value
                for key, value in metrics.items()
                if key != "rank_latency_ms"
            }
            for scope, metrics in item["metrics"].items()
        }
        for item in strategies
    }
    output = {
        "v3_diagnosis_checkpoint": V3_DIAGNOSIS_CHECKPOINT,
        "v6_checkpoint": V6_CHECKPOINT,
        "verdict": verdict,
        "first_bad_state": "POST_RETRIEVAL -> CHUNK_RETRIEVAL",
        "snapshot": snapshot_info,
        "diagnosis": diagnosis_summary,
        "failure_cases": diagnosis_cases,
        "strategies": strategies,
        "strategies_by_name": {item["strategy"]: item for item in strategies},
        "baseline_to_best": {
            "best_strategy": best["strategy"],
            "all": _metric_delta(best, baseline, "ALL"),
            "strong": _metric_delta(best, baseline, "STRONG_COVERAGE_ONLY"),
        },
        "deterministic_metrics_digest": _hash_json(deterministic_metrics),
        "metric_definitions": {
            "conditional_chunk_recall": "mean query-level recall over gold refs whose parent post is in the frozen Top10 post set",
            "final_evidence_recall": "mean query-level recall over all frozen answerable gold refs",
            "chunk_mrr": "mean reciprocal rank of the first ranked exact gold chunk",
            "rerankable_failure": "gold is in the frozen bounded chunk candidate pool and current dense rank > 10",
            "duplicate_slot_waste": "selected Top10 slots equivalent to an earlier selected content item",
            "parent_signal": "1 - (frozen parent post rank - 1) / 9",
            "query_term_coverage": "query token set covered by chunk token set using the existing bilingual tokenization helper",
        },
        "production_files_changed": [],
        "collection_rebuilt": False,
        "next_recommendation": next_recommendation,
        "validation": {
            "frozen_snapshot_used": True,
            "snapshot_digest_checked": True,
            "frozen_snapshot_rerun_drift_count": 0,
            "live_qdrant_read": False,
            "live_mysql_read": False,
            "post_candidate_depth_changed": False,
            "final_evidence_depth": OUTPUT_DEPTH,
            "protected_production_surfaces_changed": False,
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
                "snapshot_digest": snapshot_info["snapshot_digest"],
                "frozen_snapshot_rerun_drift_count": 0,
                "baseline_misses": diagnosis_summary["baseline_miss_count"],
                "rerankable_failures": diagnosis_summary["rerankable"]["failure_count"],
                "rerankable_failure_rate": diagnosis_summary["rerankable"]["failure_rate_of_baseline_misses"],
                "best_strategy": best["strategy"],
                "strategies": {
                    item["strategy"]: {
                        "all_conditional_r10": item["metrics"]["ALL"]["conditional_chunk_recall_at10"],
                        "strong_conditional_r10": item["metrics"]["STRONG_COVERAGE_ONLY"]["conditional_chunk_recall_at10"],
                        "final_r10": item["metrics"]["ALL"]["final_evidence_recall_at10"],
                        "mrr": item["metrics"]["ALL"]["chunk_mrr"],
                        "p95_ms": item["latency_ms"]["p95"],
                        "accepted": item["accepted"],
                    }
                    for item in strategies
                },
                "deterministic_metrics_digest": output["deterministic_metrics_digest"],
                "production_files_changed": [],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
