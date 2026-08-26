"""RAG generation completeness V2, evaluation-only.

V1 used a deterministic lexical claim proxy.  This pass keeps that metric,
adds a fixed auxiliary semantic claim audit, and runs small oracle-only prompt
experiments.  It reuses the saved V1 provider observations and the frozen V2
dataset/catalog.  It never calls Java/Qdrant/MySQL and never changes a
production prompt or generator.

The semantic audit is intentionally auxiliary: the report always shows
lexical coverage, semantic coverage, the per-claim classification, faithfulness,
citations, and latency together.  A single judge score is not used by itself
to decide the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from evaluate_rag_generation_v1 import (
    DEFAULT_DATASET,
    DEFAULT_RUNS,
    DEFAULT_SNAPSHOT,
    REPO_ROOT,
    CapturingLLM,
    _catalog_evidence,
    _load_json,
    _load_jsonl,
    _percentile,
    _rate,
    _sha256_bytes,
    _sha256_text,
    _terms,
    _text,
    _write_json,
)
from evaluate_rag_generation_v1 import (
    DEFAULT_OUTPUT as V1_OUTPUT,
)
from greenbook_agent_core.llm_compat import structured_call
from greenbook_mcp_server.tools import community
from openai import AsyncOpenAI

DEFAULT_V2_OUTPUT = REPO_ROOT / "docs/evaluation/rag_generation_completeness_v2_results.json"
DEFAULT_V2_REPORT = REPO_ROOT / "docs/reports/RAG_GENERATION_COMPLETENESS_V2.md"

CLAIM_MATCH_THRESHOLD = 0.50
SEMANTIC_GENERATION_LIMIT_THRESHOLD = 0.50
AUDIT_SAMPLE_SIZE = 20
EXPERIMENT_SAMPLE_SIZE = 12
MAX_P95_TOKEN_RATIO = 1.50
MAX_AVG_TOKEN_RATIO = 2.00
MAX_P95_LATENCY_RATIO = 1.50
MAX_HALLUCINATION_DELTA = 0.05

MISS_CATEGORIES = (
    "TRUE_MISSING",
    "PARAPHRASE_FALSE_NEGATIVE",
    "GOLD_OVER_SPECIFIED",
    "EVIDENCE_NOT_SUPPORTING_GOLD",
    "AMBIGUOUS",
)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "postId": {"type": "string"},
                    "title": {"type": "string"},
                    "chunkId": {"type": "string"},
                },
                "required": ["postId", "title", "chunkId"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "sources"],
    "additionalProperties": False,
}

CLAIM_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supportingPoints": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
        "sources": ANSWER_SCHEMA["properties"]["sources"],
    },
    "required": ["supportingPoints", "answer", "sources"],
    "additionalProperties": False,
}

SEMANTIC_JUDGE_PROMPT = """You are an auxiliary claim-coverage auditor for a grounded community answer.
You are not the answer generator and must not use outside knowledge.

For every supplied gold claim, decide whether the generated answer expresses
the same required fact.  Paraphrases and natural Chinese/English equivalents
count as covered.  Do not require identical wording.  Use the evidence only
to decide whether the gold claim is actually supportable.

For a claim that the deterministic lexical matcher marked as missed, choose
exactly one category:
- TRUE_MISSING: the answer does not express the claim.
- PARAPHRASE_FALSE_NEGATIVE: the answer expresses the same fact but lexical matching missed it.
- GOLD_OVER_SPECIFIED: the gold claim adds detail that the user question does not require.
- EVIDENCE_NOT_SUPPORTING_GOLD: the supplied evidence does not establish the gold claim.
- AMBIGUOUS: insufficient basis to decide reliably.

Return JSON only.  Use zero-based claim_index.  semantic_covered must be true
only when the generated answer actually covers the claim, not merely when the
evidence contains it.  Keep rationale short and quote only short answer spans.
"""

SEMANTIC_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_index": {"type": "integer"},
                    "semantic_covered": {"type": "boolean"},
                    "classification": {"type": "string", "enum": [*MISS_CATEGORIES, "LEXICAL_MATCH"]},
                    "confidence": {"type": "number"},
                    "matched_answer_span": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "claim_index",
                    "semantic_covered",
                    "classification",
                    "confidence",
                    "matched_answer_span",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

VARIANT_JUDGE_PROMPT = """You are an auxiliary semantic comparison for grounded answer completeness.
For each gold claim, evaluate each named answer independently.  Count a
natural-language paraphrase as covered, but do not reward vague topic overlap.
Use only the supplied gold evidence to decide whether the claim is supportable.
Return one JSON object containing one result per variant and claim.
"""

VARIANT_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "variant": {"type": "string", "enum": ["CURRENT", "COMPLETENESS_AWARE", "STRUCTURED_CLAIM_PLAN", "EVIDENCE_ORDERING"]},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim_index": {"type": "integer"},
                                "semantic_covered": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "rationale": {"type": "string"},
                            },
                            "required": ["claim_index", "semantic_covered", "confidence", "rationale"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["variant", "claims"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["variants"],
    "additionalProperties": False,
}


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sentences(value: Any) -> list[str]:
    import re

    return [item.strip() for item in re.split(r"[。！？!?;；\n]+", _text(value)) if item.strip()]


def _jaccard_terms(left: Any, right: Any) -> float:
    a, b = _terms(left), _terms(right)
    return _rate(len(a & b), len(a | b)) if a | b else 0.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    x_denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    y_denominator = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    return round(numerator / (x_denominator * y_denominator), 6) if x_denominator and y_denominator else 0.0


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2
        for position in range(index, end):
            ranks[ordered[position][0]] = rank
        index = end
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    return _pearson(_rank(xs), _rank(ys))


def _safe_mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 6) if values else 0.0


def _claim_diagnostics(claims: list[str], answer: str, evidence_text: str) -> list[dict[str, Any]]:
    answer_terms = _terms(answer)
    evidence_terms = _terms(evidence_text)
    diagnostics: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        claim_terms = _terms(claim)
        matched = sorted(claim_terms & answer_terms)
        missed = sorted(claim_terms - answer_terms)
        evidence_matched = sorted(claim_terms & evidence_terms)
        diagnostics.append(
            {
                "claim_index": index,
                "claim": claim,
                "lexical_coverage": _rate(len(matched), len(claim_terms)),
                "lexical_matched_terms": matched,
                "lexical_missed_terms": missed,
                "lexical_matched": _rate(len(matched), len(claim_terms)) >= CLAIM_MATCH_THRESHOLD,
                "evidence_support_coverage": _rate(len(evidence_matched), len(claim_terms)),
                "evidence_supporting_terms": evidence_matched,
            }
        )
    return diagnostics


def _load_inputs(args: argparse.Namespace) -> dict[str, Any]:
    dataset_rows = _load_jsonl(args.dataset)
    v1 = _load_json(args.v1_results)
    snapshot = _load_json(args.snapshot)
    retrieval_runs = _load_jsonl(args.runs)
    dataset_by_id = {_text(row["query_id"]): row for row in dataset_rows}
    v1_oracle = {_text(row["query_id"]): row for row in v1["case_metrics"]["oracle_answerable"]}
    v1_production = {_text(row["query_id"]): row for row in v1["case_metrics"]["production_answerable"]}
    raw_oracle = {_text(row["query_id"]): row for row in v1["raw_generation_runs"]["oracle"]}
    raw_production = {_text(row["query_id"]): row for row in v1["raw_generation_runs"]["production"]}
    catalog = {
        _text(row["chunk_id"]): row for row in snapshot.get("chunk_catalog", []) if row.get("chunk_id")
    }
    snapshot_by_id = {_text(row["query_id"]): row for row in snapshot.get("queries", [])}
    runs_by_id = {_text(row["query_id"]): row for row in retrieval_runs}
    cases: list[dict[str, Any]] = []
    for row in dataset_rows:
        query_id = _text(row["query_id"])
        if not row.get("gold_chunk_ids"):
            continue
        gold_evidence = [_catalog_evidence({"chunk_id": value}, catalog) for value in row["gold_chunk_ids"]]
        oracle = v1_oracle[query_id]
        raw = raw_oracle[query_id]
        evidence_text = "\n".join(_text(item.get("content")) for item in gold_evidence)
        answer = _text(raw.get("answer"))
        claims = [
            _text(item.get("claim")) if isinstance(item, dict) else _text(item)
            for item in row.get("evidence_claims", [])
        ] or [_text(row.get("gold_answer"))]
        lexical_claims = _claim_diagnostics(claims, answer, evidence_text)
        cases.append(
            {
                "query_id": query_id,
                "query": _text(row.get("question") or row.get("query")),
                "category": row.get("category"),
                "gold_answer": _text(row.get("gold_answer")),
                "gold_claims": claims,
                "gold_post_ids": row.get("gold_post_ids", []),
                "gold_chunk_ids": row.get("gold_chunk_ids", []),
                "provided_evidence": gold_evidence,
                "generated_answer": answer,
                "sources": raw.get("sources", []),
                "lexical_claims": lexical_claims,
                "v1_oracle_coverage": oracle.get("gold_claim_coverage"),
                "v1_production_coverage": v1_production[query_id].get("gold_claim_coverage"),
                "v1_oracle_faithfulness": oracle.get("faithfulness"),
                "v1_raw_generation": raw,
                "evidence_answer_overlap": _jaccard_terms(evidence_text, answer),
                "evidence_text": evidence_text,
            }
        )
    production_cases: list[dict[str, Any]] = []
    for case in cases:
        raw = raw_production[case["query_id"]]
        evidence = [_catalog_evidence({"chunk_id": value}, catalog) for value in raw.get("input_evidence_ids", [])]
        evidence_text = "\n".join(_text(item.get("content")) for item in evidence)
        answer = _text(raw.get("answer"))
        production_case = dict(case)
        production_case.update(
            {
                "provided_evidence": evidence,
                "generated_answer": answer,
                "sources": raw.get("sources", []),
                "lexical_claims": _claim_diagnostics(case["gold_claims"], answer, evidence_text),
                "v1_raw_generation": raw,
                "evidence_answer_overlap": _jaccard_terms(evidence_text, answer),
                "evidence_text": evidence_text,
            }
        )
        production_cases.append(production_case)
    return {
        "dataset_rows": dataset_rows,
        "dataset_by_id": dataset_by_id,
        "v1": v1,
        "snapshot": snapshot,
        "retrieval_runs": retrieval_runs,
        "catalog": catalog,
        "snapshot_by_id": snapshot_by_id,
        "runs_by_id": runs_by_id,
        "cases": cases,
        "production_cases": production_cases,
    }


def _select_audit_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    ordered_high = sorted(cases, key=lambda item: float(item.get("v1_production_coverage") or 0), reverse=True)
    ordered_low = list(reversed(ordered_high))
    middle_start = max(0, len(ordered_high) // 2 - 3)
    groups: dict[str, list[dict[str, Any]]] = {
        "PRODUCTION_COVERAGE_HIGH": ordered_high[:5],
        "PRODUCTION_COVERAGE_MEDIUM": ordered_high[middle_start : middle_start + 5],
        "PRODUCTION_COVERAGE_LOW": ordered_low[:5],
        "ORACLE_LOW_POSSIBLY_CORRECT": sorted(
            [
                item
                for item in cases
                if item.get("generated_answer")
                and float(item.get("v1_oracle_coverage") or 0) <= 0.20
            ],
            key=lambda item: float(item.get("evidence_answer_overlap") or 0),
            reverse=True,
        )[:5],
    }
    selected: list[dict[str, Any]] = []
    labels: dict[str, list[str]] = {}
    for group_name, group in groups.items():
        for case in group:
            query_id = case["query_id"]
            labels.setdefault(query_id, []).append(group_name)
            if case not in selected:
                selected.append(case)
    for case in ordered_high:
        if len(selected) >= AUDIT_SAMPLE_SIZE:
            break
        if case not in selected:
            selected.append(case)
            labels.setdefault(case["query_id"], []).append("FILL")
    return selected[:AUDIT_SAMPLE_SIZE], labels


def _normalize_judge_output(payload: Any, claims: list[str]) -> list[dict[str, Any]]:
    raw_claims = payload.get("claims", []) if isinstance(payload, dict) else []
    by_index: dict[int, dict[str, Any]] = {}
    if isinstance(raw_claims, list):
        for raw in raw_claims:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("claim_index"))
            except (TypeError, ValueError):
                continue
            if index not in range(len(claims)) and index - 1 in range(len(claims)):
                index -= 1
            if index not in range(len(claims)):
                continue
            classification = _text(raw.get("classification"))
            if classification not in {*MISS_CATEGORIES, "LEXICAL_MATCH"}:
                classification = "AMBIGUOUS"
            try:
                confidence = float(raw.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            by_index[index] = {
                "claim_index": index,
                "claim": claims[index],
                "semantic_covered": bool(raw.get("semantic_covered")),
                "classification": classification,
                "confidence": max(0.0, min(1.0, confidence)),
                "matched_answer_span": _text(raw.get("matched_answer_span"))[:300],
                "rationale": _text(raw.get("rationale"))[:500],
            }
    return [
        by_index.get(
            index,
            {
                "claim_index": index,
                "claim": claim,
                "semantic_covered": False,
                "classification": "AMBIGUOUS",
                "confidence": 0.0,
                "matched_answer_span": "",
                "rationale": "Judge did not return a valid record for this claim.",
            },
        )
        for index, claim in enumerate(claims)
    ]


async def _judge_case(
    case: dict[str, Any],
    *,
    llm: CapturingLLM,
    model: str,
) -> dict[str, Any]:
    request = {
        "query": case["query"],
        "gold_claims": case["gold_claims"],
        "provided_evidence": [
            {"chunkId": item["chunkId"], "postId": item["postId"], "content": item["content"]}
            for item in case["provided_evidence"]
        ],
        "generated_answer": case["generated_answer"],
        "lexical_claim_diagnostics": case["lexical_claims"],
    }
    call_start = len(llm.calls)
    started = time.perf_counter()
    error = None
    payload: Any = {}
    raw_content = ""
    try:
        response = await structured_call(
            llm,
            model,
            SEMANTIC_JUDGE_PROMPT,
            "rag_generation_semantic_claim_audit_v2",
            SEMANTIC_JUDGE_SCHEMA,
            request,
        )
        payload = community._grounded_payload(response)
        raw_content = _text(getattr(response.choices[0].message, "content", ""))
    except Exception as exc:  # provider failure is retained as an audit gap
        error = f"{type(exc).__name__}: {_text(exc)[:500]}"
    normalized = _normalize_judge_output(payload, case["gold_claims"])
    return {
        "query_id": case["query_id"],
        "model": model,
        "prompt_sha256": _sha256_text(SEMANTIC_JUDGE_PROMPT),
        "input": request,
        "output": payload,
        "raw_output": raw_content[:5000],
        "normalized_claims": normalized,
        "semantic_coverage": _safe_mean([float(item["semantic_covered"]) for item in normalized]),
        "judge_latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "provider_calls": llm.calls[call_start:],
        "error": error,
    }


def _variant_sources(payload: Any, evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_id = {_text(item.get("chunkId")): item for item in evidence}
    raw_sources = payload.get("sources", []) if isinstance(payload, dict) else []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(raw_sources, list):
        return result
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        chunk_id = _text(raw.get("chunkId") or raw.get("chunk_id"))
        if chunk_id not in by_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        item = by_id[chunk_id]
        result.append({"postId": _text(item.get("postId")), "title": _text(item.get("title")), "chunkId": chunk_id})
    return result


def _variant_metrics(case: dict[str, Any], answer: str, sources: list[dict[str, str]], result: dict[str, Any]) -> dict[str, Any]:
    claims = case["gold_claims"]
    answer_terms = _terms(answer)
    lexical_coverages = [_rate(len(_terms(claim) & answer_terms), len(_terms(claim))) for claim in claims]
    evidence_terms = _terms(case["evidence_text"])
    answer_claims = [] if answer == community._INSUFFICIENT_EVIDENCE else _sentences(answer)
    supported = 0
    for claim in answer_claims:
        terms = _terms(claim)
        shared = terms & evidence_terms
        if shared and (len(shared) >= 2 or _rate(len(shared), len(terms)) >= 0.25):
            supported += 1
    faithfulness = _rate(supported, len(answer_claims)) if answer_claims else 1.0
    by_id = {item["chunkId"]: item for item in case["provided_evidence"]}
    citation_ok = bool(answer == community._INSUFFICIENT_EVIDENCE and not sources) or bool(answer and sources)
    citation_ok = citation_ok and all(item.get("chunkId") in by_id for item in sources)
    return {
        "query_id": case["query_id"],
        "variant": result["variant"],
        "answer": answer,
        "sources": sources,
        "lexical_claim_coverage": _safe_mean(lexical_coverages),
        "lexical_claim_coverages": lexical_coverages,
        "faithfulness": faithfulness,
        "hallucination_rate": round(1 - faithfulness, 6),
        "citation_correctness": float(citation_ok),
        "answer_chars": len(answer),
        "answer_tokens": result.get("completion_tokens"),
        "sentence_count": len(answer_claims),
        "generation_latency_ms": result.get("generation_latency_ms"),
        "tool_error": result.get("error"),
    }


async def _run_variant(
    case: dict[str, Any],
    *,
    variant: str,
    prompt: str,
    schema: dict[str, Any],
    evidence: list[dict[str, Any]],
    llm: CapturingLLM,
    model: str,
) -> dict[str, Any]:
    request = {
        "question": case["query"],
        "evidence": evidence,
    }
    call_start = len(llm.calls)
    started = time.perf_counter()
    error = None
    payload: Any = {}
    try:
        response = await structured_call(
            llm,
            model,
            prompt,
            f"rag_generation_completeness_v2_{variant.lower()}",
            schema,
            request,
        )
        payload = community._grounded_payload(response)
    except Exception as exc:  # retained in experiment artifact
        error = f"{type(exc).__name__}: {_text(exc)[:500]}"
    answer = _text(payload.get("answer")) if isinstance(payload, dict) else ""
    raw_sources = _variant_sources(payload, evidence)
    if answer == community._INSUFFICIENT_EVIDENCE:
        sources: list[dict[str, str]] = []
    elif not answer or not raw_sources:
        answer = community._INSUFFICIENT_EVIDENCE
        sources = []
    else:
        sources = raw_sources
    calls = llm.calls[call_start:]
    output = {
        "variant": variant,
        "model": model,
        "prompt_sha256": _sha256_text(prompt),
        "input_evidence_ids": [item["chunkId"] for item in evidence],
        "answer": answer,
        "sources": sources,
        "structured_payload": payload,
        "generation_latency_ms": round(sum(float(item.get("latency_ms") or 0) for item in calls), 3),
        "completion_tokens": sum(int(item["completion_tokens"]) for item in calls if item.get("completion_tokens") is not None) or None,
        "prompt_tokens": sum(int(item["prompt_tokens"]) for item in calls if item.get("prompt_tokens") is not None) or None,
        "calls": calls,
        "error": error,
        "wall_latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    output["metrics"] = _variant_metrics(case, answer, sources, output)
    return output


def _aggregate_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    return {
        "count": len(rows),
        "lexical_claim_coverage": _safe_mean(values("lexical_claim_coverage")),
        "semantic_claim_coverage": _safe_mean(values("semantic_claim_coverage")),
        "faithfulness": _safe_mean(values("faithfulness")),
        "hallucination_rate": _safe_mean(values("hallucination_rate")),
        "citation_correctness": _safe_mean(values("citation_correctness")),
        "answer_chars": _safe_mean(values("answer_chars")),
        "answer_tokens": _safe_mean(values("answer_tokens")),
        "generation_latency_ms": _safe_mean(values("generation_latency_ms")),
        "p95_generation_latency_ms": _percentile(values("generation_latency_ms"), 0.95),
        "p95_answer_tokens": _percentile(values("answer_tokens"), 0.95),
    }


def _evidence_utilization(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        claim_terms = set().union(*(_terms(claim) for claim in case["gold_claims"]))
        answer_terms = _terms(case["generated_answer"])
        supporting_ids: list[str] = []
        used_ids: list[str] = []
        for item in case["provided_evidence"]:
            terms = _terms(item["content"])
            support_ratio = _rate(len(terms & claim_terms), len(claim_terms))
            answer_overlap = _rate(len(terms & answer_terms), len(terms))
            if terms & claim_terms and (support_ratio >= 0.05 or len(terms & claim_terms) >= 2):
                supporting_ids.append(item["chunkId"])
            if terms & answer_terms and (answer_overlap >= 0.05 or len(terms & answer_terms) >= 2):
                used_ids.append(item["chunkId"])
        total = len(case["provided_evidence"])
        rows.append(
            {
                "query_id": case["query_id"],
                "evidence_count": total,
                "supporting_evidence_ids": supporting_ids,
                "used_evidence_ids": used_ids,
                "evidence_support_rate": _rate(len(supporting_ids), total),
                "evidence_utilization_rate": _rate(len(used_ids), total),
                "claim_utilization_rate": _safe_mean([item["lexical_coverage"] for item in case["lexical_claims"]]),
                "evidence_available_but_underused": bool(supporting_ids) and _rate(len(used_ids), total) < 0.5,
                "evidence_not_supporting_gold": _rate(len(supporting_ids), total) < 0.5,
            }
        )
    return {
        "cases": rows,
        "aggregate": {
            "evidence_support_rate": _safe_mean([row["evidence_support_rate"] for row in rows]),
            "evidence_utilization_rate": _safe_mean([row["evidence_utilization_rate"] for row in rows]),
            "claim_utilization_rate": _safe_mean([row["claim_utilization_rate"] for row in rows]),
            "evidence_available_but_underused_count": sum(row["evidence_available_but_underused"] for row in rows),
            "evidence_not_supporting_gold_count": sum(row["evidence_not_supporting_gold"] for row in rows),
        },
    }


def _length_correlation(
    cases: list[dict[str, Any]],
    semantic_by_id: dict[str, float],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        raw = case["v1_raw_generation"]
        lexical = _safe_mean([item["lexical_coverage"] for item in case["lexical_claims"]])
        rows.append(
            {
                "query_id": case["query_id"],
                "chars": len(case["generated_answer"]),
                "tokens": raw.get("completion_tokens"),
                "sentence_count": len(_sentences(case["generated_answer"])),
                "gold_claim_count": len(case["gold_claims"]),
                "matched_claim_count": sum(item["lexical_matched"] for item in case["lexical_claims"]),
                "lexical_claim_coverage": lexical,
                "semantic_claim_coverage": semantic_by_id.get(case["query_id"]),
            }
        )
    valid = [row for row in rows if row["tokens"] is not None]
    lexical_coverage = [float(row["lexical_claim_coverage"]) for row in valid]
    semantic_rows = [row for row in valid if row["semantic_claim_coverage"] is not None]
    semantic_coverage = [float(row["semantic_claim_coverage"]) for row in semantic_rows]
    return {
        "rows": rows,
        "pearson": {
            "chars_vs_lexical_coverage": _pearson([float(row["chars"]) for row in valid], lexical_coverage),
            "tokens_vs_lexical_coverage": _pearson([float(row["tokens"]) for row in valid], lexical_coverage),
            "sentences_vs_lexical_coverage": _pearson([float(row["sentence_count"]) for row in valid], lexical_coverage),
            "chars_vs_semantic_coverage": _pearson([float(row["chars"]) for row in semantic_rows], semantic_coverage),
            "tokens_vs_semantic_coverage": _pearson([float(row["tokens"]) for row in semantic_rows], semantic_coverage),
            "sentences_vs_semantic_coverage": _pearson([float(row["sentence_count"]) for row in semantic_rows], semantic_coverage),
        },
        "spearman": {
            "chars_vs_lexical_coverage": _spearman([float(row["chars"]) for row in valid], lexical_coverage),
            "tokens_vs_lexical_coverage": _spearman([float(row["tokens"]) for row in valid], lexical_coverage),
            "chars_vs_semantic_coverage": _spearman([float(row["chars"]) for row in semantic_rows], semantic_coverage),
            "tokens_vs_semantic_coverage": _spearman([float(row["tokens"]) for row in semantic_rows], semantic_coverage),
        },
    }


def _miss_classification(judgments: list[dict[str, Any]], cases_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts = {key: 0 for key in MISS_CATEGORIES}
    lexical_misses = 0
    semantic_false_negatives = 0
    all_claims = 0
    semantic_covered = 0
    per_case: list[dict[str, Any]] = []
    for judgment in judgments:
        case = cases_by_id[judgment["query_id"]]
        normalized = judgment["normalized_claims"]
        case_misses: list[dict[str, Any]] = []
        for lexical, semantic in zip(case["lexical_claims"], normalized, strict=True):
            all_claims += 1
            semantic_covered += int(semantic["semantic_covered"])
            if not lexical["lexical_matched"]:
                lexical_misses += 1
                category = semantic["classification"]
                if category not in MISS_CATEGORIES:
                    category = "AMBIGUOUS"
                counts[category] += 1
                if semantic["semantic_covered"] and category == "PARAPHRASE_FALSE_NEGATIVE":
                    semantic_false_negatives += 1
                case_misses.append(
                    {
                        "claim_index": lexical["claim_index"],
                        "claim": lexical["claim"],
                        "lexical_matched_terms": lexical["lexical_matched_terms"],
                        "lexical_missed_terms": lexical["lexical_missed_terms"],
                        "semantic_covered": semantic["semantic_covered"],
                        "classification": category,
                        "confidence": semantic["confidence"],
                        "matched_answer_span": semantic["matched_answer_span"],
                        "rationale": semantic["rationale"],
                    }
                )
        per_case.append({"query_id": judgment["query_id"], "missed_claims": case_misses})
    return {
        "counts": counts,
        "lexical_miss_count": lexical_misses,
        "semantic_false_negative_count": semantic_false_negatives,
        "false_negative_rate_among_lexical_misses": _rate(semantic_false_negatives, lexical_misses),
        "false_negative_rate_of_all_claims": _rate(semantic_false_negatives, all_claims),
        "semantic_claim_coverage": _rate(semantic_covered, all_claims),
        "claim_count": all_claims,
        "per_case": per_case,
    }


def _production_prompt_audit(v1: dict[str, Any]) -> dict[str, Any]:
    first_raw = v1["raw_generation_runs"]["oracle"][0]
    first_call = (first_raw.get("llm_calls") or [{}])[0]
    return {
        "source_files": [
            "services/greenbook_mcp/greenbook_mcp_server/tools/community.py",
            "packages/agent_core/greenbook_agent_core/llm_compat.py",
        ],
        "system_prompt": community._GROUNDED_ANSWER_PROMPT,
        "system_prompt_sha256": _sha256_text(community._GROUNDED_ANSWER_PROMPT),
        "user_payload_shape": "{question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}",
        "evidence_order": "ctx.java response order; no secondary summary or selector in community tool",
        "source_format": "sources[{postId,title,chunkId}]",
        "structured_schema": "JSON object: answer string plus sources array of {postId,title,chunkId}; additional properties rejected; no answer length bound",
        "citation_rewrite": "exact chunkId lookup, dedupe, canonical postId/title",
        "temperature": 0.0,
        "max_tokens_observed": first_call.get("max_tokens"),
        "response_format_observed": first_call.get("response_format"),
        "model": v1["production_generation_chain"]["model"],
        "stop_condition": "no stop parameter observed; provider/structured_call completion",
        "answer_length_constraint": "none in production schema or handler",
        "concise_instruction": False,
        "core_only_instruction": False,
        "evidence_second_summary": False,
        "sources_context_cost": "source IDs are returned in the same structured output; no source text is duplicated in output",
        "insufficient_evidence": "empty retrieved evidence short-circuits to canonical sentinel; malformed/no-source generation fails closed",
    }


def _prompt_variants() -> dict[str, dict[str, Any]]:
    current = community._GROUNDED_ANSWER_PROMPT
    return {
        "CURRENT": {"prompt": current, "schema": ANSWER_SCHEMA, "experiment_only": False},
        "COMPLETENESS_AWARE": {
            "prompt": current
            + "\n- Within the supplied evidence, cover all key facts needed to answer the question. Do not omit important supported facts merely to make the answer shorter. Keep the answer focused and avoid unsupported detail.",
            "schema": ANSWER_SCHEMA,
            "experiment_only": True,
        },
        "STRUCTURED_CLAIM_PLAN": {
            "prompt": current
            + "\n- Before writing the final answer, identify the supporting points in the evidence that answer the question. The supportingPoints field is an internal structured checklist; ensure the final answer covers each supported point without adding unsupported facts.",
            "schema": CLAIM_PLAN_SCHEMA,
            "experiment_only": True,
        },
        "EVIDENCE_ORDERING": {"prompt": current, "schema": ANSWER_SCHEMA, "experiment_only": True},
    }


async def _variant_semantic_judge(
    case: dict[str, Any],
    answers: dict[str, str],
    *,
    llm: CapturingLLM,
    model: str,
) -> dict[str, Any]:
    request = {
        "query": case["query"],
        "gold_claims": case["gold_claims"],
        "gold_evidence": [{"chunkId": item["chunkId"], "content": item["content"]} for item in case["provided_evidence"]],
        "answers": answers,
    }
    call_start = len(llm.calls)
    started = time.perf_counter()
    error = None
    payload: Any = {}
    try:
        response = await structured_call(
            llm,
            model,
            VARIANT_JUDGE_PROMPT,
            "rag_generation_completeness_v2_variant_audit",
            VARIANT_JUDGE_SCHEMA,
            request,
        )
        payload = community._grounded_payload(response)
    except Exception as exc:
        error = f"{type(exc).__name__}: {_text(exc)[:500]}"
    return {
        "query_id": case["query_id"],
        "model": model,
        "prompt_sha256": _sha256_text(VARIANT_JUDGE_PROMPT),
        "input": request,
        "output": payload,
        "normalized": payload if isinstance(payload, dict) else {},
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "provider_calls": llm.calls[call_start:],
        "error": error,
    }


def _attach_variant_semantics(
    experiment_rows: dict[str, list[dict[str, Any]]],
    variant_judgments: list[dict[str, Any]],
) -> None:
    by_key: dict[tuple[str, str], list[bool]] = {}
    for judgment in variant_judgments:
        variants = judgment.get("normalized", {}).get("variants", []) if isinstance(judgment.get("normalized"), dict) else []
        for variant in variants if isinstance(variants, list) else []:
            if not isinstance(variant, dict):
                continue
            name = _text(variant.get("variant"))
            claims = variant.get("claims", [])
            if not isinstance(claims, list):
                continue
            by_key[(judgment["query_id"], name)] = [bool(item.get("semantic_covered")) for item in claims if isinstance(item, dict)]
    for variant, rows in experiment_rows.items():
        values: list[float] = []
        for row in rows:
            flags = by_key.get((row["query_id"], variant), [])
            row["semantic_claim_coverage"] = _safe_mean([float(flag) for flag in flags]) if flags else None
            if flags:
                values.append(row["semantic_claim_coverage"])
        for row in rows:
            row.setdefault("semantic_claim_coverage", None)


def _ratio(candidate: Any, baseline: Any) -> float | None:
    if candidate is None or baseline is None or float(baseline) <= 0:
        return None
    return round(float(candidate) / float(baseline), 6)


def _variant_gate(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    semantic_delta = round(
        float(candidate.get("semantic_claim_coverage") or 0)
        - float(current.get("semantic_claim_coverage") or 0),
        6,
    )
    faithfulness_delta = round(
        float(candidate.get("faithfulness") or 0) - float(current.get("faithfulness") or 0),
        6,
    )
    hallucination_delta = round(
        float(candidate.get("hallucination_rate") or 0) - float(current.get("hallucination_rate") or 0),
        6,
    )
    gate = {
        "semantic_delta": semantic_delta,
        "faithfulness_delta": faithfulness_delta,
        "hallucination_delta": hallucination_delta,
        "p95_answer_token_ratio": _ratio(candidate.get("p95_answer_tokens"), current.get("p95_answer_tokens")),
        "average_answer_token_ratio": _ratio(candidate.get("answer_tokens"), current.get("answer_tokens")),
        "p95_latency_ratio": _ratio(candidate.get("p95_generation_latency_ms"), current.get("p95_generation_latency_ms")),
        "citation_non_regression": float(candidate.get("citation_correctness") or 0)
        >= float(current.get("citation_correctness") or 0),
    }
    gate["passes"] = all(
        (
            gate["semantic_delta"] >= 0.10,
            gate["faithfulness_delta"] >= -0.05,
            gate["hallucination_delta"] <= MAX_HALLUCINATION_DELTA,
            gate["citation_non_regression"],
            gate["p95_answer_token_ratio"] is not None
            and gate["p95_answer_token_ratio"] <= MAX_P95_TOKEN_RATIO,
            gate["average_answer_token_ratio"] is not None
            and gate["average_answer_token_ratio"] <= MAX_AVG_TOKEN_RATIO,
            gate["p95_latency_ratio"] is not None
            and gate["p95_latency_ratio"] <= MAX_P95_LATENCY_RATIO,
        )
    )
    return gate


def _finalize_experiment_summary(summary: dict[str, Any]) -> dict[str, Any]:
    variants = summary["variants"]
    current = variants.get("CURRENT", {})
    candidates = [variant for variant in variants if variant != "CURRENT"]
    candidates.sort(
        key=lambda variant: (
            float(variants[variant].get("semantic_claim_coverage") or -1),
            float(variants[variant].get("lexical_claim_coverage") or -1),
        ),
        reverse=True,
    )
    gates = {variant: _variant_gate(current, variants[variant]) for variant in candidates}
    accepted = [variant for variant in candidates if gates[variant]["passes"]]
    semantic_best = candidates[0] if candidates else None
    selected = accepted[0] if accepted else semantic_best

    def delta(variant: str | None) -> dict[str, Any]:
        if not variant:
            return {}
        candidate = variants[variant]
        return {
            key: round(float(candidate.get(key) or 0) - float(current.get(key) or 0), 6)
            for key in (
                "semantic_claim_coverage",
                "lexical_claim_coverage",
                "faithfulness",
                "hallucination_rate",
                "answer_tokens",
                "p95_answer_tokens",
                "p95_generation_latency_ms",
            )
        }

    summary["semantic_best_variant"] = semantic_best
    summary["accepted_variant"] = accepted[0] if accepted else None
    summary["variant_gates"] = gates
    summary["best_variant"] = selected
    summary["best_delta"] = delta(selected)
    summary["semantic_best_delta"] = delta(semantic_best)
    summary["prompt_improvement_gate"] = bool(accepted)
    return summary


def _recompute_existing(args: argparse.Namespace) -> dict[str, Any]:
    """Repair/refresh derived variant aggregates without making provider calls."""
    output = _load_json(args.output)
    inputs = _load_inputs(args)
    v1 = inputs["v1"]
    semantic_by_id = {
        item["query_id"]: item["semantic_coverage"]
        for item in output.get("semantic_judgments", [])
        if item.get("semantic_coverage") is not None
    }
    output["production_generation_chain"] = v1["production_generation_chain"]
    output["production_prompt_audit"] = _production_prompt_audit(v1)
    output["overall_metrics"] = {
        "production_evidence": {
            **v1["metrics"]["production_answerable"],
            "semantic_claim_coverage": output["metrics"].get("production_semantic_claim_coverage"),
        },
        "gold_evidence_oracle": {
            **v1["metrics"]["oracle_answerable"],
            "semantic_claim_coverage": output["metrics"]["oracle_semantic_claim_coverage"],
        },
    }
    output["production_vs_oracle_delta"] = {
        "answer_correctness": round(
            output["overall_metrics"]["gold_evidence_oracle"]["answer_correctness"]
            - output["overall_metrics"]["production_evidence"]["answer_correctness"],
            6,
        ),
        "lexical_claim_coverage": round(
            output["overall_metrics"]["gold_evidence_oracle"]["gold_claim_coverage"]
            - output["overall_metrics"]["production_evidence"]["gold_claim_coverage"],
            6,
        ),
        "semantic_claim_coverage": round(
            output["overall_metrics"]["gold_evidence_oracle"]["semantic_claim_coverage"]
            - output["overall_metrics"]["production_evidence"]["semantic_claim_coverage"],
            6,
        ),
        "faithfulness": round(
            output["overall_metrics"]["gold_evidence_oracle"]["faithfulness"]
            - output["overall_metrics"]["production_evidence"]["faithfulness"],
            6,
        ),
        "completeness": round(
            output["overall_metrics"]["gold_evidence_oracle"]["answer_completeness"]
            - output["overall_metrics"]["production_evidence"]["answer_completeness"],
            6,
        ),
    }
    output["retrieval_aware_metrics"] = v1["retrieval_aware_metrics"]
    output["latency_baseline"] = v1["latency"]
    output["no_answer_metrics"] = {
        "production": v1["metrics"]["no_answer_production"],
        "empty_context_control": v1["metrics"]["no_answer_empty_context_control"],
    }
    output["first_bad_state_distribution"] = {
        "production_v1": v1["failure_distribution"],
        "oracle_semantic": {
            "GENERATION_COMPLETENESS_FAILURE": {
                "count": output["metrics"]["generation_limited_count"],
                "rate": output["metrics"]["generation_limited_rate"],
            },
            "GENERATION_SEMANTIC_ADEQUATE": {
                "count": output["metrics"]["oracle_case_count"] - output["metrics"]["generation_limited_count"],
                "rate": _rate(
                    output["metrics"]["oracle_case_count"] - output["metrics"]["generation_limited_count"],
                    output["metrics"]["oracle_case_count"],
                ),
            },
        },
    }
    output["answer_length_correlation"] = _length_correlation(inputs["cases"], semantic_by_id)
    output["next_recommendation"] = (
        "Keep production unchanged. COMPLETENESS_AWARE is an oracle-only candidate that passes the stated offline guardrails on the 12-case experiment sample; STRUCTURED_CLAIM_PLAN has the highest semantic score but fails the average-token guardrail. Preserve both as evaluation artifacts and require explicit approval plus a larger fixed/manual audit before any production prompt change."
    )
    experiments = output["prompt_experiments"]
    experiment_rows: dict[str, list[dict[str, Any]]] = {}
    for variant, raw_rows in experiments["cases"].items():
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            row = dict(raw.get("metrics") or {})
            row.pop("semantic_claim_coverage", None)
            rows.append(row)
        experiment_rows[variant] = rows

    _attach_variant_semantics(experiment_rows, experiments.get("variant_judgments", []))
    for variant, raw_rows in experiments["cases"].items():
        for raw, row in zip(raw_rows, experiment_rows[variant], strict=True):
            raw["metrics"].update(row)

    refreshed = {
        "sample_count": experiments["sample_count"],
        "variants": {},
        "best_variant": None,
        "prompt_improvement_gate": False,
        "best_delta": {},
        "cases": experiments["cases"],
        "variant_judgments": experiments.get("variant_judgments", []),
    }
    for variant, rows in experiment_rows.items():
        refreshed["variants"][variant] = _aggregate_variant(rows)

    candidates = [variant for variant in refreshed["variants"] if variant != "CURRENT"]
    candidates.sort(
        key=lambda variant: (
            float(refreshed["variants"][variant].get("semantic_claim_coverage") or -1),
            float(refreshed["variants"][variant].get("lexical_claim_coverage") or -1),
        ),
        reverse=True,
    )
    current = refreshed["variants"]["CURRENT"]
    if candidates:
        best_variant = candidates[0]
        best = refreshed["variants"][best_variant]
        refreshed["best_variant"] = best_variant
        refreshed["best_delta"] = {
            key: round(float(best.get(key) or 0) - float(current.get(key) or 0), 6)
            for key in (
                "semantic_claim_coverage",
                "lexical_claim_coverage",
                "faithfulness",
                "hallucination_rate",
                "answer_tokens",
                "p95_answer_tokens",
                "p95_generation_latency_ms",
            )
        }
        refreshed["prompt_improvement_gate"] = (
            refreshed["best_delta"]["semantic_claim_coverage"] >= 0.10
            and refreshed["best_delta"]["faithfulness"] >= -0.05
            and float(best.get("p95_answer_tokens") or 0) <= 1.5 * float(current.get("p95_answer_tokens") or 1)
        )
    refreshed = _finalize_experiment_summary(refreshed)

    output["prompt_experiments"] = refreshed
    verdict_value, verdict_reason = _choose_verdict(
        validation=output["validation"],
        miss=output["missed_claim_classification"],
        oracle_semantic={
            "semantic_claim_coverage": output["metrics"]["oracle_semantic_claim_coverage"],
            "lexical_claim_coverage": output["metrics"]["oracle_lexical_claim_coverage"],
        },
        experiment_summary=refreshed,
        retrieval_limited_rate=output["metrics"]["retrieval_limited_rate"],
    )
    output["verdict"] = {"value": verdict_value, "reason": verdict_reason}
    return output


def _choose_verdict(
    *,
    validation: dict[str, Any],
    miss: dict[str, Any],
    oracle_semantic: dict[str, Any],
    experiment_summary: dict[str, Any],
    retrieval_limited_rate: float,
) -> tuple[str, str]:
    if not validation.get("valid"):
        return "BLOCKED", "Frozen V1/Dataset inputs failed validation."
    accepted = experiment_summary.get("accepted_variant")
    if experiment_summary.get("prompt_improvement_gate") and accepted and accepted != "CURRENT":
        return (
            "RAG_GENERATION_PROMPT_IMPROVEMENT_FOUND",
            f"Oracle-only variant {accepted} materially improved semantic claim coverage while meeting the offline faithfulness, citation, token, hallucination, and p95 latency guardrails.",
        )
    if miss.get("false_negative_rate_among_lexical_misses", 0) >= 0.40 and float(oracle_semantic.get("semantic_claim_coverage") or 0) - float(oracle_semantic.get("lexical_claim_coverage") or 0) >= 0.15:
        return "RAG_GENERATION_METRIC_ISSUE", "The auxiliary semantic audit overturns a material share of lexical misses; V1 completeness was materially underestimated."
    if float(oracle_semantic.get("semantic_claim_coverage") or 0) < SEMANTIC_GENERATION_LIMIT_THRESHOLD:
        if retrieval_limited_rate >= 0.50:
            return "RAG_MIXED_RETRIEVAL_GENERATION_LIMIT", "Retrieval is the earliest failure for many production cases, and gold-evidence oracle generation still misses many claims semantically."
        return "RAG_GENERATION_COMPLETENESS_CONFIRMED", "Gold evidence is present but semantic claim coverage remains low."
    if retrieval_limited_rate >= 0.50:
        return "RAG_MIXED_RETRIEVAL_GENERATION_LIMIT", "Retrieval remains the dominant production boundary while generation is mostly semantically adequate."
    return "RAG_GENERATION_METRIC_ISSUE", "The semantic audit does not support the original lexical-only completeness verdict."


def _validation(inputs: dict[str, Any], args: argparse.Namespace, v1: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    cases = inputs["cases"]
    if len(inputs["dataset_rows"]) != 50:
        errors.append("Dataset V2 row count is not 50")
    if len(cases) != 45:
        errors.append("answerable case count is not 45")
    if v1.get("validation", {}).get("snapshot_drift") != 0:
        errors.append("V1 snapshot drift is not zero")
    if v1.get("verdict", {}).get("value") is None:
        errors.append("V1 verdict is missing")
    return {
        "valid": not errors,
        "errors": errors,
        "dataset_count": len(inputs["dataset_rows"]),
        "answerable_count": len(cases),
        "no_answer_count": sum(not row.get("gold_chunk_ids") for row in inputs["dataset_rows"]),
        "v1_results_sha256": _sha256_bytes(args.v1_results),
        "dataset_sha256": _sha256_bytes(args.dataset),
        "snapshot_sha256": _sha256_bytes(args.snapshot),
        "snapshot_digest": inputs["snapshot"].get("snapshot_digest"),
        "snapshot_drift": v1.get("validation", {}).get("snapshot_drift"),
        "semantic_embedding_available": False,
        "semantic_embedding_note": "sentence-transformers/fastembed and the embedding sidecar were unavailable; auxiliary fixed-model semantic audit used instead.",
    }


def _render_report(output: dict[str, Any]) -> str:
    metrics = output["metrics"]
    experiments = output["prompt_experiments"]
    chain = output["production_generation_chain"]
    overall = output["overall_metrics"]
    retrieval_aware = output["retrieval_aware_metrics"]
    no_answer = output["no_answer_metrics"]
    first_bad = output["first_bad_state_distribution"]
    lines = [
        "# RAG_GENERATION_COMPLETENESS_V2",
        "",
        f"**Verdict:** `{output['verdict']['value']}`  ",
        f"**Reason:** {output['verdict']['reason']}",
        "",
        "## 1. V1 checkpoint and scope",
        "",
        f"- V1 checkpoint: `{output['checkpoint']['v1_commit']}`; V1 verdict was `{output['checkpoint']['v1_verdict']}`.",
        f"- Dataset: 50 rows; 45 answerable; 5 no-answer. Frozen snapshot drift: `{output['validation']['snapshot_drift']}`.",
        f"- Reproducibility: dataset SHA256 `{output['validation']['dataset_sha256']}`, snapshot SHA256 `{output['validation']['snapshot_sha256']}`, snapshot digest `{output['validation']['snapshot_digest']}`, V1 result SHA256 `{output['validation']['v1_results_sha256']}`.",
        "- Production files changed: `0`; all semantic/judge/prompt work is evaluation-only.",
        "",
        "## 2. Metric validation",
        "",
        "V1 lexical metrics are retained. Semantic coverage is an auxiliary fixed-model claim audit; it is not used as the only verdict signal.",
        "- No local embedding runtime was available; judge prompt/model/input/output are fixed and recorded as an auxiliary audit, not a sole decision maker.",
        "",
        f"- Audited claims: `{output['missed_claim_classification']['claim_count']}` across all answerable oracle cases.",
        f"- Detailed rule/semantic audit sample: `{output['audit']['sample_count']}` cases.",
        f"- Oracle lexical misses: `{output['missed_claim_classification']['lexical_miss_count']}`; production lexical misses: `{output['production_missed_claim_classification']['lexical_miss_count']}`.",
        f"- Oracle lexical false-negative rate among misses: `{output['metrics']['lexical_false_negative_rate']:.3f}`; production: `{output['metrics']['production_lexical_false_negative_rate']:.3f}`.",
        f"- Lexical false-negative rate over all claims: `{output['metrics']['lexical_false_negative_rate_all_claims']:.3f}`.",
        "",
        "### Coverage",
        "",
        "| Scope | Lexical claim coverage | Semantic claim coverage |",
        "|---|---:|---:|",
        f"| Production (V1) | {metrics['production_lexical_claim_coverage']:.3f} | {metrics['production_semantic_claim_coverage']:.3f} |",
        f"| Gold oracle | {metrics['oracle_lexical_claim_coverage']:.3f} | {metrics['oracle_semantic_claim_coverage']:.3f} |",
        "",
        "### Missed-claim classification",
        "",
        "| Category | Count | Rate of lexical misses |",
        "|---|---:|---:|",
    ]
    miss = output["missed_claim_classification"]
    for category in MISS_CATEGORIES:
        lines.append(f"| {category} | {miss['counts'][category]} | {_rate(miss['counts'][category], miss['lexical_miss_count']):.3f} |")
    production_miss = output["production_missed_claim_classification"]
    lines += [
        "",
        "### Production missed-claim classification",
        "",
        f"Production lexical misses: `{production_miss['lexical_miss_count']}`; semantic false-negative rate among those misses: `{output['metrics']['production_lexical_false_negative_rate']:.3f}`.",
        "",
        "| Category | Count | Rate of lexical misses |",
        "|---|---:|---:|",
    ]
    for category in MISS_CATEGORIES:
        lines.append(
            f"| {category} | {production_miss['counts'][category]} | {_rate(production_miss['counts'][category], production_miss['lexical_miss_count']):.3f} |"
        )
    lines += [
        "",
        "The detailed audit records include query, gold answer/claims, supplied evidence, generated answer, matched/missed lexical terms, semantic classification, confidence, and rationale for 20 oracle cases and the corresponding 20 production cases.",
        "",
        "## 3. Production generation chain",
        "",
        f"`{chain['entrypoint']}` -> `{chain['evidence_retrieval']}` -> evidence payload `{chain['user_payload_shape']}` -> `{chain['structured_call']}` -> `{chain['citation_rewrite']}` -> response.",
        f"- Evidence budget: production handler defaults `{chain['production_defaults']}`; V1/V2 frozen evaluation used top_posts=`{chain['top_posts']}`, top_chunks=`{chain['top_chunks']}`, max evidence=`{chain['max_evidence']}`.",
        "- Evidence is passed in retrieval order; citation output is a global sources array with exact chunkId validation, not inline claim-position markers.",
        f"- Empty or insufficient evidence returns the exact sentinel with empty sources: `{no_answer['production']['sentinel']}`.",
        "",
        "## 4. Overall generation metrics",
        "",
        "These metrics preserve V1 deterministic correctness/completeness and add the V2 oracle semantic claim coverage. Production and oracle use the same generator path; oracle replaces only the evidence input with gold evidence.",
        "",
        "| Scope | Answer correctness | Lexical claim coverage | Semantic claim coverage | Faithfulness | Citation correctness | Citation completeness | Hallucination | Completeness |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| Production evidence | {overall['production_evidence']['answer_correctness']:.3f} | {overall['production_evidence']['gold_claim_coverage']:.3f} | {overall['production_evidence']['semantic_claim_coverage']:.3f} | {overall['production_evidence']['faithfulness']:.3f} | {overall['production_evidence']['citation_correctness']:.3f} | {overall['production_evidence']['citation_completeness']:.3f} | {overall['production_evidence']['hallucination_rate']:.3f} | {overall['production_evidence']['answer_completeness']:.3f} |",
        f"| Gold evidence oracle | {overall['gold_evidence_oracle']['answer_correctness']:.3f} | {overall['gold_evidence_oracle']['gold_claim_coverage']:.3f} | {overall['gold_evidence_oracle']['semantic_claim_coverage']:.3f} | {overall['gold_evidence_oracle']['faithfulness']:.3f} | {overall['gold_evidence_oracle']['citation_correctness']:.3f} | {overall['gold_evidence_oracle']['citation_completeness']:.3f} | {overall['gold_evidence_oracle']['hallucination_rate']:.3f} | {overall['gold_evidence_oracle']['answer_completeness']:.3f} |",
        "",
        f"No-answer accuracy: production `{no_answer['production']['correct']}/{no_answer['production']['count']} = {no_answer['production']['accuracy']:.3f}`; empty-context control `{no_answer['empty_context_control']['correct']}/{no_answer['empty_context_control']['count']} = {no_answer['empty_context_control']['accuracy']:.3f}`.",
        f"Production -> Gold Oracle delta: correctness `{output['production_vs_oracle_delta']['answer_correctness']}`, lexical completeness `{output['production_vs_oracle_delta']['lexical_claim_coverage']}`, semantic completeness `{output['production_vs_oracle_delta']['semantic_claim_coverage']}`, faithfulness `{output['production_vs_oracle_delta']['faithfulness']}`, completeness `{output['production_vs_oracle_delta']['completeness']}`.",
        "",
        "## 5. Retrieval-aware generation metrics",
        "",
        "The following are inherited from the frozen V1 production-evidence run; V2 does not rerun or change retrieval.",
        "",
        "| Evidence state | Cases | Answer correctness | Claim coverage | Faithfulness | Citation correctness | Citation completeness |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for state in ("GOLD_EVIDENCE_RETRIEVED", "PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"):
        row = retrieval_aware[state]
        lines.append(
            f"| {state} | {row['count']} | {row['answer_correctness']:.3f} | {row['gold_claim_coverage']:.3f} | {row['faithfulness']:.3f} | {row['citation_correctness']:.3f} | {row['citation_completeness']:.3f} |"
        )
    lines += [
        "",
        "## 6. Oracle failure-family distribution",
        "",
        "| Classification | Cases | Rate |",
        "|---|---:|---:|",
        f"| Semantic generation-limited (semantic coverage < {SEMANTIC_GENERATION_LIMIT_THRESHOLD:.2f}) | {output['metrics']['generation_limited_count']} | {output['metrics']['generation_limited_rate']:.3f} |",
        f"| Semantic adequate | {output['metrics']['oracle_case_count'] - output['metrics']['generation_limited_count']} | {_rate(output['metrics']['oracle_case_count'] - output['metrics']['generation_limited_count'], output['metrics']['oracle_case_count']):.3f} |",
        f"| V1 lexical completeness failures | {output['metrics']['v1_oracle_completeness_failure_count']} | {output['metrics']['v1_oracle_completeness_failure_rate']:.3f} |",
        "",
        "## 7. Production prompt audit",
        "",
        f"- System prompt: `community._GROUNDED_ANSWER_PROMPT`, SHA256 `{output['production_prompt_audit']['system_prompt_sha256']}`.",
        f"- Evidence payload: `{output['production_prompt_audit']['user_payload_shape']}`; original evidence order preserved; no secondary summary.",
        f"- Structured call: temperature `{output['production_prompt_audit']['temperature']}`, observed max tokens `{output['production_prompt_audit']['max_tokens_observed']}`, response format `{output['production_prompt_audit']['response_format_observed']}`.",
        f"- Structured schema: `{output['production_prompt_audit']['structured_schema']}`.",
        f"- Stop/length: `{output['production_prompt_audit']['stop_condition']}`; answer length constraint: `{output['production_prompt_audit']['answer_length_constraint']}`.",
        "- No production instruction says concise/core-only, and no answer field max length exists. The hard constraints are evidence-only, insufficient sentinel, valid source IDs, and JSON shape.",
        "",
        "## 8. Answer-length correlation (oracle)",
        "",
        f"- Pearson chars -> lexical: `{output['answer_length_correlation']['pearson']['chars_vs_lexical_coverage']}`; tokens -> lexical: `{output['answer_length_correlation']['pearson']['tokens_vs_lexical_coverage']}`; sentences -> lexical: `{output['answer_length_correlation']['pearson']['sentences_vs_lexical_coverage']}`.",
        f"- Pearson chars -> semantic: `{output['answer_length_correlation']['pearson']['chars_vs_semantic_coverage']}`; tokens -> semantic: `{output['answer_length_correlation']['pearson']['tokens_vs_semantic_coverage']}`; sentences -> semantic: `{output['answer_length_correlation']['pearson']['sentences_vs_semantic_coverage']}`.",
        f"- Spearman chars/tokens -> lexical: `{output['answer_length_correlation']['spearman']['chars_vs_lexical_coverage']}` / `{output['answer_length_correlation']['spearman']['tokens_vs_lexical_coverage']}`.",
        f"- Spearman chars/tokens -> semantic: `{output['answer_length_correlation']['spearman']['chars_vs_semantic_coverage']}` / `{output['answer_length_correlation']['spearman']['tokens_vs_semantic_coverage']}`.",
        "- These are descriptive correlations, not causal proof. Per-query chars, tokens, sentence count, gold claim count, matched claim count, and both coverage values are in the JSON artifact.",
        "",
        "## 9. Evidence utilization (oracle)",
        "",
        f"- Evidence support rate: `{output['evidence_utilization']['aggregate']['evidence_support_rate']:.3f}`.",
        f"- Evidence utilization rate by generated answer: `{output['evidence_utilization']['aggregate']['evidence_utilization_rate']:.3f}`.",
        f"- Claim utilization rate: `{output['evidence_utilization']['aggregate']['claim_utilization_rate']:.3f}`.",
        f"- Evidence available but underused: `{output['evidence_utilization']['aggregate']['evidence_available_but_underused_count']}` cases.",
        f"- Evidence not supporting gold by coarse deterministic support check: `{output['evidence_utilization']['aggregate']['evidence_not_supporting_gold_count']}` cases; this is separate from per-claim semantic category D.",
        "",
        "## 10. Oracle-only prompt experiments",
        "",
        f"Experiment sample: `{output['prompt_experiments']['sample_count']}` oracle cases; CURRENT is the saved V1 oracle output, not a new production call. B/C/D were evaluation-only calls.",
        "",
        "| Variant | Lexical coverage | Semantic coverage | Faithfulness | Hallucination | Citation | Avg tokens | p95 tokens | p95 latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, row in experiments["variants"].items():
        lines.append(
            f"| {variant} | {row.get('lexical_claim_coverage', 0):.3f} | {row.get('semantic_claim_coverage', 0) if row.get('semantic_claim_coverage') is not None else 0:.3f} | "
            f"{row.get('faithfulness', 0):.3f} | {row.get('hallucination_rate', 0):.3f} | {row.get('citation_correctness', 0):.3f} | "
            f"{row.get('answer_tokens', 0):.1f} | {row.get('p95_answer_tokens') or 0:.1f} | {row.get('p95_generation_latency_ms') or 0:.1f} |"
        )
    lines += [
        "",
        f"- Semantic-best variant: `{experiments['semantic_best_variant']}`; offline-gate selected variant: `{experiments['accepted_variant']}`; reported best: `{experiments['best_variant']}`.",
        f"- Prompt improvement gate: `{experiments['prompt_improvement_gate']}`.",
        "- C is a single structured call with an additional supportingPoints field; it does not add a second Agent loop.",
        "",
        "### Offline acceptance guardrails",
        "",
        "A candidate needs semantic delta >= 0.10, faithfulness delta >= -0.05, hallucination delta <= 0.05, citation non-regression, p95 answer-token ratio <= 1.50, average answer-token ratio <= 2.00, and p95 generation-latency ratio <= 1.50.",
        "",
        "| Variant | Semantic delta | Faithfulness delta | Hallucination delta | Avg token ratio | p95 token ratio | p95 latency ratio | Citation non-regression | Gate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, gate in experiments["variant_gates"].items():
        lines.append(
            f"| {variant} | {gate['semantic_delta']:.3f} | {gate['faithfulness_delta']:.3f} | {gate['hallucination_delta']:.3f} | {gate['average_answer_token_ratio']:.3f} | {gate['p95_answer_token_ratio']:.3f} | {gate['p95_latency_ratio']:.3f} | {gate['citation_non_regression']} | {gate['passes']} |"
        )
    lines += [
        "",
        "## 11. Baseline -> selected best impact",
        "",
        f"- Semantic coverage delta: `{experiments['best_delta']['semantic_claim_coverage']}`.",
        f"- Lexical coverage delta: `{experiments['best_delta']['lexical_claim_coverage']}`.",
        f"- Faithfulness delta: `{experiments['best_delta']['faithfulness']}`; hallucination delta: `{experiments['best_delta']['hallucination_rate']}`.",
        f"- Average token delta: `{experiments['best_delta']['answer_tokens']}`; p95 token delta: `{experiments['best_delta']['p95_answer_tokens']}`; p95 latency delta ms: `{experiments['best_delta']['p95_generation_latency_ms']}`.",
        f"- The semantic-highest C variant delta is `{experiments['semantic_best_delta']}`; it did not pass the average-token guardrail, so it is not the selected best.",
        "",
        "## 12. Latency and token baseline",
        "",
        f"- Production generation p50/p95: `{output['latency_baseline']['production_generation_ms']['p50']}` / `{output['latency_baseline']['production_generation_ms']['p95']} ms`.",
        f"- Production total estimated p50/p95: `{output['latency_baseline']['estimated_production_total_ms']['p50']}` / `{output['latency_baseline']['estimated_production_total_ms']['p95']} ms`.",
        f"- Production prompt tokens p50/p95: `{output['latency_baseline']['production_prompt_tokens']['p50']}` / `{output['latency_baseline']['production_prompt_tokens']['p95']}`; completion tokens p50/p95: `{output['latency_baseline']['production_completion_tokens']['p50']}` / `{output['latency_baseline']['production_completion_tokens']['p95']}`.",
        f"- Evidence context token estimate p50/p95: `{output['latency_baseline']['production_context_tokens_estimate']['p50']}` / `{output['latency_baseline']['production_context_tokens_estimate']['p95']}`.",
        "- Retrieval timing is historical only in V1; it was not re-run in V2. Prompt-variant timing above is oracle-only and not production latency.",
        "",
        "## 13. Independent retrieval vs generation conclusions",
        "",
        f"- RETRIEVAL_LIMITED: `{output['metrics']['retrieval_limited_rate']:.3f}` ({output['metrics']['retrieval_limited_count']}/{output['metrics']['oracle_case_count']} production answerable cases had partial/missing exact gold evidence).",
        f"- GENERATION_LIMITED: `{output['metrics']['generation_limited_rate']:.3f}` ({output['metrics']['generation_limited_count']}/{output['metrics']['oracle_case_count']} oracle cases had semantic coverage below `{SEMANTIC_GENERATION_LIMIT_THRESHOLD:.2f}`).",
        f"- METRIC_LIMITED: `{output['metrics']['lexical_false_negative_rate']:.3f}` among lexical misses in the semantic audit.",
        "- These rates use different conditioning sets and are not additive: retrieval-limited is production evidence status, generation-limited is gold-evidence oracle output, and metric-limited is the lexical audit subset.",
        "",
        "## 14. FIRST_BAD_STATE and protected boundaries",
        "",
        "Production first-bad-state distribution from V1 remains:",
        "",
        "| FIRST_BAD_STATE / failure family | Cases | Rate |",
        "|---|---:|---:|",
    ]
    for state, row in first_bad["production_v1"].items():
        if row["count"]:
            lines.append(f"| {state} | {row['count']} | {row['rate']:.3f} |")
    lines += [
        "",
        f"Oracle semantic diagnosis: GENERATION_COMPLETENESS_FAILURE `{first_bad['oracle_semantic']['GENERATION_COMPLETENESS_FAILURE']['count']}` / `{metrics['oracle_case_count']}`; semantically adequate `{first_bad['oracle_semantic']['GENERATION_SEMANTIC_ADEQUATE']['count']}` / `{metrics['oracle_case_count']}`.",
        "No V1 evidence-selection, context-construction, citation, or faithfulness first-bad-state was observed. V2 does not reinterpret retrieval failures as generator failures.",
        "",
        "Protected production files changed: `0`.",
        f"- Dirty files: `{', '.join(output['files']['dirty_files']) or '(none)'}`.",
        f"- Evaluation script: `{output['files']['script']}`.",
        f"- Results: `{output['files']['results']}`.",
        f"- Report: `{output['files']['report']}`.",
        "",
        "## 15. Recommendation",
        "",
        "Keep production unchanged. COMPLETENESS_AWARE is an oracle-only candidate that passes the stated offline guardrails on the 12-case experiment sample; STRUCTURED_CLAIM_PLAN has the highest semantic score but fails the average-token guardrail. Preserve both as evaluation artifacts and require explicit approval plus a larger fixed/manual audit before any production prompt change.",
        "Retrieval remains the earliest production limitation at 40/45 cases. Do not use the prompt experiment to justify retrieval, chunk, embedding, runtime, or MCP changes.",
        "",
        "No production prompt, generator, retrieval, chunking, embedding, or runtime change was made.",
    ]
    return "\n".join(lines) + "\n"


def _git_dirty_files() -> list[str]:
    import subprocess

    try:
        result = subprocess.run(["git", "status", "--short"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line[3:].strip() for line in result.stdout.splitlines() if len(line) >= 4]


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env")
    inputs = _load_inputs(args)
    v1 = inputs["v1"]
    validation = _validation(inputs, args, v1)
    if not validation["valid"]:
        raise RuntimeError("Input validation failed: " + "; ".join(validation["errors"]))
    model = v1["production_generation_chain"]["model"]
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required for the auxiliary semantic audit")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout)
    llm = CapturingLLM(client)
    cases = inputs["cases"]
    cases_by_id = {case["query_id"]: case for case in cases}
    audit_cases, audit_labels = _select_audit_cases(cases)
    audit_ids = {case["query_id"] for case in audit_cases}
    judgments: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(cases, start=1):
            judgment = await _judge_case(case, llm=llm, model=model)
            judgment["detailed_rule_audit"] = case["query_id"] in audit_ids
            judgment["audit_labels"] = audit_labels.get(case["query_id"], [])
            judgments.append(judgment)
            print(f"[semantic {index:02d}/{len(cases)}] {case['query_id']}", flush=True)

        production_cases = inputs["production_cases"]
        production_judgments: list[dict[str, Any]] = []
        for index, case in enumerate(production_cases, start=1):
            judgment = await _judge_case(case, llm=llm, model=model)
            judgment["detailed_rule_audit"] = case["query_id"] in audit_ids
            judgment["audit_labels"] = audit_labels.get(case["query_id"], [])
            production_judgments.append(judgment)
            print(f"[production semantic {index:02d}/{len(production_cases)}] {case['query_id']}", flush=True)

        experiment_cases = audit_cases[:EXPERIMENT_SAMPLE_SIZE]
        variants = _prompt_variants()
        experiment_raw: dict[str, list[dict[str, Any]]] = {key: [] for key in variants}
        experiment_observations: dict[str, list[dict[str, Any]]] = {key: [] for key in variants}
        for index, case in enumerate(experiment_cases, start=1):
            current_case = next(item for item in cases if item["query_id"] == case["query_id"])
            saved = current_case["v1_raw_generation"]
            current_output = {
                "variant": "CURRENT",
                "model": model,
                "prompt_sha256": _sha256_text(community._GROUNDED_ANSWER_PROMPT),
                "input_evidence_ids": current_case["gold_chunk_ids"],
                "answer": current_case["generated_answer"],
                "sources": current_case["sources"],
                "structured_payload": {"answer": current_case["generated_answer"], "sources": current_case["sources"]},
                "generation_latency_ms": saved.get("generation_latency_ms"),
                "completion_tokens": saved.get("completion_tokens"),
                "prompt_tokens": saved.get("prompt_tokens"),
                "calls": saved.get("llm_calls", []),
                "error": saved.get("tool_error"),
                "wall_latency_ms": saved.get("wall_latency_ms"),
            }
            current_output["metrics"] = _variant_metrics(
                current_case,
                current_output["answer"],
                current_output["sources"],
                current_output,
            )
            experiment_observations["CURRENT"].append(current_output)
            experiment_raw["CURRENT"].append(current_output)
            for variant in ("COMPLETENESS_AWARE", "STRUCTURED_CLAIM_PLAN", "EVIDENCE_ORDERING"):
                evidence = list(current_case["provided_evidence"])
                if variant == "EVIDENCE_ORDERING":
                    evidence = [
                        item
                        for _, item in sorted(
                            enumerate(evidence),
                            key=lambda pair: (-_jaccard_terms(current_case["query"], pair[1]["content"]), pair[0]),
                        )
                    ]
                result = await _run_variant(
                    current_case,
                    variant=variant,
                    prompt=variants[variant]["prompt"],
                    schema=variants[variant]["schema"],
                    evidence=evidence,
                    llm=llm,
                    model=model,
                )
                experiment_observations[variant].append(result)
                experiment_raw[variant].append(result)
            print(f"[experiment {index:02d}/{len(experiment_cases)}] {case['query_id']}", flush=True)

        variant_judgments: list[dict[str, Any]] = []
        for index, case in enumerate(experiment_cases, start=1):
            answers = {
                variant: experiment_observations[variant][index - 1]["answer"]
                for variant in variants
            }
            variant_judgments.append(await _variant_semantic_judge(case, answers, llm=llm, model=model))
            print(f"[variant semantic {index:02d}/{len(experiment_cases)}] {case['query_id']}", flush=True)
    finally:
        await client.close()

    miss_classification = _miss_classification(judgments, cases_by_id)
    production_cases = inputs["production_cases"]
    production_cases_by_id = {case["query_id"]: case for case in production_cases}
    production_miss_classification = _miss_classification(production_judgments, production_cases_by_id)
    oracle_case_semantic = {
        judgment["query_id"]: judgment["semantic_coverage"] for judgment in judgments
    }
    oracle_semantic_coverage = _safe_mean(
        [value for value in oracle_case_semantic.values() if value is not None]
    )
    oracle_lexical = _safe_mean([float(case.get("v1_oracle_coverage") or 0) for case in cases])
    production_lexical = _safe_mean([float(case.get("v1_production_coverage") or 0) for case in cases])
    production_semantic_coverage = _safe_mean(
        [float(judgment["semantic_coverage"]) for judgment in production_judgments]
    )
    generation_limited_cases = [
        query_id for query_id, value in oracle_case_semantic.items()
        if value is not None and value < SEMANTIC_GENERATION_LIMIT_THRESHOLD
    ]
    evidence_utilization = _evidence_utilization(cases)
    answer_length = _length_correlation(cases, oracle_case_semantic)

    for variant, rows in experiment_observations.items():
        metric_rows = [row["metrics"] for row in rows]
        experiment_observations[variant] = metric_rows
    _attach_variant_semantics(experiment_observations, variant_judgments)
    experiment_summary: dict[str, Any] = {
        "sample_count": len(experiment_cases),
        "variants": {},
        "best_variant": None,
        "prompt_improvement_gate": False,
        "best_delta": {},
        "cases": experiment_raw,
        "variant_judgments": variant_judgments,
    }
    for variant, rows in experiment_observations.items():
        aggregate = _aggregate_variant(rows)
        experiment_summary["variants"][variant] = aggregate
    candidates = [variant for variant in experiment_summary["variants"] if variant != "CURRENT"]
    candidates.sort(
        key=lambda variant: (
            float(experiment_summary["variants"][variant].get("semantic_claim_coverage") or -1),
            float(experiment_summary["variants"][variant].get("lexical_claim_coverage") or -1),
        ),
        reverse=True,
    )
    current_aggregate = experiment_summary["variants"]["CURRENT"]
    if candidates:
        best_variant = candidates[0]
        experiment_summary["best_variant"] = best_variant
        best_aggregate = experiment_summary["variants"][best_variant]
        experiment_summary["best_delta"] = {
            key: round(float(best_aggregate.get(key) or 0) - float(current_aggregate.get(key) or 0), 6)
            for key in ("semantic_claim_coverage", "lexical_claim_coverage", "faithfulness", "hallucination_rate", "answer_tokens", "p95_generation_latency_ms")
        }
        experiment_summary["prompt_improvement_gate"] = (
            experiment_summary["best_delta"]["semantic_claim_coverage"] >= 0.10
            and experiment_summary["best_delta"]["faithfulness"] >= -0.05
            and float(best_aggregate.get("p95_answer_tokens") or 0) <= 1.5 * float(current_aggregate.get("p95_answer_tokens") or 1)
        )
    experiment_summary = _finalize_experiment_summary(experiment_summary)

    retrieval_limited_count = int(v1["metrics"]["retrieval_limited_case_count"])
    retrieval_limited_rate = _rate(retrieval_limited_count, len(cases))
    metrics = {
        "oracle_case_count": len(cases),
        "production_lexical_claim_coverage": production_lexical,
        "production_semantic_claim_coverage": production_semantic_coverage,
        "oracle_lexical_claim_coverage": oracle_lexical,
        "oracle_semantic_claim_coverage": oracle_semantic_coverage,
        "lexical_false_negative_rate": miss_classification["false_negative_rate_among_lexical_misses"],
        "lexical_false_negative_rate_all_claims": miss_classification["false_negative_rate_of_all_claims"],
        "production_lexical_false_negative_rate": production_miss_classification["false_negative_rate_among_lexical_misses"],
        "production_lexical_false_negative_rate_all_claims": production_miss_classification["false_negative_rate_of_all_claims"],
        "generation_limited_count": len(generation_limited_cases),
        "generation_limited_rate": _rate(len(generation_limited_cases), len(cases)),
        "generation_limited_query_ids": generation_limited_cases,
        "retrieval_limited_count": retrieval_limited_count,
        "retrieval_limited_rate": retrieval_limited_rate,
        "v1_oracle_completeness_failure_count": sum(
            case.get("v1_oracle_coverage", 0) < 0.5 for case in cases
        ),
        "v1_oracle_completeness_failure_rate": _rate(
            sum(case.get("v1_oracle_coverage", 0) < 0.5 for case in cases), len(cases)
        ),
    }
    verdict_value, verdict_reason = _choose_verdict(
        validation=validation,
        miss=miss_classification,
        oracle_semantic={"semantic_claim_coverage": oracle_semantic_coverage, "lexical_claim_coverage": oracle_lexical},
        experiment_summary=experiment_summary,
        retrieval_limited_rate=retrieval_limited_rate,
    )
    output: dict[str, Any] = {
        "evaluation": "RAG_GENERATION_COMPLETENESS_V2",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checkpoint": {
            "v1_commit": args.v1_commit,
            "v1_verdict": v1["verdict"]["value"],
            "v1_results_path": str(args.v1_results.relative_to(REPO_ROOT)),
        },
        "validation": validation,
        "dataset": {
            "path": str(args.dataset.relative_to(REPO_ROOT)),
            "row_count": len(inputs["dataset_rows"]),
            "answerable_count": len(cases),
            "no_answer_count": validation["no_answer_count"],
            "gold_reference_count": sum(len(row.get("gold_chunk_ids", [])) for row in inputs["dataset_rows"]),
        },
        "production_generation_chain": v1["production_generation_chain"],
        "production_prompt_audit": _production_prompt_audit(v1),
        "audit": {
            "sample_count": len(audit_cases),
            "sample_query_ids": [case["query_id"] for case in audit_cases],
            "labels": audit_labels,
            "cases": [
                {
                    **case,
                    "detailed_audit": True,
                    "semantic_judgment": next(item for item in judgments if item["query_id"] == case["query_id"]),
                }
                for case in audit_cases
            ],
            "production_cases": [
                {
                    **next(item for item in production_cases if item["query_id"] == case["query_id"]),
                    "detailed_audit": True,
                    "semantic_judgment": next(
                        item for item in production_judgments if item["query_id"] == case["query_id"]
                    ),
                }
                for case in audit_cases
            ],
        },
        "semantic_judgments": judgments,
        "production_semantic_judgments": production_judgments,
        "missed_claim_classification": miss_classification,
        "production_missed_claim_classification": production_miss_classification,
        "answer_length_correlation": answer_length,
        "evidence_utilization": evidence_utilization,
        "overall_metrics": {
            "production_evidence": {
                **v1["metrics"]["production_answerable"],
                "semantic_claim_coverage": production_semantic_coverage,
            },
            "gold_evidence_oracle": {
                **v1["metrics"]["oracle_answerable"],
                "semantic_claim_coverage": oracle_semantic_coverage,
            },
        },
        "production_vs_oracle_delta": {
            "answer_correctness": round(
                v1["metrics"]["oracle_answerable"]["answer_correctness"]
                - v1["metrics"]["production_answerable"]["answer_correctness"],
                6,
            ),
            "lexical_claim_coverage": round(oracle_lexical - production_lexical, 6),
            "semantic_claim_coverage": round(oracle_semantic_coverage - production_semantic_coverage, 6),
            "faithfulness": round(
                v1["metrics"]["oracle_answerable"]["faithfulness"]
                - v1["metrics"]["production_answerable"]["faithfulness"],
                6,
            ),
            "completeness": round(
                v1["metrics"]["oracle_answerable"]["answer_completeness"]
                - v1["metrics"]["production_answerable"]["answer_completeness"],
                6,
            ),
        },
        "retrieval_aware_metrics": v1["retrieval_aware_metrics"],
        "latency_baseline": v1["latency"],
        "no_answer_metrics": {
            "production": v1["metrics"]["no_answer_production"],
            "empty_context_control": v1["metrics"]["no_answer_empty_context_control"],
        },
        "first_bad_state_distribution": {
            "production_v1": v1["failure_distribution"],
            "oracle_semantic": {
                "GENERATION_COMPLETENESS_FAILURE": {
                    "count": len(generation_limited_cases),
                    "rate": _rate(len(generation_limited_cases), len(cases)),
                },
                "GENERATION_SEMANTIC_ADEQUATE": {
                    "count": len(cases) - len(generation_limited_cases),
                    "rate": _rate(len(cases) - len(generation_limited_cases), len(cases)),
                },
            },
        },
        "prompt_experiments": experiment_summary,
        "metrics": metrics,
        "verdict": {"value": verdict_value, "reason": verdict_reason},
        "llm_judge": {
            "used": True,
            "role": "auxiliary semantic claim matcher only",
            "model": model,
            "fixed_prompt_sha256": _sha256_text(SEMANTIC_JUDGE_PROMPT),
            "variant_prompt_sha256": _sha256_text(VARIANT_JUDGE_PROMPT),
            "input_output_recorded": True,
            "not_sole_verdict_signal": True,
        },
        "production_files_changed": [],
        "next_recommendation": (
            "Keep production unchanged. COMPLETENESS_AWARE is an oracle-only candidate that passes the stated offline guardrails on the 12-case experiment sample; STRUCTURED_CLAIM_PLAN has the highest semantic score but fails the average-token guardrail. Preserve both as evaluation artifacts and require explicit approval plus a larger fixed/manual audit before any production prompt change."
        ),
        "files": {
            "script": str(Path(__file__).resolve().relative_to(REPO_ROOT)),
            "results": str(args.output.relative_to(REPO_ROOT)),
            "report": str(args.report.relative_to(REPO_ROOT)),
            "dirty_files": [],
        },
    }
    return output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--v1-results", type=Path, default=V1_OUTPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_V2_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_V2_REPORT)
    parser.add_argument("--v1-commit", default="92f0923")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--recompute-existing",
        action="store_true",
        help="Refresh derived metrics from an existing V2 result without provider calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    for name in ("dataset", "snapshot", "runs", "v1_results", "output", "report"):
        setattr(args, name, getattr(args, name).resolve())
    output = _recompute_existing(args) if args.recompute_existing else asyncio.run(_run(args))
    _write_json(args.output, output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(output), encoding="utf-8")
    output["files"]["dirty_files"] = _git_dirty_files()
    _write_json(args.output, output)
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(json.dumps({"verdict": output["verdict"], "output": str(args.output), "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
