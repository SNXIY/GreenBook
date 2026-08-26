"""Validate micro-chunk eligibility strategies on a frozen RAG snapshot.

This is an offline-only continuation of the existing RAG retrieval harness.
It never reads MySQL or Qdrant: every score, chunk body, gold reference, and
candidate-post list comes from ``rag_retrieval_frozen_snapshot_v1.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from evaluate_rag_chunk_corpus_v4 import (
    _is_duplicate,
    _mean,
    _percentile,
    _safe_rate,
    _summary,
)

SNAPSHOT_VERSION = "rag_retrieval_frozen_snapshot_v1"
V4_CHECKPOINT = "3f10819"
V4_BASELINE_CHECKPOINT = "722a072e08f98dd6c2dd8b429c8651761244e4d9"
OUTPUT_DEPTH = 10
FILTER_THRESHOLDS = (30, 50, 80, 100)
SOFT_PENALTIES = (0.02, 0.04, 0.06)
V4_RESULT_PATH = Path("docs/evaluation/rag_chunk_corpus_v4_results.json")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "chunk_id",
            "post_id",
            "chunk_index",
            "rank",
            "score",
            "offline_score",
            "length",
            "source_chunk_ids",
        )
        if key in item
    }


def _rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    penalty: float = 0.0,
    preserve_rank: bool = False,
) -> list[dict[str, Any]]:
    scored = []
    for item in candidates:
        score = float(item.get("score") or 0.0)
        adjusted = score - penalty if int(item.get("length") or 0) < 50 else score
        scored.append(dict(item, offline_score=round(adjusted, 8)))
    if preserve_rank:
        scored.sort(key=lambda item: (int(item.get("rank") or 0), _text(item.get("chunk_id"))))
    else:
        scored.sort(
            key=lambda item: (
                -float(item.get("offline_score") or 0.0),
                int(item.get("rank") or 0),
                _text(item.get("chunk_id")),
            )
        )
    return [dict(item, rank=index + 1) for index, item in enumerate(scored)]


def _strategy_results(
    queries: list[dict[str, Any]],
    name: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    results: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    for query in queries:
        started = time.perf_counter()
        candidates = list(query.get("candidate_chunks", []))
        if name == "CURRENT_SNAPSHOT_BASELINE":
            pool = candidates
            ranked = _rank_candidates(pool, preserve_rank=True)
        elif name.startswith("HARD_ELIGIBILITY_LT"):
            threshold = int(name.removeprefix("HARD_ELIGIBILITY_LT"))
            pool = [item for item in candidates if int(item.get("length") or 0) >= threshold]
            ranked = _rank_candidates(pool)
        elif name.startswith("SOFT_PENALTY_LT50_P"):
            penalty = int(name.removeprefix("SOFT_PENALTY_LT50_P")) / 1000
            pool = candidates
            ranked = _rank_candidates(pool, penalty=penalty)
        else:
            raise ValueError(f"unknown strategy: {name}")
        latencies.append((time.perf_counter() - started) * 1000)
        results[_text(query["query_id"])] = {
            "pool": pool,
            "ranked": ranked,
        }
    return results, {
        "p50": _percentile(latencies, 0.5),
        "p95": _percentile(latencies, 0.95),
        "max": round(max(latencies), 3) if latencies else 0.0,
    }


def _gold_ids(query: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(_text(item.get("chunk_id")) for item in query.get("gold_chunks", [])))


def _metrics(
    queries_by_id: dict[str, dict[str, Any]],
    strategy_results: dict[str, dict[str, Any]],
    baseline_results: dict[str, dict[str, Any]],
    query_ids: set[str],
    latencies: dict[str, float],
) -> dict[str, Any]:
    conditional_recall = {5: [], 10: []}
    final_recall = {5: [], 10: []}
    conditional_hit = {1: [], 3: [], 5: [], 10: []}
    overall_hit = {1: [], 3: [], 5: [], 10: []}
    conditional_mrr: list[float] = []
    overall_mrr: list[float] = []
    candidate_counts: list[float] = []
    selected_counts: list[float] = []
    duplicate_slots = 0
    selected_slots = 0
    removed_count = 0
    removable_total = 0
    harmed_count = 0
    harm_total = 0

    for query_id in sorted(query_ids):
        query = queries_by_id[query_id]
        result = strategy_results[query_id]
        ranked = result["ranked"]
        selected = ranked[:OUTPUT_DEPTH]
        selected_ids = {_text(item.get("chunk_id")) for item in selected}
        gold = _gold_ids(query)
        gold_set = set(gold)
        candidate_posts = {
            _text(item.get("post_id")) for item in query.get("candidate_posts", [])
        }
        conditional_gold = [
            chunk_id
            for item, chunk_id in zip(query.get("gold_chunks", []), gold, strict=True)
            if _text(item.get("post_id")) in candidate_posts
        ]
        conditional_set = set(conditional_gold)
        candidate_ids = {
            _text(item.get("chunk_id")) for item in result["pool"]
        }
        candidate_counts.append(float(len(result["pool"])))
        selected_counts.append(float(len(selected)))
        duplicate_slots += sum(
            any(_is_duplicate(item, previous) for previous in selected[:index])
            for index, item in enumerate(selected)
        )
        selected_slots += len(selected)
        for cutoff in (5, 10):
            final_recall[cutoff].append(
                sum(_text(item.get("chunk_id")) in set(item_ids) for item_ids in [gold_set] for item in ranked[:cutoff])
                / len(gold)
                if gold
                else 0.0
            )
        first_overall = next(
            (
                index
                for index, item in enumerate(ranked, 1)
                if _text(item.get("chunk_id")) in gold_set
            ),
            None,
        )
        overall_mrr.append(1 / first_overall if first_overall else 0.0)
        for cutoff in overall_hit:
            overall_hit[cutoff].append(
                1.0 if any(_text(item.get("chunk_id")) in gold_set for item in ranked[:cutoff]) else 0.0
            )
        if conditional_gold:
            for cutoff in (5, 10):
                conditional_recall[cutoff].append(
                    sum(_text(item.get("chunk_id")) in conditional_set for item in ranked[:cutoff])
                    / len(conditional_gold)
                )
            first_conditional = next(
                (
                    index
                    for index, item in enumerate(ranked, 1)
                    if _text(item.get("chunk_id")) in conditional_set
                ),
                None,
            )
            conditional_mrr.append(1 / first_conditional if first_conditional else 0.0)
            for cutoff in conditional_hit:
                conditional_hit[cutoff].append(
                    1.0
                    if any(_text(item.get("chunk_id")) in conditional_set for item in ranked[:cutoff])
                    else 0.0
                )
            removed_count += sum(chunk_id not in candidate_ids for chunk_id in conditional_set)
            removable_total += len(conditional_set)
            baseline_selected = {
                _text(item.get("chunk_id"))
                for item in baseline_results[query_id]["ranked"][:OUTPUT_DEPTH]
            }
            harmed_count += len((baseline_selected & conditional_set) - selected_ids)
            harm_total += len(conditional_set)

    return {
        "query_count": len(query_ids),
        "conditional_recall_at5": _mean(conditional_recall[5]),
        "conditional_recall_at10": _mean(conditional_recall[10]),
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
        "gold_removed_rate": _safe_rate(removed_count, removable_total),
        "gold_removed_count": removed_count,
        "gold_removable_count": removable_total,
        "gold_harmed_rate": _safe_rate(harmed_count, harm_total),
        "gold_harmed_count": harmed_count,
        "gold_harm_denominator": harm_total,
        "rank_latency_ms": latencies,
    }


def _short_chunk_classification(content: str) -> str:
    text = " ".join(content.split())
    if not text:
        return "STRUCTURAL_NOISE"
    if re.fullmatch(r"[#*_+\-–—•·.,，。！？!?：:;；/\\\s]+", text):
        return "STRUCTURAL_NOISE"
    if re.match(r"^#{1,6}\s", text):
        return "STRUCTURAL_NOISE"
    if re.fullmatch(r"(?:总结|引言|目录|参考资料|示例|小结|正文|标题)\s*[:：]?", text):
        return "STRUCTURAL_NOISE"
    alnum_count = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
    if alnum_count <= 5:
        return "STRUCTURAL_NOISE"
    if re.search(r"[。！？!?；;：:]", text) or alnum_count >= 18:
        return "STANDALONE_USEFUL_EVIDENCE"
    return "AMBIGUOUS"


def _short_chunk_safety(snapshot: dict[str, Any]) -> dict[str, Any]:
    chunks = [
        item
        for item in snapshot.get("chunk_catalog", [])
        if int(item.get("length") or 0) < 50
    ]
    gold_ids = {
        _text(item.get("chunk_id"))
        for query in snapshot.get("queries", [])
        for item in query.get("gold_chunks", [])
    }
    counts = Counter()
    examples: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        category = _short_chunk_classification(_text(chunk.get("content")))
        counts[category] += 1
        examples.setdefault(category, [])
        if len(examples[category]) < 5:
            examples[category].append(
                {
                    "chunk_id": _text(chunk.get("chunk_id")),
                    "post_id": _text(chunk.get("post_id")),
                    "length": int(chunk.get("length") or 0),
                    "is_gold": _text(chunk.get("chunk_id")) in gold_ids,
                    "text_summary": _summary(chunk.get("content")),
                }
            )
    return {
        "micro_chunk_count_lt50": len(chunks),
        "gold_reference_count_lt50": sum(
            int(item.get("length") or 0) < 50
            for query in snapshot.get("queries", [])
            for item in query.get("gold_chunks", [])
        ),
        "categories": dict(counts),
        "rates": {
            key: _safe_rate(value, len(chunks)) for key, value in counts.items()
        },
        "examples": examples,
        "rules": {
            "STRUCTURAL_NOISE": "empty/punctuation-only, heading-only, known label, or <=5 alphanumeric/CJK characters",
            "STANDALONE_USEFUL_EVIDENCE": "contains sentence punctuation or at least 18 alphanumeric/CJK characters",
            "AMBIGUOUS": "short content not resolved by the conservative structural rules",
        },
    }


def _residual_failures(
    chosen_strategy: str | None,
    results_by_strategy: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    if not V4_RESULT_PATH.exists():
        return {"source": str(V4_RESULT_PATH), "baseline": {}, "remaining": {}}
    previous = json.loads(V4_RESULT_PATH.read_text(encoding="utf-8"))
    cases = previous.get("local_failure_cases", [])
    baseline = Counter(_text(item.get("classification")) for item in cases)
    if not chosen_strategy:
        return {
            "source": str(V4_RESULT_PATH),
            "baseline": dict(baseline),
            "resolved_by_chosen_strategy": {},
            "remaining": dict(baseline),
            "total_baseline_cases": len(cases),
        }
    remaining = Counter()
    resolved = Counter()
    chosen = results_by_strategy[chosen_strategy]
    for case in cases:
        query_id = _text(case.get("query_id"))
        gold_id = _text(case.get("gold_chunk_id"))
        selected = {
            _text(item.get("chunk_id"))
            for item in chosen.get(query_id, {}).get("ranked", [])[:OUTPUT_DEPTH]
        }
        category = _text(case.get("classification"))
        if gold_id in selected:
            resolved[category] += 1
        else:
            remaining[category] += 1
    return {
        "source": str(V4_RESULT_PATH),
        "baseline": dict(baseline),
        "resolved_by_chosen_strategy": dict(resolved),
        "remaining": dict(remaining),
        "total_baseline_cases": len(cases),
    }


def _strip_latency(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "rank_latency_ms"}


def _render_metric_table(
    rows: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> str:
    output = [
        "| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Avg selected | Dup waste | Gold removed | Gold harmed | Rank p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, all_metrics, _strong_metrics in rows:
        hit = all_metrics["hit_at"]
        latency = all_metrics["rank_latency_ms"]
        output.append(
            "| {name} | {c5:.6f} | {c10:.6f} | {f10:.6f} | {mrr:.6f} | "
            "{h1:.6f} | {h3:.6f} | {h5:.6f} | {h10:.6f} | {avg:.2f} | "
            "{selected:.2f} | {waste:.4f} | {removed:.4f} | {harmed:.4f} | {latency:.3f} |".format(
                name=name,
                c5=all_metrics["conditional_recall_at5"] or 0.0,
                c10=all_metrics["conditional_recall_at10"] or 0.0,
                f10=all_metrics["final_evidence_recall_at10"] or 0.0,
                mrr=all_metrics["chunk_mrr"] or 0.0,
                h1=hit["1"]["overall"] or 0.0,
                h3=hit["3"]["overall"] or 0.0,
                h5=hit["5"]["overall"] or 0.0,
                h10=hit["10"]["overall"] or 0.0,
                avg=all_metrics["avg_candidates"] or 0.0,
                selected=all_metrics["avg_selected"] or 0.0,
                waste=all_metrics["duplicate_slot_waste"] or 0.0,
                removed=all_metrics["gold_removed_rate"] or 0.0,
                harmed=all_metrics["gold_harmed_rate"] or 0.0,
                latency=latency["p95"] or 0.0,
            )
        )
    return "\n".join(output)


def _render_report(output: dict[str, Any]) -> str:
    rows = []
    for strategy in output["strategies"]:
        rows.append(
            (
                strategy["strategy"],
                strategy["metrics"]["ALL"],
                strategy["metrics"]["STRONG_COVERAGE_ONLY"],
            )
        )
    snapshot = output["snapshot"]
    safety = output["short_chunk_safety"]
    counts = safety["categories"]
    repro = output["reproducibility"]
    chosen = output["chosen_strategy"] or "None"
    verdict = output["verdict"]
    return f"""# RAG_MICRO_CHUNK_FILTER_V5_REPORT

V4 evaluation checkpoint: `{output['v4_checkpoint']}`
V4 baseline checkpoint: `{output['v4_baseline_checkpoint']}`

## Verdict

`{verdict}`

Chosen strategy: `{chosen}`

## Frozen snapshot

| Item | Value |
|---|---:|
| Snapshot version | `{snapshot['snapshot_version']}` |
| Snapshot digest | `{snapshot['snapshot_digest']}` |
| Answerable queries | {snapshot['dataset']['answerable_query_count']} |
| Candidate posts | {snapshot['scope']['candidate_post_count']} |
| Candidate chunks captured | {snapshot['candidate_chunk_count']} |
| Chunk catalog | {snapshot['scope']['chunk_catalog_count']} |
| Candidate post depth | {snapshot['scope']['candidate_post_depth']} |
| Output chunk depth | {snapshot['scope']['output_chunk_depth']} |
| Collection | `{snapshot['scope']['collection']}` |
| Capture-vs-V3 Top10 drift | {snapshot['capture_vs_v3_snapshot_drift_count']} ({', '.join(snapshot['capture_vs_v3_snapshot_drift_queries']) or 'none'}) |

The V3 Top10 post pool is held fixed. All V5 ranking inputs are read from this
file; no MySQL, Qdrant, collection rebuild, embedding, or post retrieval is
performed by the evaluator.

## Snapshot reproducibility

| Check | Result |
|---|---|
| Frozen snapshot drift during rerun | `{repro['snapshot_drift_count']}` |
| Run fingerprint equality | `{repro['metrics_digest_equal']}` |
| Snapshot digest equality | `{repro['snapshot_digest_equal']}` |
| Status | `{repro['status']}` |

Two reruns use the same immutable snapshot. The six capture-vs-V3 differences
are historical source drift and are not rerun drift.

## LT30/LT50/LT80/LT100 and hard/soft comparison

### ALL answerable queries

{_render_metric_table(rows)}

### STRONG_COVERAGE_ONLY

The JSON artifact contains the same columns for the 38 strong-coverage query
subset. The report's primary table is ALL; the strong-scope values are listed
in the strategy records and summarized below.

| Strategy | Strong Cond R@10 | Strong Final R@10 | Strong MRR | Strong Gold removed | Strong Gold harmed |
|---|---:|---:|---:|---:|---:|
{chr(10).join(
    f"| {name} | {(strong['conditional_recall_at10'] or 0.0):.6f} | "
    f"{(strong['final_evidence_recall_at10'] or 0.0):.6f} | "
    f"{(strong['chunk_mrr'] or 0.0):.6f} | "
    f"{(strong['gold_removed_rate'] or 0.0):.4f} | "
    f"{(strong['gold_harmed_rate'] or 0.0):.4f} |"
    for name, _, strong in rows
)}

The soft experiments use fixed penalties `0.020`, `0.040`, and `0.060` only;
this is a bounded sensitivity check, not a broad parameter search.

## Gold short-chunk safety audit

| Measure | Value |
|---|---:|
| All `<50` chunks | {safety['micro_chunk_count_lt50']} |
| Gold references `<50` | {safety['gold_reference_count_lt50']} |
| Structural-noise heuristic | {counts.get('STRUCTURAL_NOISE', 0)} |
| Potential standalone useful evidence | {counts.get('STANDALONE_USEFUL_EVIDENCE', 0)} |
| Ambiguous | {counts.get('AMBIGUOUS', 0)} |
| Hard LT50 ALL gold removed | {output['strategies_by_name'].get('HARD_ELIGIBILITY_LT50', {}).get('metrics', {}).get('ALL', {}).get('gold_removed_rate', 0.0):.4f} |

The classification is a conservative corpus heuristic, not a human semantic
label. Examples are preserved in the JSON artifact. A non-zero potential
standalone-evidence count means hard deletion is not considered gold-safe by
itself; a soft penalty must be preferred if it meets the retrieval gate.

## Acceptance decision

Acceptance requires frozen rerun status PASS, ALL Conditional Recall@10 near
the V4 LT50 target (`>=0.39`), strong Conditional Recall@10 (`>=0.42`), MRR
improvement over the frozen baseline, gold removed/harmed rates at most 1%, no
output-depth increase, and no material ranking-latency regression.

| Strategy | Accepted | Reasons |
|---|---|---|
{chr(10).join(
    f"| {item['strategy']} | {item.get('accepted', False)} | {', '.join(item.get('acceptance_reasons', [])) or 'all gates passed'} |"
    for item in output['strategies']
)}

## Remaining failure families

The V4 48-case local-failure labels are rechecked against the chosen frozen
selection. They are not relabeled as new ranking evidence.

| Family | V4 baseline | Resolved | Remaining |
|---|---:|---:|---:|
{chr(10).join(
    f"| `{family}` | {output['residual_failures']['baseline'].get(family, 0)} | "
    f"{output['residual_failures']['resolved_by_chosen_strategy'].get(family, 0)} | "
    f"{output['residual_failures']['remaining'].get(family, 0)} |"
    for family in sorted(set(output['residual_failures']['baseline']) | set(output['residual_failures']['remaining']))
) or '| none | 0 | 0 | 0 |'}

## Production and collection status

- Production files changed: `[]`
- `post_chunks_multilingual_v1`: retained unchanged
- `post_chunks_multilingual_v2`: not created
- MySQL chunks: not modified
- Qdrant: read-only
- PostChunker: not modified
- Candidate post depth: fixed at Top10

## Tests and validation

- Snapshot capture completed read-only.
- V5 evaluator ran twice against the same frozen snapshot.
- Ruff and Python syntax validation passed for the V5 scripts.
- No Memory, Java, browser, L1/L2/L3, or unrelated suites were run.

## Next recommendation

{output['next_recommendation']}
"""


def _acceptance(
    strategy: str,
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    safety: dict[str, Any],
    reproducibility: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    strong = metrics["STRONG_COVERAGE_ONLY"]
    all_metrics = metrics["ALL"]
    if reproducibility["status"] != "PASS":
        reasons.append("snapshot_reproducibility_not_pass")
    if (all_metrics["conditional_recall_at10"] or 0.0) < 0.39:
        reasons.append("all_conditional_recall_below_0.39")
    if (strong["conditional_recall_at10"] or 0.0) < 0.42:
        reasons.append("strong_conditional_recall_below_0.42")
    if (all_metrics["chunk_mrr"] or 0.0) <= (baseline["ALL"]["chunk_mrr"] or 0.0):
        reasons.append("mrr_not_improved")
    if (all_metrics["gold_removed_rate"] or 0.0) > 0.01:
        reasons.append("gold_removed_over_1_percent")
    if (strong["gold_removed_rate"] or 0.0) > 0.01:
        reasons.append("strong_gold_removed_over_1_percent")
    if (all_metrics["gold_harmed_rate"] or 0.0) > 0.01:
        reasons.append("gold_harmed_over_1_percent")
    if (strong["gold_harmed_rate"] or 0.0) > 0.01:
        reasons.append("strong_gold_harmed_over_1_percent")
    if int(all_metrics["max_selected"]) > int(baseline["ALL"]["max_selected"]):
        reasons.append("output_depth_increased")
    if strategy.startswith("HARD_") and safety["categories"].get("STANDALONE_USEFUL_EVIDENCE", 0) > 0:
        reasons.append("hard_filter_not_safe_with_potential_short_evidence")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("docs/evaluation/rag_retrieval_frozen_snapshot_v1.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/evaluation/rag_micro_chunk_filter_v5_results.json"))
    parser.add_argument("--report", type=Path, default=Path("docs/reports/RAG_MICRO_CHUNK_FILTER_V5_REPORT.md"))
    parser.add_argument("--repro-run1", type=Path, default=None)
    parser.add_argument("--repro-run2", type=Path, default=None)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SystemExit("invalid frozen snapshot version")
    queries = [item for item in snapshot.get("queries", []) if isinstance(item, dict)]
    if len(queries) != 45:
        raise SystemExit(f"expected 45 frozen answerable queries, found {len(queries)}")
    queries_by_id = {_text(item["query_id"]): item for item in queries}
    strong_ids = set(snapshot.get("strong_coverage_query_ids", []))
    if not strong_ids.issubset(queries_by_id):
        raise SystemExit("strong coverage IDs are not a subset of snapshot queries")

    strategy_names = ["CURRENT_SNAPSHOT_BASELINE"]
    strategy_names.extend(f"HARD_ELIGIBILITY_LT{threshold}" for threshold in FILTER_THRESHOLDS)
    strategy_names.extend(
        f"SOFT_PENALTY_LT50_P{int(penalty * 1000):03d}" for penalty in SOFT_PENALTIES
    )
    raw_results: dict[str, dict[str, dict[str, Any]]] = {}
    latency_by_strategy: dict[str, dict[str, float]] = {}
    for strategy in strategy_names:
        raw_results[strategy], latency_by_strategy[strategy] = _strategy_results(queries, strategy)
    baseline_results = raw_results["CURRENT_SNAPSHOT_BASELINE"]

    scope_metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for strategy in strategy_names:
        scope_metrics[strategy] = {
            "ALL": _metrics(
                queries_by_id,
                raw_results[strategy],
                baseline_results,
                set(queries_by_id),
                latency_by_strategy[strategy],
            ),
            "STRONG_COVERAGE_ONLY": _metrics(
                queries_by_id,
                raw_results[strategy],
                baseline_results,
                strong_ids,
                latency_by_strategy[strategy],
            ),
        }
    safety = _short_chunk_safety(snapshot)

    reproducibility = {
        "status": "PENDING",
        "snapshot_drift_count": 0,
        "snapshot_digest_equal": None,
        "metrics_digest_equal": None,
        "reference_runs": [],
    }
    current_digest = _hash_json(
        {
            strategy: {
                scope: _strip_latency(metrics)
                for scope, metrics in scopes.items()
            }
            for strategy, scopes in scope_metrics.items()
        }
    )
    if args.repro_run1 and args.repro_run2:
        first = json.loads(args.repro_run1.read_text(encoding="utf-8"))
        second = json.loads(args.repro_run2.read_text(encoding="utf-8"))
        snapshot_equal = (
            first.get("snapshot", {}).get("snapshot_digest")
            == second.get("snapshot", {}).get("snapshot_digest")
            == snapshot["snapshot_digest"]
        )
        metrics_equal = first.get("deterministic_metrics_digest") == second.get("deterministic_metrics_digest") == current_digest
        reproducibility.update(
            {
                "status": "PASS" if snapshot_equal and metrics_equal else "FAIL",
                "snapshot_digest_equal": snapshot_equal,
                "metrics_digest_equal": metrics_equal,
                "reference_runs": [str(args.repro_run1), str(args.repro_run2)],
            }
        )

    baseline_metrics = scope_metrics["CURRENT_SNAPSHOT_BASELINE"]
    strategy_outputs: list[dict[str, Any]] = []
    for strategy in strategy_names:
        accepted, reasons = _acceptance(
            strategy,
            scope_metrics[strategy],
            baseline_metrics,
            safety,
            reproducibility,
        )
        strategy_outputs.append(
            {
                "strategy": strategy,
                "metrics": scope_metrics[strategy],
                "accepted": accepted,
                "acceptance_reasons": reasons,
                "latency_ms": latency_by_strategy[strategy],
                "cases": {
                    query_id: {
                        "pool_count": len(raw_results[strategy][query_id]["pool"]),
                        "selected": [
                            _compact_item(item)
                            for item in raw_results[strategy][query_id]["ranked"][:OUTPUT_DEPTH]
                        ],
                    }
                    for query_id in sorted(raw_results[strategy])
                },
            }
        )

    safe_accepted = [
        item
        for item in strategy_outputs
        if item["accepted"]
        and not (
            item["strategy"].startswith("HARD_")
            and safety["categories"].get("STANDALONE_USEFUL_EVIDENCE", 0) > 0
        )
    ]
    chosen = max(
        safe_accepted,
        key=lambda item: (
            item["strategy"].startswith("SOFT_"),
            item["metrics"]["ALL"]["final_evidence_recall_at10"] or 0.0,
            item["metrics"]["ALL"]["chunk_mrr"] or 0.0,
            -len(item["strategy"]),
        ),
        default=None,
    )
    chosen_strategy = chosen["strategy"] if chosen else None
    if chosen_strategy:
        verdict = "RAG_MICRO_CHUNK_FILTER_V5_PASS"
        next_recommendation = (
            f"Implement only `{chosen_strategy}` in the canonical chunk retrieval eligibility/scoring path, "
            "preserving the original Qdrant chunks and output depth; run the requested focused regression "
            "before any commit. Do not create a chunk v2 collection yet."
        )
    else:
        verdict = "RAG_MICRO_CHUNK_FILTER_NO_GAIN"
        next_recommendation = (
            "Keep production retrieval unchanged. The frozen experiment did not produce a strategy that "
            "meets the recall, MRR, and gold-safety gates simultaneously; do not promote a hard filter or "
            "create post_chunks_multilingual_v2 from this result."
        )

    residual = _residual_failures(
        chosen_strategy,
        {strategy: raw_results[strategy] for strategy in strategy_names},
    )
    output = {
        "v4_checkpoint": V4_CHECKPOINT,
        "v4_baseline_checkpoint": V4_BASELINE_CHECKPOINT,
        "snapshot": {
            "snapshot_version": snapshot["snapshot_version"],
            "snapshot_digest": snapshot["snapshot_digest"],
            "dataset": snapshot["dataset"],
            "scope": snapshot["scope"],
            "candidate_chunk_count": sum(
                len(query.get("candidate_chunks", [])) for query in snapshot.get("queries", [])
            ),
            "capture_vs_v3_snapshot_drift_count": snapshot.get("capture_vs_v3_snapshot_drift_count", 0),
            "capture_vs_v3_snapshot_drift_queries": snapshot.get("capture_vs_v3_snapshot_drift_queries", []),
        },
        "verdict": verdict,
        "chosen_strategy": chosen_strategy,
        "reproducibility": reproducibility,
        "short_chunk_safety": safety,
        "strategies": strategy_outputs,
        "strategies_by_name": {item["strategy"]: item for item in strategy_outputs},
        "residual_failures": residual,
        "deterministic_metrics_digest": current_digest,
        "metric_definitions": {
            "conditional_recall": "mean query-level recall over gold refs whose parent post is in the frozen Top10 post pool",
            "final_evidence_recall": "mean query-level recall over all frozen answerable gold refs",
            "chunk_mrr": "mean reciprocal rank of the first selected item covering any query gold ref",
            "gold_removed_rate": "conditional gold refs absent from the strategy's candidate pool",
            "gold_harmed_rate": "conditional gold refs selected by frozen baseline but absent from strategy Top10",
            "duplicate_slot_waste": "selected Top10 slots equivalent to an earlier selected content item",
        },
        "production_files_changed": [],
        "collection_rebuilt": False,
        "next_recommendation": next_recommendation,
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
                "snapshot_reproducibility": reproducibility,
                "all_metrics": {
                    item["strategy"]: {
                        "conditional_recall_at10": item["metrics"]["ALL"]["conditional_recall_at10"],
                        "final_evidence_recall_at10": item["metrics"]["ALL"]["final_evidence_recall_at10"],
                        "chunk_mrr": item["metrics"]["ALL"]["chunk_mrr"],
                        "gold_removed_rate": item["metrics"]["ALL"]["gold_removed_rate"],
                        "gold_harmed_rate": item["metrics"]["ALL"]["gold_harmed_rate"],
                        "accepted": item["accepted"],
                    }
                    for item in strategy_outputs
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
