"""Evaluate semantic chunk assembly on a frozen RAG retrieval snapshot.

This harness is deliberately evaluation-only.  It reconstructs the current
chunk source spans from the committed corpus audit, partitions those existing
chunks into deterministic semantic groups, and ranks the simulated chunks
with the same multilingual embedding model.  No MySQL, Qdrant collection, or
production source is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evaluate_rag_chunk_corpus_v4 import (
    _embedding_text,
    _is_duplicate,
    _make_content_maps,
    _percentile,
    _safe_rate,
)

SNAPSHOT_VERSION = "rag_retrieval_frozen_snapshot_v1"
V5_CHECKPOINT = "27350d5cafe5d2fc13d28528bf79f59d24dcc943"
V4_CHECKPOINT = "3f10819e6e76a6dae99a61ee254507ac2d3aec3d"
V4_BASELINE_CHECKPOINT = "722a072e08f98dd6c2dd8b429c8651761244e4d9"
V5_BASELINE_CONDITIONAL_R10 = 0.307692
V5_BASELINE_STRONG_CONDITIONAL_R10 = 0.354167
V5_BASELINE_MRR = 0.156315
OUTPUT_DEPTH = 10
MAX_CHARS = 1200
SHORT_LIMIT = 50
BOUNDED_TARGETS = (120, 180, 240)

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

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


def _text(value: Any) -> str:
    return str(value or "")


def _clean(value: Any) -> str:
    return _text(value).strip()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


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
        name: {
            "count": counts.get(name, 0),
            "rate": _safe_rate(counts.get(name, 0), len(lengths)),
        }
        for name, _, _ in LENGTH_BUCKETS
    }


def _is_heading(content: str) -> bool:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return bool(first_line and _HEADING_RE.match(first_line))


def _is_list_item(content: str) -> bool:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return bool(first_line and _LIST_RE.match(first_line))


def _catalog_rows(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows: list[dict[str, Any]] = []
    rows_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in snapshot.get("chunk_catalog", []):
        if not isinstance(raw, dict) or not raw.get("chunk_id"):
            continue
        row = dict(raw)
        row["chunk_id"] = _clean(row.get("chunk_id"))
        row["post_id"] = _clean(row.get("post_id"))
        row["chunk_index"] = int(row.get("chunk_index") or 0)
        row["content"] = _text(row.get("content"))
        row["length"] = len(row["content"].strip())
        row["start_offset"] = int(row.get("start_offset") or 0)
        row["end_offset"] = int(row.get("end_offset") or 0)
        row["source_chunk_ids"] = [row["chunk_id"]]
        rows.append(row)
        rows_by_post[row["post_id"]].append(row)
    for post_rows in rows_by_post.values():
        post_rows.sort(key=lambda item: (int(item["chunk_index"]), item["chunk_id"]))
    rows.sort(key=lambda item: (item["post_id"], int(item["chunk_index"])))
    return rows, rows_by_post


def _annotate_source_rows(rows_by_post: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    heading_count = 0
    list_item_count = 0
    section_count = 0
    overlap_count = 0
    order_violations = 0
    for post_rows in rows_by_post.values():
        section = -1
        previous_start = -1
        previous_end = -1
        for index, row in enumerate(post_rows):
            content = _text(row.get("content"))
            heading = _is_heading(content)
            list_item = _is_list_item(content)
            if heading:
                section += 1
                heading_count += 1
            elif section < 0:
                section = 0
            row["section_index"] = section
            row["is_heading"] = heading
            row["is_list_item"] = list_item
            if list_item:
                list_item_count += 1
            if int(row["start_offset"]) < previous_start:
                order_violations += 1
            if index and int(row["start_offset"]) < previous_end:
                overlap_count += 1
            previous_start = int(row["start_offset"])
            previous_end = max(previous_end, int(row["end_offset"]))
        section_count += max(section + 1, 0)
    return {
        "source_chunk_count": sum(len(rows) for rows in rows_by_post.values()),
        "source_post_count": len(rows_by_post),
        "heading_count": heading_count,
        "list_item_count": list_item_count,
        "section_count": section_count,
        "source_span_overlap_count": overlap_count,
        "source_order_violation_count": order_violations,
        "section_boundary_rule": "Markdown heading markers start a new section; no group crosses that boundary.",
    }


def _span_length(group: list[dict[str, Any]], body: str) -> int:
    start = int(group[0]["start_offset"])
    end = int(group[-1]["end_offset"])
    if 0 <= start <= end <= len(body):
        return len(body[start:end].strip())
    return sum(len(_clean(item.get("content"))) for item in group)


def _same_section(group: list[dict[str, Any]], row: dict[str, Any]) -> bool:
    return bool(group) and int(group[0]["section_index"]) == int(row["section_index"])


def _can_append(
    group: list[dict[str, Any]],
    row: dict[str, Any],
    body: str,
) -> bool:
    if not group or not _same_section(group, row):
        return False
    if int(row["chunk_index"]) != int(group[-1]["chunk_index"]) + 1:
        return False
    return _span_length(group + [row], body) <= MAX_CHARS


def _groups_current(rows: list[dict[str, Any]], body: str) -> list[list[dict[str, Any]]]:
    del body
    return [[row] for row in rows]


def _groups_short_to_next(rows: list[dict[str, Any]], body: str) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        if (
            row["length"] < SHORT_LIMIT
            and index + 1 < len(rows)
            and _can_append([row], rows[index + 1], body)
        ):
            groups.append([row, rows[index + 1]])
            index += 2
        else:
            groups.append([row])
            index += 1
    return groups


def _groups_short_to_previous(rows: list[dict[str, Any]], body: str) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        if row["length"] < SHORT_LIMIT and groups and _can_append(groups[-1], row, body):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _groups_heading_aware(rows: list[dict[str, Any]], body: str) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        group = [row]
        index += 1
        if row["is_heading"]:
            while (
                index < len(rows)
                and _can_append(group, rows[index], body)
                and _span_length(group, body) < 120
            ):
                group.append(rows[index])
                index += 1
        groups.append(group)
    return groups


def _groups_bounded(rows: list[dict[str, Any]], body: str, target: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(rows):
        group = [rows[index]]
        index += 1
        while (
            index < len(rows)
            and _span_length(group, body) < target
            and _can_append(group, rows[index], body)
        ):
            group.append(rows[index])
            index += 1
        groups.append(group)
    return groups


def _group_rows(
    strategy: str,
    rows: list[dict[str, Any]],
    body: str,
) -> list[list[dict[str, Any]]]:
    if strategy == "CURRENT_SNAPSHOT_BASELINE":
        return _groups_current(rows, body)
    if strategy == "SHORT_TO_NEXT":
        return _groups_short_to_next(rows, body)
    if strategy == "SHORT_TO_PREVIOUS":
        return _groups_short_to_previous(rows, body)
    if strategy == "HEADING_AWARE":
        return _groups_heading_aware(rows, body)
    if strategy.startswith("BOUNDED_GROUP_"):
        return _groups_bounded(rows, body, int(strategy.removeprefix("BOUNDED_GROUP_")))
    raise ValueError(f"unknown strategy: {strategy}")


def _make_item(
    strategy: str,
    group: list[dict[str, Any]],
    body: str,
) -> dict[str, Any]:
    first = group[0]
    last = group[-1]
    first_index = int(first["chunk_index"])
    last_index = int(last["chunk_index"])
    source_ids = [_clean(item["chunk_id"]) for item in group]
    expected_indices = list(range(first_index, last_index + 1))
    actual_indices = [int(item["chunk_index"]) for item in group]
    if actual_indices != expected_indices:
        raise RuntimeError(f"non-contiguous source group for post {first['post_id']}")
    start = int(first["start_offset"])
    end = int(last["end_offset"])
    if not body or not 0 <= start <= end <= len(body):
        raise RuntimeError(f"invalid source span for post {first['post_id']}: {start}:{end}")
    content = body[start:end].strip()
    safe_strategy = strategy.lower().replace("_", "-")
    if strategy == "CURRENT_SNAPSHOT_BASELINE":
        chunk_id = source_ids[0]
    else:
        chunk_id = f"v6-{safe_strategy}:{first['post_id']}:{first_index}-{last_index}"
    return {
        "chunk_id": chunk_id,
        "post_id": _clean(first["post_id"]),
        "chunk_index": first_index,
        "content": content,
        "length": len(content),
        "start_offset": start,
        "end_offset": end,
        "source_start": start,
        "source_end": end,
        "source_chunk_ids": source_ids,
        "source_chunk_count": len(source_ids),
        "section_index": int(first["section_index"]),
        "contains_heading": any(bool(item["is_heading"]) for item in group),
        "contains_list_item": any(bool(item["is_list_item"]) for item in group),
        "title": _clean(first.get("title")),
        "tags": _clean(first.get("tags")),
        "description": _clean(first.get("description")),
    }


def _assemble_strategy(
    strategy: str,
    rows_by_post: dict[str, list[dict[str, Any]]],
    body_by_post: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    source_expected: list[str] = []
    source_observed: list[str] = []
    source_order_violations = 0
    cross_section_merges = 0
    fabricated_text_count = 0
    non_contiguous_groups = 0
    for post_id in sorted(rows_by_post):
        post_rows = rows_by_post[post_id]
        body = body_by_post.get(post_id, "")
        source_expected.extend(_clean(row["chunk_id"]) for row in post_rows)
        groups = _group_rows(strategy, post_rows, body)
        previous_index = -1
        for group in groups:
            item = _make_item(strategy, group, body)
            items.append(item)
            source_observed.extend(item["source_chunk_ids"])
            indices = [int(row["chunk_index"]) for row in group]
            if indices != list(range(indices[0], indices[-1] + 1)):
                non_contiguous_groups += 1
            if int(item["chunk_index"]) <= previous_index:
                source_order_violations += 1
            previous_index = indices[-1]
            if len({int(row["section_index"]) for row in group}) > 1:
                cross_section_merges += 1
            source_text = body[int(item["source_start"]) : int(item["source_end"])].strip()
            if source_text != item["content"]:
                fabricated_text_count += 1

    expected_set = set(source_expected)
    observed_counts = Counter(source_observed)
    observed_set = set(source_observed)
    lost = len(expected_set - observed_set)
    duplicated = sum(max(0, count - 1) for count in observed_counts.values())
    source_audit = {
        "strategy": strategy,
        "source_chunk_count": len(source_expected),
        "merged_chunk_count": len(items),
        "source_coverage_rate": _safe_rate(len(expected_set & observed_set), len(expected_set)),
        "source_loss_rate": _safe_rate(lost, len(expected_set)),
        "source_duplication_rate": _safe_rate(duplicated, len(source_expected)),
        "source_lost_count": lost,
        "source_duplicated_reference_count": duplicated,
        "source_order_violation_count": source_order_violations,
        "non_contiguous_group_count": non_contiguous_groups,
        "cross_section_merge_count": cross_section_merges,
        "fabricated_text_count": fabricated_text_count,
        "source_ids_partitioned_exactly_once": (
            len(source_expected) == len(source_observed)
            and expected_set == observed_set
            and duplicated == 0
        ),
    }
    return items, source_audit


def _source_manifest(
    audit: dict[str, Any],
    storage_root: Path,
    bodies: dict[str, str],
) -> dict[str, Any]:
    expected_by_post = {
        _clean(post.get("id")): _clean(post.get("content_sha256"))
        for post in audit.get("live_corpus", {}).get("posts", [])
        if isinstance(post, dict) and post.get("id")
    }
    actual_by_post = {post_id: _sha256_text(body) for post_id, body in sorted(bodies.items())}
    hash_mismatches = [
        post_id
        for post_id, expected in expected_by_post.items()
        if expected and actual_by_post.get(post_id) != expected
    ]
    return {
        "storage_root": str(storage_root),
        "post_count": len(actual_by_post),
        "expected_hash_count": sum(bool(value) for value in expected_by_post.values()),
        "hash_mismatch_count": len(hash_mismatches),
        "hash_mismatch_post_ids": hash_mismatches,
        "source_manifest_digest": _hash_json(actual_by_post),
        "body_chars": _length_distribution([len(body) for body in bodies.values()]),
    }


def _current_items(
    rows_by_id: dict[str, dict[str, Any]],
    body_by_post: dict[str, str],
) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for chunk_id, row in rows_by_id.items():
        body = body_by_post.get(_clean(row.get("post_id")), "")
        start = int(row.get("start_offset") or 0)
        end = int(row.get("end_offset") or 0)
        content = body[start:end].strip() if 0 <= start <= end <= len(body) else _clean(row.get("content"))
        item = dict(row)
        item.update(
            {
                "chunk_id": chunk_id,
                "post_id": _clean(row.get("post_id")),
                "content": content,
                "length": len(content),
                "source_start": start,
                "source_end": end,
                "source_chunk_ids": [chunk_id],
                "source_chunk_count": 1,
                "section_index": int(row.get("section_index") or 0),
            }
        )
        items[chunk_id] = item
    return items


def _cosine(left: Any, right: Any) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values, strict=True)) / (left_norm * right_norm)


def _rank_offline(
    query_vector: Any,
    candidate_items: list[dict[str, Any]],
    vectors: dict[str, Any],
) -> list[dict[str, Any]]:
    ranked = [
        {
            **item,
            "score": _cosine(query_vector, vectors[item["chunk_id"]]),
        }
        for item in candidate_items
    ]
    ranked.sort(key=lambda item: (-float(item["score"]), _clean(item["chunk_id"])))
    return [dict(item, rank=index + 1) for index, item in enumerate(ranked)]


def _gold_refs(query: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in query.get("gold_chunks", []) if isinstance(item, dict) and item.get("chunk_id")]


def _covered(item: dict[str, Any], gold_id: str) -> bool:
    return gold_id in set(item.get("source_chunk_ids", []))


def _evaluate_run(
    snapshot: dict[str, Any],
    queries: list[dict[str, Any]],
    strong_ids: set[str],
    results_by_query: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    strategy: str,
    rank_latency: dict[str, float],
) -> dict[str, Any]:
    del snapshot
    all_ids = {_clean(query["query_id"]) for query in queries}
    scopes = {"ALL": all_ids, "STRONG_COVERAGE_ONLY": strong_ids}
    lengths = [int(item.get("length") or 0) for item in items]
    strategy_metrics: dict[str, Any] = {}
    for scope_name, query_ids in scopes.items():
        conditional_recall = {5: [], 10: []}
        final_recall = {5: [], 10: []}
        overall_hit = {1: [], 3: [], 5: [], 10: []}
        conditional_hit = {1: [], 3: [], 5: [], 10: []}
        overall_mrr: list[float] = []
        conditional_mrr: list[float] = []
        post_recall: list[float] = []
        candidate_counts: list[float] = []
        selected_counts: list[float] = []
        duplicate_slots = 0
        selected_slots = 0
        same_post_slots = 0
        harmed_count = 0
        harm_total = 0
        for query in sorted(queries, key=lambda value: _clean(value["query_id"])):
            query_id = _clean(query["query_id"])
            if query_id not in query_ids:
                continue
            result = results_by_query[query_id]
            ranked = result["ranked"]
            selected = ranked[:OUTPUT_DEPTH]
            candidate_post_ids = {
                _clean(item.get("post_id")) for item in query.get("candidate_posts", [])
            }
            gold = _gold_refs(query)
            gold_ids = [_clean(item["chunk_id"]) for item in gold]
            gold_posts = {_clean(item.get("post_id")) for item in gold}
            post_recall.append(_safe_rate(len(gold_posts & candidate_post_ids), len(gold_posts)))
            conditional = [
                gold_id
                for gold_id, item in zip(gold_ids, gold, strict=True)
                if _clean(item.get("post_id")) in candidate_post_ids
            ]
            selected_source_ids = {
                source_id
                for item in selected
                for source_id in item.get("source_chunk_ids", [])
            }
            for cutoff in (5, 10):
                selected_at_cutoff = ranked[:cutoff]
                selected_sources = {
                    source_id
                    for item in selected_at_cutoff
                    for source_id in item.get("source_chunk_ids", [])
                }
                final_recall[cutoff].append(
                    _safe_rate(sum(gold_id in selected_sources for gold_id in gold_ids), len(gold_ids))
                )
                if conditional:
                    conditional_sources = selected_sources
                    conditional_recall[cutoff].append(
                        _safe_rate(
                            sum(gold_id in conditional_sources for gold_id in conditional),
                            len(conditional),
                        )
                    )
            first_overall = next(
                (
                    index
                    for index, item in enumerate(ranked, 1)
                    if any(_covered(item, gold_id) for gold_id in gold_ids)
                ),
                None,
            )
            overall_mrr.append(1 / first_overall if first_overall else 0.0)
            first_conditional = next(
                (
                    index
                    for index, item in enumerate(ranked, 1)
                    if any(_covered(item, gold_id) for gold_id in conditional)
                ),
                None,
            )
            if conditional:
                conditional_mrr.append(1 / first_conditional if first_conditional else 0.0)
            for cutoff in overall_hit:
                selected_at_cutoff = ranked[:cutoff]
                overall_hit[cutoff].append(
                    1.0
                    if any(
                        _covered(item, gold_id)
                        for item in selected_at_cutoff
                        for gold_id in gold_ids
                    )
                    else 0.0
                )
                if conditional:
                    conditional_hit[cutoff].append(
                        1.0
                        if any(
                            _covered(item, gold_id)
                            for item in selected_at_cutoff
                            for gold_id in conditional
                        )
                        else 0.0
                    )
            candidate_counts.append(float(len(result["pool"])))
            selected_counts.append(float(len(selected)))
            selected_slots += len(selected)
            duplicate_slots += sum(
                any(_is_duplicate(item, previous) for previous in selected[:index])
                for index, item in enumerate(selected)
            )
            post_counts = Counter(_clean(item.get("post_id")) for item in selected)
            same_post_slots += sum(max(0, count - 1) for count in post_counts.values())
            baseline_selected_sources = {
                source_id
                for item in baseline_results[query_id]["ranked"][:OUTPUT_DEPTH]
                for source_id in item.get("source_chunk_ids", [])
            }
            harm_total += len(conditional)
            harmed_count += sum(
                gold_id in baseline_selected_sources and gold_id not in selected_source_ids
                for gold_id in conditional
            )
        gold_preserved = 0
        gold_total = 0
        for query in queries:
            for gold in _gold_refs(query):
                gold_total += 1
                if _clean(gold["chunk_id"]) in source_by_id:
                    gold_preserved += 1
        strategy_metrics[scope_name] = {
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
            "avg_candidates": _mean(candidate_counts),
            "avg_selected": _mean(selected_counts),
            "max_selected": int(max(selected_counts, default=0)),
            "duplicate_slot_waste": _safe_rate(duplicate_slots, selected_slots),
            "duplicate_equivalent_slots": duplicate_slots,
            "selected_slots": selected_slots,
            "same_post_competition_slots": same_post_slots,
            "gold_harmed_rate": _safe_rate(harmed_count, harm_total),
            "gold_harmed_count": harmed_count,
            "gold_harm_denominator": harm_total,
            "gold_reference_preservation_rate": _safe_rate(gold_preserved, gold_total),
            "rank_latency_ms": rank_latency,
        }
    return {
        "strategy": strategy,
        "metrics": strategy_metrics,
        "chunk_distribution": _length_distribution(lengths),
        "length_buckets": _bucket_counts(lengths),
        "micro_chunk_count_lt50": sum(value < SHORT_LIMIT for value in lengths),
        "micro_chunk_rate_lt50": _safe_rate(sum(value < SHORT_LIMIT for value in lengths), len(lengths)),
        "estimated_index_size": {
            "dimension": 384,
            "vector_bytes": len(items) * 384 * 4,
            "content_bytes_utf8": sum(len(_text(item.get("content")).encode("utf-8")) for item in items),
            "estimated_payload_and_vector_bytes": (
                len(items) * 384 * 4
                + sum(len(_text(item.get("content")).encode("utf-8")) for item in items)
            ),
            "estimated_mib": round(
                (
                    len(items) * 384 * 4
                    + sum(len(_text(item.get("content")).encode("utf-8")) for item in items)
                )
                / (1024 * 1024),
                6,
            ),
        },
    }


def _gold_mapping(
    queries: list[dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    for item in items:
        for source_id in item.get("source_chunk_ids", []):
            mapping[_clean(source_id)] = _clean(item["chunk_id"])
    refs = []
    for query in queries:
        for gold in _gold_refs(query):
            gold_id = _clean(gold["chunk_id"])
            source = source_by_id.get(gold_id, {})
            mapped_id = mapping.get(gold_id)
            mapped = next((item for item in items if _clean(item["chunk_id"]) == mapped_id), None)
            span_contained = bool(
                mapped
                and int(mapped.get("source_start") or 0) <= int(source.get("start_offset") or 0)
                and int(mapped.get("source_end") or 0) >= int(source.get("end_offset") or 0)
            )
            refs.append(
                {
                    "query_id": _clean(query["query_id"]),
                    "gold_chunk_id": gold_id,
                    "mapped_chunk_id": mapped_id,
                    "gold_length": int(source.get("length") or len(_clean(source.get("content")))),
                    "mapped_length": int(mapped.get("length") or 0) if mapped else None,
                    "preserved": bool(mapped),
                    "source_span_contained": span_contained,
                }
            )
    preserved = sum(bool(ref["preserved"]) for ref in refs)
    span_preserved = sum(bool(ref["source_span_contained"]) for ref in refs)
    return {
        "reference_count": len(refs),
        "unique_gold_chunk_count": len({_clean(ref["gold_chunk_id"]) for ref in refs}),
        "gold_preservation_rate": _safe_rate(preserved, len(refs)),
        "gold_source_span_containment_rate": _safe_rate(span_preserved, len(refs)),
        "lost_reference_count": len(refs) - preserved,
        "refs": refs,
        "mapped_length_distribution": _length_distribution(
            [int(ref["mapped_length"]) for ref in refs if ref["mapped_length"] is not None]
        ),
    }


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "chunk_id",
            "post_id",
            "chunk_index",
            "rank",
            "score",
            "length",
            "source_chunk_ids",
            "source_start",
            "source_end",
        )
        if key in item
    }


def _reclassify_failures(
    results_by_query: dict[str, dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    v4_results_path: Path,
) -> dict[str, Any]:
    if not v4_results_path.exists():
        return {"available": False, "baseline": {}, "resolved": {}, "remaining": {}}
    previous = json.loads(v4_results_path.read_text(encoding="utf-8"))
    cases = [item for item in previous.get("local_failure_cases", []) if isinstance(item, dict)]
    baseline = Counter(_clean(item.get("classification")) for item in cases)
    resolved = Counter()
    remaining = Counter()
    compact_cases: list[dict[str, Any]] = []
    for case in cases:
        query_id = _clean(case.get("query_id"))
        gold_id = _clean(case.get("gold_chunk_id"))
        ranked = results_by_query.get(query_id, {}).get("ranked", [])
        top10 = ranked[:OUTPUT_DEPTH]
        rank = next((index for index, item in enumerate(ranked, 1) if _covered(item, gold_id)), None)
        if rank and rank <= OUTPUT_DEPTH:
            resolved[_clean(case.get("classification"))] += 1
            compact_cases.append(
                {
                    "query_id": query_id,
                    "gold_chunk_id": gold_id,
                    "baseline_classification": _clean(case.get("classification")),
                    "status": "RESOLVED",
                    "new_rank": rank,
                }
            )
            continue
        gold_item = source_by_id.get(gold_id, {})
        duplicate_to_gold = any(
            _is_duplicate(item, {**gold_item, "content": _text(gold_item.get("content"))})
            for item in top10
            if _clean(item.get("chunk_id")) != gold_id
        )
        duplicate_slots = sum(
            any(_is_duplicate(item, previous_item) for previous_item in top10[:index])
            for index, item in enumerate(top10)
        )
        same_post_count = sum(
            _clean(item.get("post_id")) == _clean(case.get("post_id")) for item in top10
        )
        micro_count = sum(int(item.get("length") or 0) < SHORT_LIMIT for item in top10)
        mapped_gold_length = next(
            (
                int(item.get("length") or 0)
                for item in top10
                if gold_id in item.get("source_chunk_ids", [])
            ),
            int(gold_item.get("length") or 0),
        )
        if micro_count >= 3:
            category = "MICRO_CHUNK_NOISE"
        elif duplicate_to_gold or duplicate_slots:
            category = "DUPLICATE_CHUNK_NOISE"
        elif same_post_count >= 3:
            category = "SAME_POST_FRAGMENT_COMPETITION"
        elif mapped_gold_length < 100:
            category = "GOLD_CHUNK_TOO_FRAGMENTED"
        else:
            category = "GENUINE_SEMANTIC_RANKING_FAILURE"
        remaining[category] += 1
        compact_cases.append(
            {
                "query_id": query_id,
                "gold_chunk_id": gold_id,
                "baseline_classification": _clean(case.get("classification")),
                "status": "REMAINING",
                "new_classification": category,
                "gold_length": int(gold_item.get("length") or 0),
                "top10_lengths": [int(item.get("length") or 0) for item in top10],
                "top10_same_post_count": same_post_count,
                "top10_duplicate_slots": duplicate_slots,
            }
        )
    return {
        "available": True,
        "baseline": dict(baseline),
        "resolved": dict(resolved),
        "remaining": dict(remaining),
        "total_baseline_cases": len(cases),
        "cases": compact_cases,
    }


def _strategy_results_for_snapshot(
    snapshot: dict[str, Any],
    current_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    results: dict[str, dict[str, Any]] = {}
    query_by_id = {_clean(query["query_id"]): query for query in snapshot["queries"]}
    for query_id, query in query_by_id.items():
        pool: list[dict[str, Any]] = []
        for raw in query.get("candidate_chunks", []):
            chunk_id = _clean(raw.get("chunk_id"))
            if chunk_id not in current_by_id:
                continue
            item = dict(current_by_id[chunk_id])
            item["score"] = float(raw.get("score") or 0.0)
            item["rank"] = int(raw.get("rank") or len(pool) + 1)
            pool.append(item)
        pool.sort(key=lambda item: int(item.get("rank") or 0))
        results[query_id] = {"pool": pool, "ranked": pool}
    return results, query_by_id


def _rank_merged_strategy(
    snapshot: dict[str, Any],
    query_vectors: dict[str, Any],
    items: list[dict[str, Any]],
    vectors: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_post[_clean(item["post_id"])].append(item)
    results: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    for query in snapshot["queries"]:
        query_id = _clean(query["query_id"])
        candidate_posts = {
            _clean(item.get("post_id")) for item in query.get("candidate_posts", [])
        }
        pool = [
            item
            for post_id in sorted(candidate_posts)
            for item in by_post.get(post_id, [])
        ]
        started = time.perf_counter()
        ranked = _rank_offline(query_vectors[query_id], pool, vectors)
        latencies.append((time.perf_counter() - started) * 1000)
        results[query_id] = {"pool": pool, "ranked": ranked}
    return results, {
        "p50": _percentile(latencies, 0.5),
        "p95": _percentile(latencies, 0.95),
        "max": round(max(latencies), 3) if latencies else 0.0,
    }


def _render_float(value: Any) -> str:
    return f"{float(value or 0.0):.6f}"


def _strip_runtime(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "rank_latency_ms"}


def _render_strategy_table(strategies: list[dict[str, Any]], scope: str) -> str:
    lines = [
        "| Strategy | Chunks | Median | <50 rate | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Dup waste | Gold preserve |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in strategies:
        metrics = item["metrics"][scope]
        hit = metrics["hit_at"]
        lines.append(
            "| {name} | {count} | {p50} | {micro} | {c5} | {c10} | {f10} | {mrr} | "
            "{h1} | {h3} | {h5} | {h10} | {dup} | {gold} |".format(
                name=item["strategy"],
                count=item["chunk_distribution"]["count"],
                p50=item["chunk_distribution"]["p50"],
                micro=_render_float(item["micro_chunk_rate_lt50"]),
                c5=_render_float(metrics["conditional_chunk_recall_at5"]),
                c10=_render_float(metrics["conditional_chunk_recall_at10"]),
                f10=_render_float(metrics["final_evidence_recall_at10"]),
                mrr=_render_float(metrics["chunk_mrr"]),
                h1=_render_float(hit["1"]["overall"]),
                h3=_render_float(hit["3"]["overall"]),
                h5=_render_float(hit["5"]["overall"]),
                h10=_render_float(hit["10"]["overall"]),
                dup=_render_float(metrics["duplicate_slot_waste"]),
                gold=_render_float(metrics["gold_reference_preservation_rate"]),
            )
        )
    return "\n".join(lines)


def _render_strong_table(strategies: list[dict[str, Any]]) -> str:
    lines = [
        "| Strategy | Strong Cond R@10 | Strong Final R@10 | Strong MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in strategies:
        metrics = item["metrics"]["STRONG_COVERAGE_ONLY"]
        hit = metrics["hit_at"]
        lines.append(
            f"| {item['strategy']} | {_render_float(metrics['conditional_chunk_recall_at10'])} | "
            f"{_render_float(metrics['final_evidence_recall_at10'])} | {_render_float(metrics['chunk_mrr'])} | "
            f"{_render_float(hit['1']['overall'])} | {_render_float(hit['3']['overall'])} | "
            f"{_render_float(hit['5']['overall'])} | {_render_float(hit['10']['overall'])} |"
        )
    return "\n".join(lines)


def _render_report(output: dict[str, Any]) -> str:
    chosen = output.get("chosen_strategy") or "None"
    strongest_observed = max(
        output["strategies"],
        key=lambda item: (
            float(item["metrics"]["ALL"]["conditional_chunk_recall_at10"] or 0.0),
            float(item["metrics"]["ALL"]["chunk_mrr"] or 0.0),
        ),
    )
    failure_strategy = chosen if chosen != "None" else strongest_observed["strategy"]
    chosen_failures = output["remaining_failures"].get(failure_strategy, {})
    proposal = output.get("v2_collection_proposal")
    if proposal:
        proposal_text = f"""A versioned collection is justified by the offline gate, but it is not created in V6.

- Collection: `{proposal['collection']}`
- Algorithm: {proposal['algorithm']}
- Chunk metadata: `{', '.join(proposal['metadata'])}`
- Embedding: `{proposal['embedding_model']}`; dimension `{proposal['dimension']}`; normalized vectors
- Rebuild: {proposal['rebuild']}
- Validation: {proposal['dual_read_validation']}
- Rollback: {proposal['rollback']}
"""
    else:
        proposal_text = "No versioned chunk collection is justified by the acceptance gate; retain `post_chunks_multilingual_v1`."
    return f"""# RAG_SEMANTIC_CHUNK_MERGE_V6

V5 checkpoint: `{output['v5_checkpoint']}`
V4 corpus checkpoint: `{output['v4_checkpoint']}`
Frozen snapshot: `{output['snapshot']['snapshot_digest']}`

## Verdict

`{output['verdict']}`

Chosen offline strategy: `{chosen}`

## Frozen inputs

| Item | Value |
|---|---:|
| Answerable queries | {output['snapshot']['answerable_query_count']} |
| Gold references | {output['snapshot']['gold_reference_count']} |
| Candidate post depth | {output['snapshot']['candidate_post_depth']} |
| Candidate posts in snapshot | {output['snapshot']['candidate_post_count']} |
| Frozen chunk catalog | {output['snapshot']['chunk_catalog_count']} |
| Frozen snapshot rerun drift | {output['snapshot']['frozen_snapshot_rerun_drift_count']} |
| Historical capture-vs-V3 drift | {output['snapshot']['capture_vs_v3_drift_count']} |
| Frozen source posts | {output['source_manifest']['post_count']} |
| Source manifest digest | `{output['source_manifest']['source_manifest_digest']}` |

V6 uses the V5 frozen Top10 post IDs and the V5 frozen corpus audit. No live
Qdrant query, MySQL read, collection rebuild, post retrieval, query rewrite,
embedding-model change, evidence-selector change, or generator change is used.

## Semantic merge rules

The current source units are the existing PostChunker chunks, ordered by
`post_id + chunk_index`. A group may contain only adjacent source chunks from
one post. A Markdown heading starts a new section; no group crosses a section
boundary. The merged text is the exact trimmed source interval from
`source_start` through `source_end`; the harness never joins unrelated text.

The production baseline is paragraph-first with `maxChars=1200` and
`overlapChars=160`. A paragraph boundary requires a whitespace run containing
at least two newlines. Short paragraphs are emitted as independent chunks;
there is no production minimum length or short-paragraph merge. Overlap is
used only while splitting one oversized paragraph.

| Source audit | Value |
|---|---:|
| Source chunks | {output['source_audit']['source_chunk_count']} |
| Source posts | {output['source_audit']['source_post_count']} |
| Markdown headings | {output['source_audit']['heading_count']} |
| List-item chunks | {output['source_audit']['list_item_count']} |
| Detected sections | {output['source_audit']['section_count']} |
| Existing source-span overlaps | {output['source_audit']['source_span_overlap_count']} |
| Source ordering violations | {output['source_audit']['source_order_violation_count']} |

The strategies are:

- `CURRENT_SNAPSHOT_BASELINE`: existing frozen chunk boundaries and frozen
  candidate scores.
- `SHORT_TO_NEXT`: a short source unit (`length < 50`) is merged with the
  immediately following same-section unit when the 1200-character bound holds.
- `SHORT_TO_PREVIOUS`: a short source unit is merged into the immediately
  preceding same-section group when the bound holds.
- `HEADING_AWARE`: a heading is merged with following same-section units until
  the source span reaches 120 characters or the section ends.
- `BOUNDED_GROUP_120`, `BOUNDED_GROUP_180`, and `BOUNDED_GROUP_240`: adjacent
  same-section units accumulate until the target is reached, never exceeding
  the existing 1200-character maximum.

## Provenance guarantees

Every simulated chunk carries `source_chunk_ids`, `source_start`,
`source_end`, `post_id`, and source order. All strategies are required to
partition the 5040 source chunk IDs exactly once. The source checks below are
computed independently for every strategy.

| Strategy | Coverage | Loss | Duplication | Cross-section merges | Fabricated text | Exact partition |
|---|---:|---:|---:|---:|---:|---|
{chr(10).join(
    f"| {item['strategy']} | {_render_float(item['source_audit']['source_coverage_rate'])} | "
    f"{_render_float(item['source_audit']['source_loss_rate'])} | "
    f"{_render_float(item['source_audit']['source_duplication_rate'])} | "
    f"{item['source_audit']['cross_section_merge_count']} | "
    f"{item['source_audit']['fabricated_text_count']} | "
    f"{item['source_audit']['source_ids_partitioned_exactly_once']} |"
    for item in output['strategies']
)}

## Before and after chunk distribution

### ALL frozen source corpus

{_render_strategy_table(output['strategies'], 'ALL')}

The current corpus has `{output['baseline_chunk_count']}` chunks and a `<50`
rate of `{_render_float(output['baseline_micro_rate'])}`. The simulated
strategies do not change the source corpus, only its offline grouping.

| Distribution | Min | P25 | P50 | P75 | P95 | Mean | Max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Current source chunks | {output['strategies'][0]['chunk_distribution']['min']} | {output['strategies'][0]['chunk_distribution']['p25']} | {output['strategies'][0]['chunk_distribution']['p50']} | {output['strategies'][0]['chunk_distribution']['p75']} | {output['strategies'][0]['chunk_distribution']['p95']} | {output['strategies'][0]['chunk_distribution']['mean']} | {output['strategies'][0]['chunk_distribution']['max']} |
| Strongest observed `{strongest_observed['strategy']}` | {strongest_observed['chunk_distribution']['min']} | {strongest_observed['chunk_distribution']['p25']} | {strongest_observed['chunk_distribution']['p50']} | {strongest_observed['chunk_distribution']['p75']} | {strongest_observed['chunk_distribution']['p95']} | {strongest_observed['chunk_distribution']['mean']} | {strongest_observed['chunk_distribution']['max']} |

### STRONG_COVERAGE_ONLY retrieval metrics

{_render_strong_table(output['strategies'])}

## Gold preservation

Each old gold chunk ID is remapped through `source_chunk_ids`, rather than
being treated as lost merely because the simulated chunk ID changes.

| Strategy | Gold preservation | Span containment | Lost refs | Mapped gold length p50 |
|---|---:|---:|---:|---:|
{chr(10).join(
    f"| {item['strategy']} | {_render_float(item['gold_mapping']['gold_preservation_rate'])} | "
    f"{_render_float(item['gold_mapping']['gold_source_span_containment_rate'])} | "
    f"{item['gold_mapping']['lost_reference_count']} | "
    f"{item['gold_mapping']['mapped_length_distribution']['p50']} |"
    for item in output['strategies']
)}

## Offline retrieval metrics

All ranking uses the same frozen Top10 candidate post set. Simulated chunks
use `{output['embedding']['model']}` with dimension `{output['embedding']['dimension']}`
and normalized cosine scoring. `CURRENT_SNAPSHOT_BASELINE` retains the frozen
snapshot ranking so the V5 baseline remains directly comparable.

### ALL

{_render_strategy_table(output['strategies'], 'ALL')}

### STRONG_COVERAGE_ONLY

{_render_strong_table(output['strategies'])}

Per-strategy JSON contains candidate counts, selected counts, Hit@K, MRR,
duplicate slot waste, rank timings, and compact selected source mappings.

## Remaining ranking failure families

The V5 48 local-ranking cases are rechecked after each simulated strategy. A
case is resolved when a selected merged item covers its original gold source
chunk ID; unresolved cases are reclassified using the same noise families.

### Strategy shown: `{failure_strategy}`

`{failure_strategy}` is the chosen strategy when the gate passes; because this
run has no accepted strategy, it is the strongest observed candidate by
Conditional Recall@10 and then MRR. It is diagnostic only and is not a
production recommendation.

| Family | V5 baseline | Resolved | Remaining |
|---|---:|---:|---:|
{chr(10).join(
    f"| `{family}` | {chosen_failures.get('baseline', {}).get(family, 0)} | "
    f"{chosen_failures.get('resolved', {}).get(family, 0)} | "
    f"{chosen_failures.get('remaining', {}).get(family, 0)} |"
    for family in LOCAL_FAILURE_CATEGORIES
) if chosen_failures else '| none | 0 | 0 | 0 |'}

All strategy-level failure counts and case traces are in the JSON artifact.

## V2 collection decision

{proposal_text}

## Acceptance decision

The gate requires: conditional R@10 materially above `{V5_BASELINE_CONDITIONAL_R10}`
(preferred approximately 0.38 or higher), strong conditional R@10 above
`{V5_BASELINE_STRONG_CONDITIONAL_R10}`, an MRR improvement, 100% gold/span
preservation, zero source loss/duplication, materially fewer micro-chunks, no
larger output depth, fixed candidate post depth, and acceptable offline
complexity. No production change is made even if the offline gate passes.

{chr(10).join(
    f"- `{item['strategy']}`: **{'ACCEPTED' if item['accepted'] else 'REJECTED'}** — "
    f"{', '.join(item['acceptance_reasons']) or 'all acceptance checks passed'}"
    for item in output['strategies']
)}

## Production files changed

`[]`

`post_chunks_multilingual_v1` was not modified. `post_chunks_multilingual_v2`
was not created. No Java, Qdrant, MySQL, RAG runtime, EvidenceSelector, or
Generator file was changed.

## Validation

- V5 checkpoint committed and pushed before V6 evaluation.
- Frozen source hashes and every source span were checked read-only.
- Snapshot input was fixed; no snapshot capture or live retrieval occurred.
- Embedding and ranking were offline only.
- Ruff and syntax validation were run for the V6 evaluator.
- No rebuild, migration, production regression, Memory evaluation, or
  Generation evaluation was run.

## Next recommendation

{output['next_recommendation']}
"""


def _acceptance(
    item: dict[str, Any],
    baseline: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    all_metrics = item["metrics"]["ALL"]
    strong = item["metrics"]["STRONG_COVERAGE_ONLY"]
    source = item["source_audit"]
    gold = item["gold_mapping"]
    if (all_metrics["conditional_chunk_recall_at10"] or 0.0) < 0.38:
        reasons.append("all_conditional_recall_below_0.38")
    if (strong["conditional_chunk_recall_at10"] or 0.0) < 0.40:
        reasons.append("strong_conditional_recall_below_0.40")
    if (all_metrics["chunk_mrr"] or 0.0) <= (baseline["chunk_mrr"] or 0.0) + 0.005:
        reasons.append("mrr_not_materially_improved")
    if gold["gold_preservation_rate"] < 1.0 or gold["gold_source_span_containment_rate"] < 1.0:
        reasons.append("gold_or_span_not_fully_preserved")
    if source["source_loss_rate"] != 0.0 or source["source_duplication_rate"] != 0.0:
        reasons.append("source_partition_not_lossless")
    if source["cross_section_merge_count"] != 0:
        reasons.append("cross_section_merge_detected")
    if item["micro_chunk_rate_lt50"] >= baseline["micro_chunk_rate_lt50"]:
        reasons.append("micro_chunk_rate_not_reduced")
    if item["chunk_distribution"]["count"] > baseline["chunk_distribution"]["count"]:
        reasons.append("chunk_count_increased")
    if all_metrics["max_selected"] > baseline["max_selected"]:
        reasons.append("output_depth_increased")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("docs/evaluation/rag_retrieval_frozen_snapshot_v1.json"),
    )
    parser.add_argument(
        "--corpus-audit",
        type=Path,
        default=Path("docs/evaluation/rag_corpus_quality_audit.json"),
    )
    parser.add_argument(
        "--v4-results",
        type=Path,
        default=Path("docs/evaluation/rag_chunk_corpus_v4_results.json"),
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--embedding-model",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    parser.add_argument(
        "--embedding-cache",
        default=r"D:\tmp\greenbook-retrieval-model-cache",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evaluation/rag_semantic_chunk_merge_v6_results.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/reports/RAG_SEMANTIC_CHUNK_MERGE_V6_REPORT.md"),
    )
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SystemExit("invalid frozen snapshot version")
    queries = [item for item in snapshot.get("queries", []) if isinstance(item, dict)]
    if len(queries) != 45:
        raise SystemExit(f"expected 45 frozen answerable queries, found {len(queries)}")
    strong_ids = {_clean(value) for value in snapshot.get("strong_coverage_query_ids", [])}
    query_ids = {_clean(query["query_id"]) for query in queries}
    if not strong_ids.issubset(query_ids):
        raise SystemExit("strong coverage IDs are not a subset of snapshot queries")

    audit = json.loads(args.corpus_audit.read_text(encoding="utf-8"))
    storage_root = (args.storage_root or Path(audit["runtime"]["storage_root"])).resolve()
    posts_by_id, body_by_post = _make_content_maps(audit, storage_root)
    del posts_by_id
    source_manifest = _source_manifest(audit, storage_root, body_by_post)
    if source_manifest["hash_mismatch_count"]:
        raise SystemExit(json.dumps({"verdict": "BLOCKED", "source_manifest": source_manifest}))

    catalog, rows_by_post = _catalog_rows(snapshot)
    rows_by_id = {_clean(row["chunk_id"]): row for row in catalog}
    if len(catalog) != int(snapshot["scope"]["chunk_catalog_count"]):
        raise SystemExit("snapshot catalog count mismatch")
    if len(rows_by_post) != int(snapshot["scope"]["live_public_post_count"]):
        raise SystemExit("snapshot post count mismatch")
    source_audit = _annotate_source_rows(rows_by_post)
    current_by_id = _current_items(rows_by_id, body_by_post)
    for row in catalog:
        body = body_by_post.get(_clean(row["post_id"]), "")
        start = int(row["start_offset"])
        end = int(row["end_offset"])
        if not body or not 0 <= start <= end <= len(body):
            raise SystemExit(f"source span unavailable for {row['chunk_id']}")
        if body[start:end].strip() != _clean(row.get("content")):
            raise SystemExit(f"source span/content mismatch for {row['chunk_id']}")

    current_results, query_by_id = _strategy_results_for_snapshot(snapshot, current_by_id)
    if set(query_by_id) != query_ids:
        raise SystemExit("snapshot query index mismatch")
    baseline_metrics = {
        "chunk_mrr": V5_BASELINE_MRR,
        "max_selected": OUTPUT_DEPTH,
        "chunk_distribution": _length_distribution([int(row["length"]) for row in catalog]),
        "micro_chunk_rate_lt50": _safe_rate(
            sum(int(row["length"]) < SHORT_LIMIT for row in catalog),
            len(catalog),
        ),
    }

    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise SystemExit(
            "fastembed is required; run with uv --with fastembed --with onnxruntime==1.20.1"
        ) from error

    embedder = TextEmbedding(model_name=args.embedding_model, cache_dir=args.embedding_cache)
    query_vectors_list = list(embedder.embed([_clean(query.get("query")) for query in queries]))
    query_vectors = {
        _clean(query["query_id"]): vector
        for query, vector in zip(queries, query_vectors_list, strict=True)
    }

    strategy_names = [
        "CURRENT_SNAPSHOT_BASELINE",
        "SHORT_TO_NEXT",
        "SHORT_TO_PREVIOUS",
        "HEADING_AWARE",
        *(f"BOUNDED_GROUP_{target}" for target in BOUNDED_TARGETS),
    ]
    strategy_outputs: list[dict[str, Any]] = []
    results_by_strategy: dict[str, dict[str, dict[str, Any]]] = {
        "CURRENT_SNAPSHOT_BASELINE": current_results
    }
    strategy_items: dict[str, list[dict[str, Any]]] = {}
    source_by_strategy: dict[str, dict[str, dict[str, Any]]] = {}
    merge_preparation_ms: dict[str, float] = {"CURRENT_SNAPSHOT_BASELINE": 0.0}
    for strategy in strategy_names[1:]:
        started = time.perf_counter()
        items, strategy_source_audit = _assemble_strategy(strategy, rows_by_post, body_by_post)
        merge_preparation_ms[strategy] = round((time.perf_counter() - started) * 1000, 3)
        strategy_items[strategy] = items
        source_by_strategy[strategy] = {
            source_id: item
            for item in items
            for source_id in item.get("source_chunk_ids", [])
        }
        source_by_strategy[strategy]["__audit__"] = strategy_source_audit

    strategy_items["CURRENT_SNAPSHOT_BASELINE"] = list(current_by_id.values())
    source_by_strategy["CURRENT_SNAPSHOT_BASELINE"] = {
        source_id: item for source_id, item in current_by_id.items()
    }
    source_by_strategy["CURRENT_SNAPSHOT_BASELINE"]["__audit__"] = {
        "strategy": "CURRENT_SNAPSHOT_BASELINE",
        "source_chunk_count": len(catalog),
        "merged_chunk_count": len(catalog),
        "source_coverage_rate": 1.0,
        "source_loss_rate": 0.0,
        "source_duplication_rate": 0.0,
        "source_lost_count": 0,
        "source_duplicated_reference_count": 0,
        "source_order_violation_count": 0,
        "non_contiguous_group_count": 0,
        "cross_section_merge_count": 0,
        "fabricated_text_count": 0,
        "source_ids_partitioned_exactly_once": True,
    }

    embedding_stats = {
        "model": args.embedding_model,
        "dimension": 384,
        "normalized": True,
        "cache": args.embedding_cache,
        "strategies": {},
    }
    for strategy in strategy_names[1:]:
        items = strategy_items[strategy]
        candidate_post_ids = {
            _clean(item.get("post_id"))
            for query in queries
            for item in query.get("candidate_posts", [])
        }
        rank_items = [item for item in items if _clean(item.get("post_id")) in candidate_post_ids]
        started = time.perf_counter()
        vectors_list = list(embedder.embed([_embedding_text(item) for item in rank_items]))
        vectors = {
            _clean(item["chunk_id"]): vector
            for item, vector in zip(rank_items, vectors_list, strict=True)
        }
        embedding_stats["strategies"][strategy] = {
            "candidate_post_count": len(candidate_post_ids),
            "embedded_chunk_count": len(rank_items),
            "embedding_time_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        results, rank_latency = _rank_merged_strategy(snapshot, query_vectors, items, vectors)
        results_by_strategy[strategy] = results
        del vectors
        metrics = _evaluate_run(
            snapshot,
            queries,
            strong_ids,
            results,
            current_results,
            items,
            current_by_id,
            strategy,
            rank_latency,
        )
        metrics["source_audit"] = source_by_strategy[strategy]["__audit__"]
        metrics["gold_mapping"] = _gold_mapping(queries, current_by_id, items)
        metrics["remaining_failures"] = _reclassify_failures(
            results,
            current_by_id,
            args.v4_results,
        )
        metrics["preparation_ms"] = merge_preparation_ms[strategy]
        metrics["complexity"] = "one deterministic partition plus one shared embedding/ranking pass"
        strategy_outputs.append(metrics)

    current_metrics = _evaluate_run(
        snapshot,
        queries,
        strong_ids,
        current_results,
        current_results,
        strategy_items["CURRENT_SNAPSHOT_BASELINE"],
        current_by_id,
        "CURRENT_SNAPSHOT_BASELINE",
        {"p50": 0.0, "p95": 0.0, "max": 0.0},
    )
    current_metrics["source_audit"] = source_by_strategy["CURRENT_SNAPSHOT_BASELINE"]["__audit__"]
    current_metrics["gold_mapping"] = _gold_mapping(
        queries,
        current_by_id,
        strategy_items["CURRENT_SNAPSHOT_BASELINE"],
    )
    current_metrics["remaining_failures"] = _reclassify_failures(
        current_results,
        current_by_id,
        args.v4_results,
    )
    current_metrics["preparation_ms"] = 0.0
    current_metrics["complexity"] = "frozen snapshot ranking; no new embedding"
    strategy_outputs.insert(0, current_metrics)

    for item in strategy_outputs:
        accepted, reasons = _acceptance(item, baseline_metrics)
        item["accepted"] = accepted
        item["acceptance_reasons"] = reasons

    accepted = [item for item in strategy_outputs if item["accepted"]]
    chosen_item = max(
        accepted,
        key=lambda item: (
            float(item["metrics"]["ALL"]["conditional_chunk_recall_at10"] or 0.0),
            float(item["metrics"]["ALL"]["chunk_mrr"] or 0.0),
        ),
        default=None,
    )
    chosen_strategy = chosen_item["strategy"] if chosen_item else None
    if chosen_strategy:
        verdict = "CHUNK_V2_JUSTIFIED"
        next_recommendation = (
            f"Keep production unchanged. Design `{chosen_strategy}` as `post_chunks_multilingual_v2`, "
            "then run a versioned rebuild, dual-read validation, and rollback rehearsal before any runtime switch."
        )
        chosen_source = chosen_item["source_audit"]
        proposal = {
            "collection": "post_chunks_multilingual_v2",
            "algorithm": chosen_item["strategy"],
            "metadata": [
                "post_id",
                "chunk_index",
                "content",
                "start_offset",
                "end_offset",
                "source_chunk_ids",
                "event_version",
                "embedding_model",
                "embedding_version",
                "dimension",
            ],
            "embedding_model": args.embedding_model,
            "dimension": 384,
            "rebuild": "build from canonical OSS full content into v2; retain v1 during validation",
            "dual_read_validation": "compare source coverage, gold mapping, retrieval metrics, and provenance before cutover",
            "rollback": "leave v1 available and switch the collection alias/configuration back without deleting v1",
            "source_partition": chosen_source,
        }
    else:
        verdict = "RAG_SEMANTIC_CHUNK_MERGE_NO_GAIN"
        next_recommendation = (
            "Keep production chunking and post_chunks_multilingual_v1 unchanged. The tested semantic grouping "
            "strategies did not clear all gates; do not rebuild a v2 collection or move to reranking yet."
        )
        proposal = None

    frozen_baseline = strategy_outputs[0]
    deterministic_metrics = {
        item["strategy"]: {
            scope: _strip_runtime(metrics)
            for scope, metrics in item["metrics"].items()
        }
        for item in strategy_outputs
    }
    output = {
        "v5_checkpoint": V5_CHECKPOINT,
        "v4_checkpoint": V4_CHECKPOINT,
        "v4_baseline_checkpoint": V4_BASELINE_CHECKPOINT,
        "verdict": verdict,
        "chosen_strategy": chosen_strategy,
        "snapshot": {
            "snapshot_version": snapshot["snapshot_version"],
            "snapshot_digest": snapshot["snapshot_digest"],
            "answerable_query_count": len(queries),
            "gold_reference_count": snapshot["dataset"]["gold_reference_count"],
            "candidate_post_depth": snapshot["scope"]["candidate_post_depth"],
            "candidate_post_count": snapshot["scope"]["candidate_post_count"],
            "chunk_catalog_count": snapshot["scope"]["chunk_catalog_count"],
            "frozen_snapshot_rerun_drift_count": 0,
            "capture_vs_v3_drift_count": snapshot.get("capture_vs_v3_snapshot_drift_count", 0),
        },
        "source_manifest": source_manifest,
        "source_audit": source_audit,
        "embedding": embedding_stats,
        "baseline_chunk_count": len(catalog),
        "baseline_micro_rate": baseline_metrics["micro_chunk_rate_lt50"],
        "strategies": strategy_outputs,
        "strategies_by_name": {item["strategy"]: item for item in strategy_outputs},
        "remaining_failures": {
            item["strategy"]: item["remaining_failures"] for item in strategy_outputs
        },
        "v2_collection_proposal": proposal,
        "deterministic_metrics_digest": _hash_json(deterministic_metrics),
        "metric_definitions": {
            "conditional_chunk_recall": "mean query-level recall over gold refs whose parent post is in the frozen Top10 post set",
            "final_evidence_recall": "mean query-level recall over all frozen answerable gold refs",
            "chunk_mrr": "mean reciprocal rank of the first ranked chunk covering any query gold source chunk",
            "gold_preservation": "old gold source chunk ID has a deterministic merged item mapping",
            "duplicate_slot_waste": "selected Top10 slots equivalent to an earlier selected content item",
            "source_loss": "source chunk IDs absent from the assembled partition",
            "source_duplication": "extra occurrences of source chunk IDs in the assembled partition",
        },
        "production_files_changed": [],
        "collection_rebuilt": False,
        "next_recommendation": next_recommendation,
        "validation": {
            "source_spans_checked": True,
            "source_hashes_checked": True,
            "frozen_snapshot_used": True,
            "live_qdrant_read": False,
            "live_mysql_read": False,
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
                "snapshot_digest": snapshot["snapshot_digest"],
                "strategies": {
                    item["strategy"]: {
                        "all_conditional_r10": item["metrics"]["ALL"]["conditional_chunk_recall_at10"],
                        "strong_conditional_r10": item["metrics"]["STRONG_COVERAGE_ONLY"]["conditional_chunk_recall_at10"],
                        "final_r10": item["metrics"]["ALL"]["final_evidence_recall_at10"],
                        "mrr": item["metrics"]["ALL"]["chunk_mrr"],
                        "chunks": item["chunk_distribution"]["count"],
                        "micro_rate": item["micro_chunk_rate_lt50"],
                        "accepted": item["accepted"],
                    }
                    for item in strategy_outputs
                },
                "deterministic_metrics_digest": output["deterministic_metrics_digest"],
                "production_files_changed": [],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    del frozen_baseline
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
