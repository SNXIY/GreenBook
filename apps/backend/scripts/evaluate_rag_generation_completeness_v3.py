"""Evaluation-only production-evidence A/B for RAG answer completeness.

The harness enters the existing ``community.answer_from_knowledge`` boundary
with the already frozen retrieval inputs.  It runs the current prompt and one
minimal completeness-aware prompt against exactly the same evidence, model,
schema, and generation configuration.  The Java boundary is a local frozen
double, so this script never calls the live retriever, Java, Qdrant, MySQL, or
the production retrieval stack.

The semantic claim comparison is an auxiliary fixed-model audit.  Deterministic
evidence, citation, refusal, and unsupported-claim checks remain independent
signals and are included in the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from evaluate_rag_generation_v1 import (
    CANONICAL_TOP_CHUNKS,
    CANONICAL_TOP_POSTS,
    DEFAULT_DATASET,
    DEFAULT_RUNS,
    DEFAULT_SNAPSHOT,
    DEFAULT_V7_RESULTS,
    CapturingLLM,
    _aggregate,
    _catalog_evidence,
    _check_context_integrity,
    _failure_distribution,
    _invoke_production_generation,
    _load_json,
    _load_jsonl,
    _mean,
    _percentile,
    _rate,
    _response_metrics,
    _sha256_bytes,
    _sha256_text,
    _terms,
    _text,
    _write_json,
)
from evaluate_rag_generation_v1 import (
    DEFAULT_OUTPUT as V1_OUTPUT,
    REPO_ROOT,
)
from evaluate_rag_generation_completeness_v2 import (
    _claim_diagnostics,
    _safe_mean,
)
from greenbook_agent_core.llm_compat import structured_call
from greenbook_mcp_server.tools import community
from openai import AsyncOpenAI

DEFAULT_V2_OUTPUT = REPO_ROOT / "docs/evaluation/rag_generation_completeness_v2_results.json"
DEFAULT_OUTPUT = REPO_ROOT / "docs/evaluation/rag_generation_completeness_v3_results.json"
DEFAULT_REPORT = REPO_ROOT / "docs/reports/RAG_GENERATION_COMPLETENESS_V3_REPORT.md"

V2_CHECKPOINT_COMMIT = "c8a5726"
AUDIT_SAMPLE_SIZE = 20
MAX_ACCEPTABLE_P95_LATENCY_DELTA_MS = 500.0
MAX_ACCEPTABLE_P95_LATENCY_RATIO = 1.25
MAX_ACCEPTABLE_AVERAGE_TOKEN_RATIO = 1.75
MAX_ACCEPTABLE_P95_TOKEN_RATIO = 1.25
MAX_FAITHFULNESS_DELTA = -0.02
MAX_HALLUCINATION_DELTA = 0.01
SEMANTIC_GAIN_THRESHOLD = 0.10
STRATUM_GAIN_THRESHOLD = 0.05
AUDIT_OVER_VERBOSE_TOKEN_RATIO = 1.75

COMPLETENESS_AWARE_SUFFIX = (
    "\n- Within the supplied evidence, cover all key facts needed to answer the current question. "
    "Do not omit important supported facts merely to be concise. Do not add or guess facts unsupported by the evidence."
)

VARIANT_JUDGE_PROMPT = """You are an auxiliary semantic comparison for grounded answer completeness.
You are not the answer generator and must not use outside knowledge.

For every supplied gold claim, evaluate each named answer independently.
Count a natural-language paraphrase as covered, but do not reward vague topic
overlap. Use only the supplied gold evidence to decide whether the claim is
supportable. A claim is covered only if that answer actually states the fact.
Return JSON only with one result per named variant and claim.
"""

VARIANT_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "variant": {"type": "string", "enum": ["CURRENT", "COMPLETENESS_AWARE"]},
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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sentences(value: Any) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?；;\n]+", _text(value)) if item.strip()]


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _git_dirty_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line[3:].strip() for line in result.stdout.splitlines() if len(line) >= 4]


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ratio(candidate: Any, baseline: Any) -> float | None:
    left = _safe_float(candidate)
    right = _safe_float(baseline)
    if left is None or right is None or right <= 0:
        return None
    return round(left / right, 6)


def _latencies_from_run(run: dict[str, Any]) -> dict[str, Any]:
    values = run.get("latencies_ms") or {}
    result: dict[str, Any] = {}
    for key in ("embedding", "chunk_retrieval", "total"):
        raw = values.get(key)
        if isinstance(raw, list):
            result[key] = raw[0] if raw else None
        else:
            result[key] = raw
    return result


def _candidate_post_ids(raw_posts: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(raw_posts, list):
        return result
    for item in raw_posts:
        if isinstance(item, dict):
            post_id = _text(item.get("post_id") or item.get("postId"))
            if post_id and post_id not in result:
                result.append(post_id)
    return result


def _retrieval_stratum(dataset_row: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    gold_ids = {_text(value) for value in dataset_row.get("gold_chunk_ids", [])}
    evidence_ids = {_text(item.get("chunkId")) for item in evidence}
    if not gold_ids:
        return "NO_REQUIRED_EVIDENCE"
    exact = gold_ids & evidence_ids
    if exact == gold_ids:
        return "GOLD_EVIDENCE_RETRIEVED"
    if exact:
        return "PARTIAL_EVIDENCE_RETRIEVED"
    return "REQUIRED_EVIDENCE_MISSING"


def _is_evidence_qualification_or_heading(claim: Any) -> bool:
    """Exclude non-factual discourse fragments from unsupported-claim safety.

    The shared V1 sentence splitter intentionally remains unchanged.  For the
    V3 safety gate, a list heading such as ``具体实践包括：`` and a scoped
    limitation such as ``资料中未直接提及`` are not model-knowledge facts.
    They remain visible in ``claim_support`` as proxy diagnostics.
    """
    value = _text(claim).strip()
    if not value:
        return True
    if not _terms(value):
        return True
    if value.rstrip("`*_ ").endswith((":", "：")):
        return True
    patterns = (
        r"资料中(?:未|没有|并未|未曾)(?:直接)?(?:提及|说明|给出|覆盖)",
        r"证据中(?:未|没有|并未)(?:直接)?(?:提及|说明|给出|覆盖)",
        r"(?:无法|不能)(?:从资料|从证据)(?:确认|确定)",
        r"(?:not|does not|doesn't) (?:explicitly )?(?:mention|state|cover)",
        r"(?:no|without) (?:direct )?(?:mention|evidence)",
    )
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def _evidence_fingerprint(evidence: list[dict[str, Any]]) -> str:
    return _json_hash(
        [
            {
                "chunkId": _text(item.get("chunkId")),
                "postId": _text(item.get("postId")),
                "title": _text(item.get("title")),
                "content": _text(item.get("content")),
                "startOffset": item.get("startOffset"),
                "endOffset": item.get("endOffset"),
            }
            for item in evidence
        ]
    )


def _gold_claims(row: dict[str, Any]) -> list[str]:
    claims = [
        _text(item.get("claim")) if isinstance(item, dict) else _text(item)
        for item in row.get("evidence_claims", [])
    ]
    return claims or [_text(row.get("gold_answer"))]


def _load_inputs(args: argparse.Namespace) -> dict[str, Any]:
    dataset_rows = _load_jsonl(args.dataset)
    snapshot = _load_json(args.snapshot)
    retrieval_runs = _load_jsonl(args.runs)
    v1 = _load_json(args.v1_results)
    v2 = _load_json(args.v2_results)
    catalog = {
        _text(item.get("chunk_id")): item
        for item in snapshot.get("chunk_catalog", [])
        if item.get("chunk_id")
    }
    dataset_by_id = {_text(row.get("query_id")): row for row in dataset_rows}
    snapshot_by_id = {_text(row.get("query_id")): row for row in snapshot.get("queries", [])}
    runs_by_id = {_text(row.get("query_id")): row for row in retrieval_runs}
    v1_production = {
        _text(item.get("query_id")): item
        for item in v1.get("raw_generation_runs", {}).get("production", [])
    }
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in dataset_rows:
        query_id = _text(row.get("query_id"))
        if query_id not in v1_production:
            errors.append(f"V1 production run missing: {query_id}")
            continue
        anchor = v1_production[query_id]
        if row.get("gold_chunk_ids"):
            snapshot_row = snapshot_by_id.get(query_id)
            if snapshot_row is None:
                errors.append(f"frozen snapshot row missing: {query_id}")
                continue
            expected_ids = [
                _text(item.get("chunk_id"))
                for item in snapshot_row.get("candidate_chunks", [])[:CANONICAL_TOP_CHUNKS]
            ]
            input_ids = [_text(value) for value in anchor.get("input_evidence_ids", [])]
            if expected_ids != input_ids:
                errors.append(f"V1 production evidence differs from frozen Top10: {query_id}")
            evidence = [_catalog_evidence({"chunk_id": value}, catalog) for value in input_ids]
            candidate_post_ids = _candidate_post_ids(snapshot_row.get("candidate_posts", []))
            candidate_post_count = len(candidate_post_ids)
            retrieval_run = runs_by_id.get(query_id, {})
            retrieval_latencies = _latencies_from_run(retrieval_run)
        else:
            input_ids = [_text(value) for value in anchor.get("input_evidence_ids", [])]
            evidence = [_catalog_evidence({"chunk_id": value}, catalog) for value in input_ids]
            candidate_post_ids = []
            for item in evidence:
                post_id = _text(item.get("postId"))
                if post_id and post_id not in candidate_post_ids:
                    candidate_post_ids.append(post_id)
            candidate_post_count = int(anchor.get("candidate_post_count") or 0)
            retrieval_run = runs_by_id.get(query_id, {})
            retrieval_latencies = anchor.get("retrieval_latencies_ms") or _latencies_from_run(retrieval_run)
        for item in evidence:
            if not item.get("chunkId") or item.get("chunkId") not in catalog:
                errors.append(f"evidence chunk missing from frozen catalog: {query_id}/{item.get('chunkId')}")
        claims = _gold_claims(row) if row.get("gold_chunk_ids") else []
        evidence_text = "\n".join(_text(item.get("content")) for item in evidence)
        cases.append(
            {
                "query_id": query_id,
                "question": _text(row.get("question") or row.get("query")),
                "category": row.get("category"),
                "answerable": bool(row.get("gold_chunk_ids")),
                "gold_answer": _text(row.get("gold_answer")),
                "gold_claims": claims,
                "gold_post_ids": [_text(value) for value in row.get("gold_post_ids", [])],
                "gold_chunk_ids": [_text(value) for value in row.get("gold_chunk_ids", [])],
                "evidence": evidence,
                "evidence_ids": input_ids,
                "evidence_fingerprint": _evidence_fingerprint(evidence),
                "candidate_post_ids": candidate_post_ids,
                "candidate_post_count": candidate_post_count,
                "retrieval_latencies_ms": retrieval_latencies,
                "retrieval_run_id": query_id if retrieval_run else None,
                "retrieval_stratum": _retrieval_stratum(row, evidence),
                "evidence_text": evidence_text,
                "lexical_gold_claims": _claim_diagnostics(
                    claims,
                    _text(anchor.get("answer")),
                    evidence_text,
                )
                if claims
                else [],
                "dataset_row": row,
                "v1_anchor": anchor,
            }
        )
    return {
        "dataset_rows": dataset_rows,
        "dataset_by_id": dataset_by_id,
        "snapshot": snapshot,
        "snapshot_by_id": snapshot_by_id,
        "retrieval_runs": retrieval_runs,
        "runs_by_id": runs_by_id,
        "catalog": catalog,
        "v1": v1,
        "v2": v2,
        "cases": cases,
        "errors": errors,
    }


def _validate_inputs(inputs: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    dataset_rows = inputs["dataset_rows"]
    cases = inputs["cases"]
    snapshot = inputs["snapshot"]
    v1 = inputs["v1"]
    v2 = inputs["v2"]
    errors = list(inputs["errors"])
    answerable_count = sum(bool(row.get("gold_chunk_ids")) for row in dataset_rows)
    no_answer_count = len(dataset_rows) - answerable_count
    if len(dataset_rows) != 50:
        errors.append(f"dataset count={len(dataset_rows)}, expected 50")
    if answerable_count != 45 or no_answer_count != 5:
        errors.append(f"answerable/no-answer={answerable_count}/{no_answer_count}, expected 45/5")
    if len(cases) != len(dataset_rows):
        errors.append(f"frozen generation cases={len(cases)}, expected {len(dataset_rows)}")
    if len(snapshot.get("queries", [])) != 45:
        errors.append(f"snapshot query count={len(snapshot.get('queries', []))}, expected 45")
    v2_validation = v2.get("validation", {})
    current_dataset_sha = _sha256_bytes(args.dataset)
    current_snapshot_sha = _sha256_bytes(args.snapshot)
    current_v1_sha = _sha256_bytes(args.v1_results)
    current_prompt_sha = _sha256_text(community._GROUNDED_ANSWER_PROMPT)
    expected_dataset_sha = _text(v2_validation.get("dataset_sha256"))
    expected_snapshot_sha = _text(v2_validation.get("snapshot_sha256"))
    expected_v1_sha = _text(v2_validation.get("v1_results_sha256"))
    if expected_dataset_sha and current_dataset_sha != expected_dataset_sha:
        errors.append("dataset SHA differs from V2 checkpoint")
    if expected_snapshot_sha and current_snapshot_sha != expected_snapshot_sha:
        errors.append("snapshot SHA differs from V2 checkpoint")
    if expected_v1_sha and current_v1_sha != expected_v1_sha:
        errors.append("V1 results SHA differs from V2 checkpoint")
    if v2_validation.get("snapshot_drift") != 0:
        errors.append("V2 checkpoint snapshot drift is not zero")
    if v2.get("verdict", {}).get("value") != "RAG_GENERATION_PROMPT_IMPROVEMENT_FOUND":
        errors.append(f"unexpected V2 verdict={v2.get('verdict', {}).get('value')!r}")
    expected_prompt_sha = _text(v2.get("production_prompt_audit", {}).get("system_prompt_sha256"))
    if expected_prompt_sha and current_prompt_sha != expected_prompt_sha:
        errors.append("live community prompt is not the saved V2 CURRENT prompt")
    for case in cases:
        if len(case["evidence_ids"]) > 10:
            errors.append(f"evidence exceeds production max 10: {case['query_id']}")
        if case["answerable"] and len(case["evidence_ids"]) != len(case["v1_anchor"].get("input_evidence_ids", [])):
            errors.append(f"V1 evidence contract mismatch: {case['query_id']}")
    return {
        "valid": not errors,
        "errors": errors,
        "dataset_count": len(dataset_rows),
        "answerable_count": answerable_count,
        "no_answer_count": no_answer_count,
        "dataset_sha256": current_dataset_sha,
        "snapshot_sha256": current_snapshot_sha,
        "snapshot_digest": snapshot.get("snapshot_digest"),
        "snapshot_drift": 0 if not errors else None,
        "v1_results_sha256": current_v1_sha,
        "v2_results_sha256": _sha256_bytes(args.v2_results),
        "v2_checkpoint_commit": args.v2_commit,
        "v2_validation_snapshot_sha256": expected_snapshot_sha,
        "v2_validation_dataset_sha256": expected_dataset_sha,
        "v2_validation_v1_results_sha256": expected_v1_sha,
        "current_prompt_sha256": current_prompt_sha,
        "v2_current_prompt_sha256": expected_prompt_sha,
        "frozen_java_double": True,
        "live_retrieval_calls": 0,
        "retrieval_snapshot_source": str(args.snapshot.relative_to(REPO_ROOT)),
    }


def _make_case_metric(
    case: dict[str, Any],
    raw: dict[str, Any],
    variant: str,
) -> dict[str, Any]:
    metric = _response_metrics(
        dataset_row=case["dataset_row"],
        run={"query_id": case["query_id"]},
        result=raw,
        evidence=case["evidence"],
        candidate_posts=set(case["candidate_post_ids"]),
        mode="production_evidence" if case["answerable"] else "no_answer_production_evidence",
    )
    answer = _text(raw.get("answer"))
    claim_support = metric.get("claim_support", [])
    non_factual_claims = [
        item
        for item in claim_support
        if _is_evidence_qualification_or_heading(item.get("claim"))
    ]
    unsupported_factual_claims = [
        item
        for item in claim_support
        if not item.get("supported") and not _is_evidence_qualification_or_heading(item.get("claim"))
    ]
    for item in claim_support:
        item["evidence_qualification_or_heading"] = _is_evidence_qualification_or_heading(item.get("claim"))
    metric.update(
        {
            "variant": variant,
            "answer_chars": len(answer),
            "sentence_count": len(_sentences(answer)),
            "unsupported_claim_count": sum(
                not bool(item.get("supported")) for item in claim_support
            ),
            "unsupported_claims": [
                item.get("claim")
                for item in claim_support
                if not item.get("supported")
            ],
            "unsupported_factual_claim_count": len(unsupported_factual_claims),
            "unsupported_factual_claims": [item.get("claim") for item in unsupported_factual_claims],
            "evidence_qualification_or_heading_count": len(non_factual_claims),
            "evidence_qualification_or_headings": [item.get("claim") for item in non_factual_claims],
            "input_evidence_fingerprint": case["evidence_fingerprint"],
            "input_evidence_ids": case["evidence_ids"],
            "candidate_post_ids": case["candidate_post_ids"],
            "question_sha256": _sha256_text(case["question"]),
            "retrieval_total_latency_reference_ms": case["retrieval_latencies_ms"].get("total"),
        }
    )
    retrieval_total = _safe_float(case["retrieval_latencies_ms"].get("total"))
    generation_latency = _safe_float(metric.get("generation_latency_ms"))
    metric["total_rag_latency_ms"] = (
        round(retrieval_total + generation_latency, 3)
        if retrieval_total is not None and generation_latency is not None
        else None
    )
    return metric


async def _run_one_variant(
    case: dict[str, Any],
    *,
    variant: str,
    prompt: str,
    llm: CapturingLLM,
    model: str,
) -> dict[str, Any]:
    saved_prompt = community._GROUNDED_ANSWER_PROMPT
    community._GROUNDED_ANSWER_PROMPT = prompt
    try:
        raw = await _invoke_production_generation(
            query_id=case["query_id"],
            question=case["question"],
            evidence=case["evidence"],
            candidate_post_count=case["candidate_post_count"],
            retrieval_latencies=case["retrieval_latencies_ms"],
            llm=llm,
            model=model,
            mode="production_evidence" if case["answerable"] else "no_answer_production_evidence",
        )
    finally:
        community._GROUNDED_ANSWER_PROMPT = saved_prompt
    raw["variant"] = variant
    raw["context_integrity_failure"] = _check_context_integrity(raw, case["evidence"])
    return raw


def _normalize_variant_claims(payload: Any, claims: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    raw_variants = payload.get("variants", []) if isinstance(payload, dict) else []
    if not isinstance(raw_variants, list):
        return result
    for raw_variant in raw_variants:
        if not isinstance(raw_variant, dict):
            continue
        name = _text(raw_variant.get("variant"))
        if name not in {"CURRENT", "COMPLETENESS_AWARE"}:
            continue
        by_index: dict[int, dict[str, Any]] = {}
        raw_claims = raw_variant.get("claims", [])
        if isinstance(raw_claims, list):
            for raw_claim in raw_claims:
                if not isinstance(raw_claim, dict):
                    continue
                try:
                    index = int(raw_claim.get("claim_index"))
                except (TypeError, ValueError):
                    continue
                if index not in range(len(claims)) and index - 1 in range(len(claims)):
                    index -= 1
                if index not in range(len(claims)):
                    continue
                try:
                    confidence = float(raw_claim.get("confidence", 0) or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                by_index[index] = {
                    "claim_index": index,
                    "claim": claims[index],
                    "semantic_covered": bool(raw_claim.get("semantic_covered")),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "rationale": _text(raw_claim.get("rationale"))[:500],
                }
        if len(by_index) == len(claims):
            result[name] = [by_index[index] for index in range(len(claims))]
    return result


async def _semantic_variant_judge(
    case: dict[str, Any],
    answers: dict[str, str],
    *,
    llm: CapturingLLM,
    model: str,
) -> dict[str, Any]:
    request = {
        "query": case["question"],
        "gold_claims": case["gold_claims"],
        "provided_evidence": [
            {"chunkId": item["chunkId"], "postId": item["postId"], "content": item["content"]}
            for item in case["evidence"]
        ],
        "answers": answers,
    }
    call_start = len(llm.calls)
    started = time.perf_counter()
    error: str | None = None
    payload: Any = {}
    raw_content = ""
    try:
        response = await structured_call(
            llm,
            model,
            VARIANT_JUDGE_PROMPT,
            "rag_generation_completeness_v3_variant_audit",
            VARIANT_JUDGE_SCHEMA,
            request,
        )
        payload = community._grounded_payload(response)
        raw_content = _text(getattr(response.choices[0].message, "content", ""))
    except Exception as exc:  # retain an audit gap instead of inventing a score
        error = f"{type(exc).__name__}: {_text(exc)[:500]}"
    normalized = _normalize_variant_claims(payload, case["gold_claims"])
    return {
        "query_id": case["query_id"],
        "model": model,
        "prompt_sha256": _sha256_text(VARIANT_JUDGE_PROMPT),
        "input": request,
        "output": payload,
        "raw_output": raw_content[:5000],
        "normalized": normalized,
        "semantic_coverage": {
            variant: _safe_mean([float(item["semantic_covered"]) for item in claims])
            for variant, claims in normalized.items()
        },
        "provider_calls": llm.calls[call_start:],
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "error": error,
    }


def _add_semantic_metrics(
    metrics_by_variant: dict[str, dict[str, dict[str, Any]]],
    judgments: list[dict[str, Any]],
) -> None:
    for judgment in judgments:
        query_id = judgment["query_id"]
        normalized = judgment.get("normalized", {})
        for variant in ("CURRENT", "COMPLETENESS_AWARE"):
            metric = metrics_by_variant[variant].get(query_id)
            claims = normalized.get(variant) if isinstance(normalized, dict) else None
            if metric is None:
                continue
            if isinstance(claims, list) and len(claims) == len(metric.get("evidence_claims", [])):
                metric["semantic_claims"] = claims
                metric["semantic_claim_coverage"] = _safe_mean(
                    [float(item.get("semantic_covered")) for item in claims]
                )
            else:
                metric["semantic_claims"] = []
                metric["semantic_claim_coverage"] = None


def _aggregate_v3(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row.get("answerable")]
    aggregate = _aggregate(answerable)

    def values(key: str) -> list[float]:
        return [float(row[key]) for row in answerable if row.get(key) is not None]

    aggregate.update(
        {
            "semantic_claim_coverage": _mean(values("semantic_claim_coverage")),
            "lexical_claim_coverage": aggregate.get("gold_claim_coverage"),
            "answer_chars": _mean(values("answer_chars")),
            "answer_tokens": _mean(values("completion_tokens")),
            "prompt_tokens": _mean(values("prompt_tokens")),
            "sentence_count": _mean(values("sentence_count")),
            "generation_latency_ms": _mean(values("generation_latency_ms")),
            "p50_generation_latency_ms": _percentile(values("generation_latency_ms"), 0.50),
            "p95_generation_latency_ms": _percentile(values("generation_latency_ms"), 0.95),
            "p50_total_rag_latency_ms": _percentile(values("total_rag_latency_ms"), 0.50),
            "p95_total_rag_latency_ms": _percentile(values("total_rag_latency_ms"), 0.95),
            "p50_prompt_tokens": _percentile(values("prompt_tokens"), 0.50),
            "p95_prompt_tokens": _percentile(values("prompt_tokens"), 0.95),
            "p50_completion_tokens": _percentile(values("completion_tokens"), 0.50),
            "p95_completion_tokens": _percentile(values("completion_tokens"), 0.95),
            "unsupported_expansion_count": sum(
                int(row.get("unsupported_factual_claim_count") or 0) > 0 for row in answerable
            ),
            "citation_failure_count": sum(
                float(row.get("citation_correctness") or 0) < 1.0
                or float(row.get("citation_completeness") or 0) < 0.75
                for row in answerable
            ),
            "semantic_judge_missing_count": sum(
                row.get("semantic_claim_coverage") is None for row in answerable
            ),
        }
    )
    return aggregate


def _aggregate_by_stratum(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        stratum: _aggregate_v3(
            [row for row in rows if row.get("retrieval_stratum") == stratum]
        )
        for stratum in (
            "GOLD_EVIDENCE_RETRIEVED",
            "PARTIAL_EVIDENCE_RETRIEVED",
            "REQUIRED_EVIDENCE_MISSING",
        )
    }


def _evidence_utilization(
    cases: list[dict[str, Any]],
    metrics_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        if not case["answerable"]:
            continue
        metric = metrics_by_id[case["query_id"]]
        claim_terms: set[str] = set()
        for claim in case["gold_claims"]:
            claim_terms |= _terms(claim)
        answer_terms = _terms(metric.get("answer"))
        support_ids: list[str] = []
        used_ids: list[str] = []
        for item in case["evidence"]:
            terms = _terms(item.get("content"))
            support_overlap = terms & claim_terms
            used_overlap = terms & answer_terms
            if support_overlap and (len(support_overlap) >= 2 or _rate(len(support_overlap), len(claim_terms)) >= 0.05):
                support_ids.append(item["chunkId"])
            if used_overlap and (len(used_overlap) >= 2 or _rate(len(used_overlap), len(terms)) >= 0.05):
                used_ids.append(item["chunkId"])
        evidence_count = len(case["evidence"])
        rows.append(
            {
                "query_id": case["query_id"],
                "retrieval_stratum": case["retrieval_stratum"],
                "evidence_count": evidence_count,
                "gold_supporting_evidence_ids": support_ids,
                "answer_used_evidence_ids": used_ids,
                "evidence_support_rate": _rate(len(support_ids), evidence_count),
                "evidence_utilization_rate": _rate(len(used_ids), evidence_count),
                "claim_utilization_rate_lexical": metric.get("gold_claim_coverage"),
                "claim_utilization_rate_semantic": metric.get("semantic_claim_coverage"),
                "evidence_available_but_underused": bool(support_ids) and _rate(len(used_ids), evidence_count) < 0.5,
                "evidence_not_supporting_gold": _rate(len(support_ids), evidence_count) < 0.5,
            }
        )
    return {
        "cases": rows,
        "aggregate": {
            "evidence_support_rate": _mean([float(row["evidence_support_rate"]) for row in rows]),
            "evidence_utilization_rate": _mean([float(row["evidence_utilization_rate"]) for row in rows]),
            "claim_utilization_rate_lexical": _mean(
                [float(row["claim_utilization_rate_lexical"]) for row in rows if row.get("claim_utilization_rate_lexical") is not None]
            ),
            "claim_utilization_rate_semantic": _mean(
                [float(row["claim_utilization_rate_semantic"]) for row in rows if row.get("claim_utilization_rate_semantic") is not None]
            ),
            "evidence_available_but_underused_count": sum(row["evidence_available_but_underused"] for row in rows),
            "evidence_not_supporting_gold_count": sum(row["evidence_not_supporting_gold"] for row in rows),
        },
    }


def _missing_evidence_safety(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing = [row for row in rows if row.get("retrieval_stratum") == "REQUIRED_EVIDENCE_MISSING"]
    unsupported = [row for row in missing if int(row.get("unsupported_factual_claim_count") or 0) > 0]
    faithfulness_failures = [
        row for row in missing if int(row.get("unsupported_factual_claim_count") or 0) > 0
    ]
    citation_failures = [
        row
        for row in missing
        if float(row.get("citation_correctness") or 0) < 1.0
    ]
    answerable_refusals = [row for row in missing if row.get("answer_is_insufficient")]
    return {
        "count": len(missing),
        "unsupported_expansion_count": len(unsupported),
        "unsupported_claim_count": sum(int(row.get("unsupported_factual_claim_count") or 0) for row in missing),
        "evidence_qualification_or_heading_count": sum(
            int(row.get("evidence_qualification_or_heading_count") or 0) for row in missing
        ),
        "faithfulness_failure_count": len(faithfulness_failures),
        "fake_or_invalid_citation_count": len(citation_failures),
        "safe_refusal_count": len(answerable_refusals),
        "case_ids": [row.get("query_id") for row in missing],
        "unsupported_case_ids": [row.get("query_id") for row in unsupported],
        "faithfulness_failure_case_ids": [row.get("query_id") for row in faithfulness_failures],
    }


def _select_audit_cases(cases: list[dict[str, Any]], metrics_by_variant: dict[str, dict[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    answerable = [case for case in cases if case["answerable"]]
    selected: list[dict[str, Any]] = []
    labels: dict[str, list[str]] = {}
    for stratum in (
        "GOLD_EVIDENCE_RETRIEVED",
        "PARTIAL_EVIDENCE_RETRIEVED",
        "REQUIRED_EVIDENCE_MISSING",
    ):
        group = [case for case in answerable if case["retrieval_stratum"] == stratum]
        group.sort(
            key=lambda case: (
                float(metrics_by_variant["CURRENT"][case["query_id"]].get("completion_tokens") or 0),
                case["query_id"],
            )
        )
        target = min(5, len(group))
        if target:
            positions = sorted({round(index * (len(group) - 1) / max(target - 1, 1)) for index in range(target)})
            for position in positions:
                case = group[int(position)]
                if case not in selected:
                    selected.append(case)
                    labels.setdefault(case["query_id"], []).append(stratum)
    ordered = sorted(
        answerable,
        key=lambda case: float(metrics_by_variant["CURRENT"][case["query_id"]].get("completion_tokens") or 0),
    )
    for case, label in ((ordered[0], "SHORT_OUTPUT"), (ordered[-1], "LONG_OUTPUT")) if ordered else ():
        if case not in selected:
            selected.append(case)
        labels.setdefault(case["query_id"], []).append(label)
    for case in answerable:
        if len(selected) >= AUDIT_SAMPLE_SIZE:
            break
        if case not in selected:
            selected.append(case)
            labels.setdefault(case["query_id"], []).append("FILL")
    return selected[:AUDIT_SAMPLE_SIZE], labels


def _audit_label(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> str:
    if int(candidate.get("unsupported_factual_claim_count") or 0) > int(current.get("unsupported_factual_claim_count") or 0):
        return "UNSUPPORTED_EXPANSION"
    if float(candidate.get("citation_correctness") or 0) < float(current.get("citation_correctness") or 0):
        return "REGRESSION"
    current_semantic = _safe_float(current.get("semantic_claim_coverage"))
    candidate_semantic = _safe_float(candidate.get("semantic_claim_coverage"))
    if current_semantic is not None and candidate_semantic is not None:
        if candidate_semantic > current_semantic:
            return "CLEAR_IMPROVEMENT"
        if candidate_semantic < current_semantic:
            return "REGRESSION"
    current_tokens = _safe_float(current.get("completion_tokens"))
    candidate_tokens = _safe_float(candidate.get("completion_tokens"))
    if (
        current_tokens is not None
        and current_tokens > 0
        and candidate_tokens is not None
        and candidate_tokens / current_tokens >= AUDIT_OVER_VERBOSE_TOKEN_RATIO
    ):
        return "OVER_VERBOSE"
    return "NO_MEANINGFUL_CHANGE"


def _build_audit(
    audit_cases: list[dict[str, Any]],
    audit_labels: dict[str, list[str]],
    metrics_by_variant: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    counts = {key: 0 for key in ("CLEAR_IMPROVEMENT", "NO_MEANINGFUL_CHANGE", "OVER_VERBOSE", "UNSUPPORTED_EXPANSION", "REGRESSION")}
    output_cases: list[dict[str, Any]] = []
    for case in audit_cases:
        query_id = case["query_id"]
        current = metrics_by_variant["CURRENT"][query_id]
        candidate = metrics_by_variant["COMPLETENESS_AWARE"][query_id]
        label = _audit_label(current, candidate)
        counts[label] += 1
        output_cases.append(
            {
                "query_id": query_id,
                "audit_labels": audit_labels.get(query_id, []),
                "retrieval_stratum": case["retrieval_stratum"],
                "query": case["question"],
                "gold_answer": case["gold_answer"],
                "gold_claims": case["gold_claims"],
                "provided_evidence": case["evidence"],
                "input_evidence_ids": case["evidence_ids"],
                "input_evidence_fingerprint": case["evidence_fingerprint"],
                "current": {
                    "answer": current.get("answer"),
                    "sources": current.get("source_ids"),
                    "lexical_claims": current.get("claim_support"),
                    "semantic_claims": current.get("semantic_claims"),
                    "semantic_coverage": current.get("semantic_claim_coverage"),
                    "faithfulness": current.get("faithfulness"),
                    "hallucination_rate": current.get("hallucination_rate"),
                    "completion_tokens": current.get("completion_tokens"),
                },
                "completeness_aware": {
                    "answer": candidate.get("answer"),
                    "sources": candidate.get("source_ids"),
                    "lexical_claims": candidate.get("claim_support"),
                    "semantic_claims": candidate.get("semantic_claims"),
                    "semantic_coverage": candidate.get("semantic_claim_coverage"),
                    "faithfulness": candidate.get("faithfulness"),
                    "hallucination_rate": candidate.get("hallucination_rate"),
                    "completion_tokens": candidate.get("completion_tokens"),
                },
                "classification": label,
            }
        )
    return {
        "sample_count": len(output_cases),
        "selection_labels": audit_labels,
        "classification_counts": counts,
        "classification_rates": {key: _rate(value, len(output_cases)) for key, value in counts.items()},
        "method": "stratified semantic/rule audit; case artifacts are retained for manual review",
        "cases": output_cases,
    }


def _variant_safety(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing = _missing_evidence_safety(rows)
    return {
        "missing_evidence": missing,
        "all_answerable_unsupported_claim_cases": sum(
            int(row.get("unsupported_factual_claim_count") or 0) > 0
            for row in rows
            if row.get("answerable")
        ),
        "all_answerable_hallucination_cases": sum(
            int(row.get("unsupported_factual_claim_count") or 0) > 0
            for row in rows
            if row.get("answerable")
        ),
        "citation_correctness_failures": sum(
            float(row.get("citation_correctness") or 0) < 1.0
            for row in rows
            if row.get("answerable")
        ),
    }


def _stable_stratum_gate(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_semantic = _safe_float(current.get("semantic_claim_coverage"))
    candidate_semantic = _safe_float(candidate.get("semantic_claim_coverage"))
    current_rows = int(current.get("count") or 0)
    delta = (
        round(candidate_semantic - current_semantic, 6)
        if current_semantic is not None and candidate_semantic is not None
        else None
    )
    return {
        "count": current_rows,
        "semantic_delta": delta,
        "has_cases": current_rows > 0,
        "stable_improvement": bool(
            current_rows > 0 and delta is not None and delta >= STRATUM_GAIN_THRESHOLD
        ),
    }


def _acceptance_gate(
    current: dict[str, Any],
    candidate: dict[str, Any],
    current_strata: dict[str, dict[str, Any]],
    candidate_strata: dict[str, dict[str, Any]],
    current_no_answer: dict[str, Any],
    candidate_no_answer: dict[str, Any],
    current_safety: dict[str, Any],
    candidate_safety: dict[str, Any],
    audit: dict[str, Any],
    v2_reference_semantic: float | None,
) -> dict[str, Any]:
    semantic_delta = round(
        float(candidate.get("semantic_claim_coverage") or 0)
        - float(current.get("semantic_claim_coverage") or 0),
        6,
    )
    p95_latency_delta = (
        round(float(candidate.get("p95_generation_latency_ms")) - float(current.get("p95_generation_latency_ms")), 3)
        if current.get("p95_generation_latency_ms") is not None and candidate.get("p95_generation_latency_ms") is not None
        else None
    )
    p95_total_delta = (
        round(float(candidate.get("p95_total_rag_latency_ms")) - float(current.get("p95_total_rag_latency_ms")), 3)
        if current.get("p95_total_rag_latency_ms") is not None and candidate.get("p95_total_rag_latency_ms") is not None
        else None
    )
    current_faithfulness = float(current.get("faithfulness") or 0)
    candidate_faithfulness = float(candidate.get("faithfulness") or 0)
    current_hallucination = float(current.get("hallucination_rate") or 0)
    candidate_hallucination = float(candidate.get("hallucination_rate") or 0)
    current_citation = float(current.get("citation_correctness") or 0)
    candidate_citation = float(candidate.get("citation_correctness") or 0)
    current_citation_complete = float(current.get("citation_completeness") or 0)
    candidate_citation_complete = float(candidate.get("citation_completeness") or 0)
    audit_counts = audit.get("classification_counts", {})
    strata = {
        stratum: _stable_stratum_gate(current_strata.get(stratum, {}), candidate_strata.get(stratum, {}))
        for stratum in ("GOLD_EVIDENCE_RETRIEVED", "PARTIAL_EVIDENCE_RETRIEVED")
    }
    checks = {
        "production_semantic_gain": bool(
            semantic_delta >= SEMANTIC_GAIN_THRESHOLD
            and (v2_reference_semantic is None or float(candidate.get("semantic_claim_coverage") or 0) > v2_reference_semantic)
        ),
        "gold_evidence_stable_gain": strata["GOLD_EVIDENCE_RETRIEVED"]["stable_improvement"],
        "partial_evidence_stable_gain": strata["PARTIAL_EVIDENCE_RETRIEVED"]["stable_improvement"],
        "faithfulness_non_regression": candidate_faithfulness - current_faithfulness >= MAX_FAITHFULNESS_DELTA,
        "hallucination_guard": (
            candidate_hallucination - current_hallucination <= MAX_HALLUCINATION_DELTA
            and candidate_safety["missing_evidence"]["unsupported_expansion_count"] == 0
        ),
        "citation_correctness_non_regression": candidate_citation >= current_citation,
        "citation_completeness_non_regression": candidate_citation_complete >= current_citation_complete - 0.02,
        "no_answer_current_5_of_5": current_no_answer.get("accuracy") == 1.0,
        "no_answer_candidate_5_of_5": candidate_no_answer.get("accuracy") == 1.0,
        "missing_evidence_no_unsupported_expansion": candidate_safety["missing_evidence"]["unsupported_expansion_count"] == 0,
        "p95_generation_latency_acceptable": bool(
            p95_latency_delta is not None
            and p95_latency_delta <= MAX_ACCEPTABLE_P95_LATENCY_DELTA_MS
            and (_ratio(candidate.get("p95_generation_latency_ms"), current.get("p95_generation_latency_ms")) or 0)
            <= MAX_ACCEPTABLE_P95_LATENCY_RATIO
        ),
        "p95_total_rag_latency_acceptable": bool(
            p95_total_delta is None
            or (
                p95_total_delta <= MAX_ACCEPTABLE_P95_LATENCY_DELTA_MS
                and (_ratio(candidate.get("p95_total_rag_latency_ms"), current.get("p95_total_rag_latency_ms")) or 0)
                <= MAX_ACCEPTABLE_P95_LATENCY_RATIO
            )
        ),
        "average_output_tokens_acceptable": bool(
            (_ratio(candidate.get("answer_tokens"), current.get("answer_tokens")) or 0)
            <= MAX_ACCEPTABLE_AVERAGE_TOKEN_RATIO
        ),
        "p95_output_tokens_acceptable": bool(
            (_ratio(candidate.get("p95_completion_tokens"), current.get("p95_completion_tokens")) or 0)
            <= MAX_ACCEPTABLE_P95_TOKEN_RATIO
        ),
        "audit_has_no_unsafe_expansion": int(audit_counts.get("UNSUPPORTED_EXPANSION", 0) or 0) == 0,
        "audit_improvements_not_outnumbered_by_regressions": int(audit_counts.get("CLEAR_IMPROVEMENT", 0) or 0)
        >= int(audit_counts.get("REGRESSION", 0) or 0),
    }
    return {
        "checks": checks,
        "passes": all(checks.values()),
        "thresholds": {
            "semantic_gain": SEMANTIC_GAIN_THRESHOLD,
            "stratum_gain": STRATUM_GAIN_THRESHOLD,
            "max_faithfulness_delta": MAX_FAITHFULNESS_DELTA,
            "max_hallucination_delta": MAX_HALLUCINATION_DELTA,
            "max_p95_generation_latency_delta_ms": MAX_ACCEPTABLE_P95_LATENCY_DELTA_MS,
            "max_p95_latency_ratio": MAX_ACCEPTABLE_P95_LATENCY_RATIO,
            "max_average_output_token_ratio": MAX_ACCEPTABLE_AVERAGE_TOKEN_RATIO,
            "max_p95_output_token_ratio": MAX_ACCEPTABLE_P95_TOKEN_RATIO,
        },
        "delta": {
            "semantic_claim_coverage": semantic_delta,
            "faithfulness": round(candidate_faithfulness - current_faithfulness, 6),
            "hallucination_rate": round(candidate_hallucination - current_hallucination, 6),
            "citation_correctness": round(candidate_citation - current_citation, 6),
            "citation_completeness": round(candidate_citation_complete - current_citation_complete, 6),
            "answer_tokens_ratio": _ratio(candidate.get("answer_tokens"), current.get("answer_tokens")),
            "p95_completion_tokens_ratio": _ratio(candidate.get("p95_completion_tokens"), current.get("p95_completion_tokens")),
            "p95_generation_latency_delta_ms": p95_latency_delta,
            "p95_total_rag_latency_delta_ms": p95_total_delta,
        },
        "strata": strata,
    }


def _choose_verdict(
    validation: dict[str, Any],
    gate: dict[str, Any],
    current_safety: dict[str, Any],
    candidate_safety: dict[str, Any],
    semantic_judge_missing: int,
) -> tuple[str, str]:
    if not validation.get("valid") or semantic_judge_missing:
        return "BLOCKED", "Frozen input or required semantic audit validation failed; no production prompt decision is safe."
    if (
        candidate_safety["missing_evidence"]["unsupported_expansion_count"] > 0
        or candidate_safety["missing_evidence"]["fake_or_invalid_citation_count"] > current_safety["missing_evidence"]["fake_or_invalid_citation_count"]
        or candidate_safety["missing_evidence"]["faithfulness_failure_count"] > current_safety["missing_evidence"]["faithfulness_failure_count"]
    ):
        return "RAG_COMPLETENESS_PROMPT_UNSAFE", "Completeness-aware prompting expanded unsupported claims or regressed missing-evidence safety."
    if gate.get("passes"):
        return "RAG_GENERATION_COMPLETENESS_V3_PASS", "The completeness-only instruction improves production-evidence coverage while all safety, citation, no-answer, and cost gates pass."
    return "RAG_COMPLETENESS_PROMPT_NO_PRODUCTION_GAIN", "The candidate did not satisfy every production-evidence A/B acceptance gate; production prompt remains unchanged."


def _production_prompt_audit(v1: dict[str, Any], current_prompt: str, candidate_prompt: str) -> dict[str, Any]:
    first_raw = (v1.get("raw_generation_runs", {}).get("production") or [{}])[0]
    first_call = (first_raw.get("llm_calls") or [{}])[0]
    return {
        "source_files": [
            "services/greenbook_mcp/greenbook_mcp_server/tools/community.py",
            "packages/agent_core/greenbook_agent_core/llm_compat.py",
        ],
        "entrypoint": "community.answer_from_knowledge",
        "evidence_retrieval": "ctx.java.retrieve_knowledge_evidence",
        "evidence_to_prompt": "structured_call user payload {question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}",
        "evidence_order": "Java response order; V3 supplied frozen production order unchanged",
        "maximum_evidence": 10,
        "citation_id_mapping": "community._validated_sources exact chunkId lookup with canonical postId/title",
        "structured_schema": "JSON object answer string plus sources[{postId,title,chunkId}], additional properties false",
        "model": v1.get("production_generation_chain", {}).get("model"),
        "temperature_observed": first_call.get("temperature"),
        "max_tokens_observed": first_call.get("max_tokens"),
        "response_format_observed": first_call.get("response_format"),
        "stop_condition": "no stop parameter observed; provider/structured_call completion",
        "current_prompt_sha256": _sha256_text(current_prompt),
        "candidate_prompt_sha256": _sha256_text(candidate_prompt),
        "candidate_change_only": COMPLETENESS_AWARE_SUFFIX,
        "schema_changed": False,
        "model_config_changed": False,
        "evidence_order_changed": False,
        "retriever_changed": False,
        "insufficient_evidence": "empty evidence short-circuits to canonical sentinel; malformed/no-source generation fails closed",
    }


def _production_input_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "answerable_count": sum(case["answerable"] for case in cases),
        "no_answer_count": sum(not case["answerable"] for case in cases),
        "case_fingerprints": {
            case["query_id"]: {
                "question_sha256": _sha256_text(case["question"]),
                "evidence_ids": case["evidence_ids"],
                "evidence_fingerprint": case["evidence_fingerprint"],
                "candidate_post_ids": case["candidate_post_ids"],
                "retrieval_stratum": case["retrieval_stratum"],
            }
            for case in cases
        },
    }


def _ab_input_consistency(
    current_raw: dict[str, dict[str, Any]],
    candidate_raw: dict[str, dict[str, Any]],
    cases_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    for query_id, case in cases_by_id.items():
        current = current_raw.get(query_id, {})
        candidate = candidate_raw.get(query_id, {})
        if current.get("input_evidence_ids") != candidate.get("input_evidence_ids"):
            errors.append(f"A/B evidence IDs differ: {query_id}")
        if current.get("question_sha256") != candidate.get("question_sha256"):
            errors.append(f"A/B question differs: {query_id}")
        if current.get("candidate_post_count") != candidate.get("candidate_post_count"):
            errors.append(f"A/B candidate post count differs: {query_id}")
        if case["evidence_ids"] != current.get("input_evidence_ids"):
            errors.append(f"CURRENT output evidence contract differs from frozen input: {query_id}")
        for variant_result in (current, candidate):
            for call in variant_result.get("llm_calls", []):
                if call.get("temperature") != 0.0:
                    errors.append(f"non-zero temperature observed: {query_id}")
    return {"valid": not errors, "errors": errors, "case_count": len(cases_by_id)}


def _case_metric_rows(
    cases: list[dict[str, Any]],
    raw_by_variant: dict[str, dict[str, Any]],
    metrics_by_variant: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for variant in ("CURRENT", "COMPLETENESS_AWARE"):
        for case in cases:
            raw = raw_by_variant[variant][case["query_id"]]
            metrics_by_variant[variant][case["query_id"]] = _make_case_metric(case, raw, variant)


def _serialize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    return raw


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env")
    inputs = _load_inputs(args)
    validation = _validate_inputs(inputs, args)
    if not validation["valid"]:
        raise RuntimeError("Input validation failed: " + "; ".join(validation["errors"]))
    v1 = inputs["v1"]
    model = args.model or v1.get("production_generation_chain", {}).get("model") or os.getenv("LLM_MODEL") or "deepseek-v4-flash"
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required for V3 generation evaluation")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout)
    llm = CapturingLLM(client)
    current_prompt = community._GROUNDED_ANSWER_PROMPT
    candidate_prompt = current_prompt + COMPLETENESS_AWARE_SUFFIX
    cases = inputs["cases"]
    cases_by_id = {case["query_id"]: case for case in cases}
    raw_by_variant: dict[str, dict[str, Any]] = {"CURRENT": {}, "COMPLETENESS_AWARE": {}}
    metrics_by_variant: dict[str, dict[str, dict[str, Any]]] = {"CURRENT": {}, "COMPLETENESS_AWARE": {}}
    semantic_judgments: list[dict[str, Any]] = []
    try:
        for variant, prompt in (("CURRENT", current_prompt), ("COMPLETENESS_AWARE", candidate_prompt)):
            for index, case in enumerate(cases, start=1):
                raw = await _run_one_variant(case, variant=variant, prompt=prompt, llm=llm, model=model)
                raw_by_variant[variant][case["query_id"]] = raw
                print(f"[{variant.lower()} {index:02d}/{len(cases)}] {case['query_id']}", flush=True)
        _case_metric_rows(cases, raw_by_variant, metrics_by_variant)
        for index, case in enumerate([item for item in cases if item["answerable"]], start=1):
            query_id = case["query_id"]
            semantic_judgment = await _semantic_variant_judge(
                case,
                {
                    "CURRENT": _text(raw_by_variant["CURRENT"][query_id].get("answer")),
                    "COMPLETENESS_AWARE": _text(raw_by_variant["COMPLETENESS_AWARE"][query_id].get("answer")),
                },
                llm=llm,
                model=model,
            )
            semantic_judgments.append(semantic_judgment)
            print(f"[semantic {index:02d}/{sum(item['answerable'] for item in cases)}] {query_id}", flush=True)
    finally:
        await client.close()
        community._GROUNDED_ANSWER_PROMPT = current_prompt

    _add_semantic_metrics(metrics_by_variant, semantic_judgments)
    consistency = _ab_input_consistency(raw_by_variant["CURRENT"], raw_by_variant["COMPLETENESS_AWARE"], cases_by_id)
    if not consistency["valid"]:
        validation["valid"] = False
        validation["errors"].extend(consistency["errors"])

    metrics_by_variant_rows = {
        variant: list(metrics_by_variant[variant].values())
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    overall = {
        variant: _aggregate_v3(rows)
        for variant, rows in metrics_by_variant_rows.items()
    }
    retrieval_aware = {
        variant: _aggregate_by_stratum(rows)
        for variant, rows in metrics_by_variant_rows.items()
    }
    no_answer_metrics = {
        variant: {
            "count": sum(not row["answerable"] for row in cases),
            "correct": sum(
                float(metrics_by_variant[variant][case["query_id"]].get("no_answer_correctness") or 0) == 1.0
                for case in cases
                if not case["answerable"]
            ),
        }
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    for variant in no_answer_metrics:
        value = no_answer_metrics[variant]
        value["false_answer_count"] = value["count"] - value["correct"]
        value["accuracy"] = _rate(value["correct"], value["count"])
        value["sentinel"] = community._INSUFFICIENT_EVIDENCE

    safety = {
        variant: _variant_safety(metrics_by_variant_rows[variant])
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    utilization = {
        variant: _evidence_utilization(cases, metrics_by_variant[variant])
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    audit_cases, audit_labels = _select_audit_cases(cases, metrics_by_variant)
    audit = _build_audit(audit_cases, audit_labels, metrics_by_variant)
    v2_metrics = inputs["v2"].get("metrics", {})
    v2_reference_semantic = _safe_float(
        v2_metrics.get("production_semantic_claim_coverage")
        or inputs["v2"].get("overall_metrics", {}).get("production_evidence", {}).get("semantic_claim_coverage")
    )
    gate = _acceptance_gate(
        overall["CURRENT"],
        overall["COMPLETENESS_AWARE"],
        retrieval_aware["CURRENT"],
        retrieval_aware["COMPLETENESS_AWARE"],
        no_answer_metrics["CURRENT"],
        no_answer_metrics["COMPLETENESS_AWARE"],
        safety["CURRENT"],
        safety["COMPLETENESS_AWARE"],
        audit,
        v2_reference_semantic,
    )
    semantic_judge_missing = int(
        overall["COMPLETENESS_AWARE"].get("semantic_judge_missing_count") or 0
    ) + int(overall["CURRENT"].get("semantic_judge_missing_count") or 0)
    verdict_value, verdict_reason = _choose_verdict(
        validation,
        gate,
        safety["CURRENT"],
        safety["COMPLETENESS_AWARE"],
        int(semantic_judge_missing),
    )

    first_bad = {
        variant: _failure_distribution(
            [row for row in metrics_by_variant_rows[variant] if row.get("answerable")]
        )
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    retrieval_limited_count = sum(
        case["retrieval_stratum"] in {"PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"}
        for case in cases
        if case["answerable"]
    )
    generation_limited_current = sum(
        float(metrics_by_variant["CURRENT"][case["query_id"]].get("semantic_claim_coverage") or 0) < 0.5
        for case in cases
        if case["answerable"] and case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED"
    )
    generation_limited_candidate = sum(
        float(metrics_by_variant["COMPLETENESS_AWARE"][case["query_id"]].get("semantic_claim_coverage") or 0) < 0.5
        for case in cases
        if case["answerable"] and case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED"
    )
    v2_generation_limited_count = int(inputs["v2"].get("metrics", {}).get("generation_limited_count") or 0)
    v2_generation_limited_rate = _rate(v2_generation_limited_count, 45)
    v2_metric_false_negative_rate = _safe_float(
        inputs["v2"].get("metrics", {}).get("production_lexical_false_negative_rate_all_claims")
    )
    output: dict[str, Any] = {
        "evaluation": "RAG_GENERATION_COMPLETENESS_V3_PRODUCTION_AB",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checkpoint": {
            "v2_commit": args.v2_commit,
            "v2_verdict": inputs["v2"].get("verdict", {}).get("value"),
            "v2_results_path": str(args.v2_results.relative_to(REPO_ROOT)),
            "v1_results_path": str(args.v1_results.relative_to(REPO_ROOT)),
            "v7_verdict": v1.get("checkpoint", {}).get("v7_verdict"),
            "production_retrieval_unchanged": True,
        },
        "validation": validation,
        "frozen_inputs": _production_input_summary(cases),
        "dataset": {
            "path": str(args.dataset.relative_to(REPO_ROOT)),
            "row_count": len(inputs["dataset_rows"]),
            "answerable_count": sum(case["answerable"] for case in cases),
            "no_answer_count": sum(not case["answerable"] for case in cases),
            "gold_reference_count": sum(len(row.get("gold_chunk_ids", [])) for row in inputs["dataset_rows"]),
        },
        "production_generation_chain": v1.get("production_generation_chain", {}),
        "production_prompt_audit": _production_prompt_audit(v1, current_prompt, candidate_prompt),
        "ab_contract": {
            "variants": {
                "CURRENT": {"prompt_sha256": _sha256_text(current_prompt), "production_prompt": True},
                "COMPLETENESS_AWARE": {
                    "prompt_sha256": _sha256_text(candidate_prompt),
                    "production_prompt": False,
                    "change": COMPLETENESS_AWARE_SUFFIX,
                },
            },
            "only_prompt_instruction_changed": True,
            "schema_changed": False,
            "model_changed": False,
            "temperature_changed": False,
            "max_tokens_changed": False,
            "evidence_order_changed": False,
            "retriever_changed": False,
            "input_consistency": consistency,
        },
        "raw_generation_runs": {
            variant: [_serialize_raw(raw_by_variant[variant][case["query_id"]]) for case in cases]
            for variant in ("CURRENT", "COMPLETENESS_AWARE")
        },
        "case_metrics": {
            variant: [metrics_by_variant[variant][case["query_id"]] for case in cases]
            for variant in ("CURRENT", "COMPLETENESS_AWARE")
        },
        "semantic_judgments": semantic_judgments,
        "overall_metrics": overall,
        "retrieval_aware_metrics": retrieval_aware,
        "no_answer_metrics": no_answer_metrics,
        "safety_checks": safety,
        "evidence_utilization": utilization,
        "human_semantic_audit": audit,
        "first_bad_state_distribution": first_bad,
        "diagnostic_rates": {
            "retrieval_limited": {
                "count": retrieval_limited_count,
                "rate": _rate(retrieval_limited_count, 45),
                "definition": "PARTIAL_EVIDENCE_RETRIEVED or REQUIRED_EVIDENCE_MISSING in frozen production evidence",
            },
            "generation_limited_current_full_evidence": {
                "count": generation_limited_current,
                "rate": _rate(generation_limited_current, sum(case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED" for case in cases)),
                "definition": "CURRENT semantic coverage below 0.50 among production cases with all exact gold chunks",
            },
            "generation_limited_completeness_aware_full_evidence": {
                "count": generation_limited_candidate,
                "rate": _rate(generation_limited_candidate, sum(case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED" for case in cases)),
                "definition": "COMPLETENESS_AWARE semantic coverage below 0.50 among production cases with all exact gold chunks",
            },
            "v2_gold_oracle_generation_limited": {
                "count": v2_generation_limited_count,
                "rate": v2_generation_limited_rate,
                "definition": "V2 gold-evidence oracle semantic coverage below 0.50; retained checkpoint diagnostic",
            },
            "v2_production_metric_false_negative_rate": {
                "rate": v2_metric_false_negative_rate,
                "definition": "V2 production lexical misses judged semantically covered / all answerable claims; non-exclusive metric diagnostic",
            },
            "v2_production_semantic_reference": {
                "value": v2_reference_semantic,
                "definition": "V2 production semantic claim coverage retained as the pre-V3 reference",
            },
        },
        "acceptance_gate": gate,
        "verdict": {"value": verdict_value, "reason": verdict_reason},
        "llm_judge": {
            "used": True,
            "role": "auxiliary semantic claim comparison; not the sole verdict signal",
            "model": model,
            "prompt_sha256": _sha256_text(VARIANT_JUDGE_PROMPT),
            "input_output_recorded": True,
            "deterministic_safety_checks_retained": True,
        },
        "production_files_changed": [],
        "post_evaluation_production_state": {
            "prompt_applied": False,
            "production_prompt_sha256": current_prompt_sha256 if (current_prompt_sha256 := _sha256_text(current_prompt)) else None,
            "focused_regression_status": None,
        },
        "next_recommendation": (
            "If and only if the V3 gate passes, apply the exact COMPLETENESS_AWARE suffix to community.answer_from_knowledge and keep that one production file dirty for acceptance. Otherwise keep production unchanged and stop prompt experiments."
        ),
        "files": {
            "script": str(Path(__file__).resolve().relative_to(REPO_ROOT)),
            "results": str(args.output.relative_to(REPO_ROOT)),
            "report": str(args.report.relative_to(REPO_ROOT)),
            "dirty_files": [],
        },
    }
    return output


def _recompute_existing(args: argparse.Namespace) -> dict[str, Any]:
    output = _load_json(args.output)
    inputs = _load_inputs(args)
    validation = _validate_inputs(inputs, args)
    raw_by_variant = {
        variant: {
            _text(item.get("query_id")): item
            for item in output.get("raw_generation_runs", {}).get(variant, [])
        }
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    metrics_by_variant: dict[str, dict[str, dict[str, Any]]] = {"CURRENT": {}, "COMPLETENESS_AWARE": {}}
    _case_metric_rows(inputs["cases"], raw_by_variant, metrics_by_variant)
    semantic_judgments = output.get("semantic_judgments", [])
    _add_semantic_metrics(metrics_by_variant, semantic_judgments)
    cases = inputs["cases"]
    cases_by_id = {case["query_id"]: case for case in cases}
    consistency = _ab_input_consistency(
        raw_by_variant["CURRENT"],
        raw_by_variant["COMPLETENESS_AWARE"],
        cases_by_id,
    )
    if not consistency["valid"]:
        validation["valid"] = False
        validation["errors"].extend(consistency["errors"])
    metric_rows = {
        variant: list(metrics_by_variant[variant].values())
        for variant in ("CURRENT", "COMPLETENESS_AWARE")
    }
    overall = {variant: _aggregate_v3(rows) for variant, rows in metric_rows.items()}
    retrieval_aware = {variant: _aggregate_by_stratum(rows) for variant, rows in metric_rows.items()}
    no_answer_metrics: dict[str, dict[str, Any]] = {}
    for variant in ("CURRENT", "COMPLETENESS_AWARE"):
        no_answer_rows = [row for row in metric_rows[variant] if not row.get("answerable")]
        correct = sum(float(row.get("no_answer_correctness") or 0) == 1.0 for row in no_answer_rows)
        no_answer_metrics[variant] = {
            "count": len(no_answer_rows),
            "correct": correct,
            "false_answer_count": len(no_answer_rows) - correct,
            "accuracy": _rate(correct, len(no_answer_rows)),
            "sentinel": community._INSUFFICIENT_EVIDENCE,
        }
    safety = {variant: _variant_safety(metric_rows[variant]) for variant in metric_rows}
    utilization = {
        variant: _evidence_utilization(cases, metrics_by_variant[variant])
        for variant in metric_rows
    }
    audit_cases, audit_labels = _select_audit_cases(cases, metrics_by_variant)
    audit = _build_audit(audit_cases, audit_labels, metrics_by_variant)
    v2_metrics = inputs["v2"].get("metrics", {})
    v2_reference_semantic = _safe_float(
        v2_metrics.get("production_semantic_claim_coverage")
        or inputs["v2"].get("overall_metrics", {}).get("production_evidence", {}).get("semantic_claim_coverage")
    )
    gate = _acceptance_gate(
        overall["CURRENT"],
        overall["COMPLETENESS_AWARE"],
        retrieval_aware["CURRENT"],
        retrieval_aware["COMPLETENESS_AWARE"],
        no_answer_metrics["CURRENT"],
        no_answer_metrics["COMPLETENESS_AWARE"],
        safety["CURRENT"],
        safety["COMPLETENESS_AWARE"],
        audit,
        v2_reference_semantic,
    )
    semantic_judge_missing = int(overall["CURRENT"].get("semantic_judge_missing_count") or 0) + int(
        overall["COMPLETENESS_AWARE"].get("semantic_judge_missing_count") or 0
    )
    verdict_value, verdict_reason = _choose_verdict(
        validation,
        gate,
        safety["CURRENT"],
        safety["COMPLETENESS_AWARE"],
        semantic_judge_missing,
    )
    first_bad = {
        variant: _failure_distribution([row for row in metric_rows[variant] if row.get("answerable")])
        for variant in metric_rows
    }
    retrieval_limited_count = sum(
        case["answerable"] and case["retrieval_stratum"] in {"PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"}
        for case in cases
    )
    full_count = sum(case["answerable"] and case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED" for case in cases)
    generation_limited_current = sum(
        float(metrics_by_variant["CURRENT"][case["query_id"]].get("semantic_claim_coverage") or 0) < 0.5
        for case in cases
        if case["answerable"] and case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED"
    )
    generation_limited_candidate = sum(
        float(metrics_by_variant["COMPLETENESS_AWARE"][case["query_id"]].get("semantic_claim_coverage") or 0) < 0.5
        for case in cases
        if case["answerable"] and case["retrieval_stratum"] == "GOLD_EVIDENCE_RETRIEVED"
    )
    v2_generation_limited_count = int(inputs["v2"].get("metrics", {}).get("generation_limited_count") or 0)
    v2_metric_false_negative_rate = _safe_float(
        inputs["v2"].get("metrics", {}).get("production_lexical_false_negative_rate_all_claims")
    )
    output.update(
        {
            "validation": validation,
            "ab_contract": {
                **output.get("ab_contract", {}),
                "input_consistency": consistency,
            },
            "production_prompt_audit": _production_prompt_audit(
                inputs["v1"],
                community._GROUNDED_ANSWER_PROMPT,
                community._GROUNDED_ANSWER_PROMPT + COMPLETENESS_AWARE_SUFFIX,
            ),
            "case_metrics": {
                variant: [metrics_by_variant[variant][case["query_id"]] for case in cases]
                for variant in metric_rows
            },
            "overall_metrics": overall,
            "retrieval_aware_metrics": retrieval_aware,
            "no_answer_metrics": no_answer_metrics,
            "safety_checks": safety,
            "evidence_utilization": utilization,
            "human_semantic_audit": audit,
            "first_bad_state_distribution": first_bad,
            "diagnostic_rates": {
                "retrieval_limited": {
                    "count": retrieval_limited_count,
                    "rate": _rate(retrieval_limited_count, 45),
                    "definition": "PARTIAL_EVIDENCE_RETRIEVED or REQUIRED_EVIDENCE_MISSING in frozen production evidence",
                },
                "generation_limited_current_full_evidence": {
                    "count": generation_limited_current,
                    "rate": _rate(generation_limited_current, full_count),
                    "definition": "CURRENT semantic coverage below 0.50 among production cases with all exact gold chunks",
                },
                "generation_limited_completeness_aware_full_evidence": {
                    "count": generation_limited_candidate,
                    "rate": _rate(generation_limited_candidate, full_count),
                    "definition": "COMPLETENESS_AWARE semantic coverage below 0.50 among production cases with all exact gold chunks",
                },
                "v2_gold_oracle_generation_limited": {
                    "count": v2_generation_limited_count,
                    "rate": _rate(v2_generation_limited_count, 45),
                    "definition": "V2 gold-evidence oracle semantic coverage below 0.50; retained checkpoint diagnostic",
                },
                "v2_production_metric_false_negative_rate": {
                    "rate": v2_metric_false_negative_rate,
                    "definition": "V2 production lexical misses judged semantically covered / all answerable claims; non-exclusive metric diagnostic",
                },
                "v2_production_semantic_reference": {
                    "value": v2_reference_semantic,
                    "definition": "V2 production semantic claim coverage retained as the pre-V3 reference",
                },
            },
            "acceptance_gate": gate,
            "verdict": {"value": verdict_value, "reason": verdict_reason},
        }
    )
    live_prompt_sha = _sha256_text(community._GROUNDED_ANSWER_PROMPT)
    candidate_sha = _text(output.get("production_prompt_audit", {}).get("candidate_prompt_sha256"))
    applied = bool(candidate_sha and live_prompt_sha == candidate_sha)
    output["post_evaluation_production_state"] = {
        "prompt_applied": applied,
        "production_prompt_sha256": live_prompt_sha,
        "focused_regression_status": args.production_regression_status,
    }
    output["production_files_changed"] = (
        ["services/greenbook_mcp/greenbook_mcp_server/tools/community.py"] if applied else []
    )
    output["next_recommendation"] = (
        "Production prompt change is intentionally left dirty for user acceptance; no retrieval or performance work should start yet."
        if applied
        else "Production remains unchanged because the V3 candidate prompt was not applied. Stop prompt experiments unless a new acceptance decision is requested."
    )
    return output


def _render_report(output: dict[str, Any]) -> str:
    verdict = output["verdict"]
    validation = output["validation"]
    overall = output["overall_metrics"]
    current = overall["CURRENT"]
    candidate = overall["COMPLETENESS_AWARE"]
    retrieval = output["retrieval_aware_metrics"]
    no_answer = output["no_answer_metrics"]
    gate = output["acceptance_gate"]
    audit = output["human_semantic_audit"]
    lines = [
        "# RAG_GENERATION_COMPLETENESS_V3_PRODUCTION_AB",
        "",
        f"**Verdict:** `{verdict['value']}`  ",
        f"**Reason:** {verdict['reason']}",
        "",
        "## 1. V2 checkpoint and frozen scope",
        "",
        f"- V2 checkpoint: `{output['checkpoint']['v2_commit']}`; V2 verdict: `{output['checkpoint']['v2_verdict']}`.",
        f"- Dataset: `{output['dataset']['row_count']}` rows (`{output['dataset']['answerable_count']}` answerable, `{output['dataset']['no_answer_count']}` no-answer).",
        f"- Dataset SHA256: `{validation['dataset_sha256']}`; snapshot SHA256: `{validation['snapshot_sha256']}`; snapshot digest: `{validation['snapshot_digest']}`.",
        f"- V2 SHA matches: dataset `{validation['dataset_sha256'] == validation.get('v2_validation_dataset_sha256')}`, snapshot `{validation['snapshot_sha256'] == validation.get('v2_validation_snapshot_sha256')}`, V1 results `{validation['v1_results_sha256'] == validation.get('v2_validation_v1_results_sha256')}`.",
        f"- Frozen snapshot drift: `{validation['snapshot_drift']}`; live retrieval calls: `{validation['live_retrieval_calls']}`.",
        f"- Input A/B contract valid: `{output['ab_contract']['input_consistency']['valid']}`; all input fingerprints and evidence IDs are recorded in JSON.",
        "- The 45 answerable contexts came from the frozen production Top10 chunk order. The five no-answer contexts reuse the captured V1 production evidence, exactly as the V1 frozen protocol requires.",
        "",
        "## 2. Actual production generation chain",
        "",
        "`community.answer_from_knowledge` -> `ctx.java.retrieve_knowledge_evidence` -> evidence payload -> `structured_call` -> `_grounded_payload` -> `_validated_sources` -> response.",
        f"- Current prompt SHA256: `{output['production_prompt_audit']['current_prompt_sha256']}`.",
        f"- Candidate prompt SHA256: `{output['production_prompt_audit']['candidate_prompt_sha256']}`.",
        f"- Model: `{output['production_prompt_audit']['model']}`; temperature observed: `{output['production_prompt_audit']['temperature_observed']}`; max tokens: `{output['production_prompt_audit']['max_tokens_observed']}`; response format: `{output['production_prompt_audit']['response_format_observed']}`.",
        f"- Evidence maximum: `{output['production_prompt_audit']['maximum_evidence']}`; ordering and citation rewrite unchanged.",
        f"- A/B changed only this instruction: `{COMPLETENESS_AWARE_SUFFIX.strip()}`",
        "",
        "## 3. CURRENT vs COMPLETENESS_AWARE overall metrics",
        "",
        "All answerable metrics below use the same frozen production evidence; semantic coverage is an auxiliary judge metric and lexical/deterministic metrics remain visible.",
        "",
        "| Variant | Semantic claim coverage | Lexical claim coverage | Correctness | Faithfulness | Hallucination | Citation correctness | Citation completeness | Completeness |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| CURRENT | {_fmt(current.get('semantic_claim_coverage'))} | {_fmt(current.get('lexical_claim_coverage'))} | {_fmt(current.get('answer_correctness'))} | {_fmt(current.get('faithfulness'))} | {_fmt(current.get('hallucination_rate'))} | {_fmt(current.get('citation_correctness'))} | {_fmt(current.get('citation_completeness'))} | {_fmt(current.get('answer_completeness'))} |",
        f"| COMPLETENESS_AWARE | {_fmt(candidate.get('semantic_claim_coverage'))} | {_fmt(candidate.get('lexical_claim_coverage'))} | {_fmt(candidate.get('answer_correctness'))} | {_fmt(candidate.get('faithfulness'))} | {_fmt(candidate.get('hallucination_rate'))} | {_fmt(candidate.get('citation_correctness'))} | {_fmt(candidate.get('citation_completeness'))} | {_fmt(candidate.get('answer_completeness'))} |",
        f"| Delta (candidate - current) | {_fmt(gate['delta'].get('semantic_claim_coverage'))} | {_fmt((candidate.get('lexical_claim_coverage') or 0) - (current.get('lexical_claim_coverage') or 0))} | {_fmt((candidate.get('answer_correctness') or 0) - (current.get('answer_correctness') or 0))} | {_fmt(gate['delta'].get('faithfulness'))} | {_fmt(gate['delta'].get('hallucination_rate'))} | {_fmt(gate['delta'].get('citation_correctness'))} | {_fmt(gate['delta'].get('citation_completeness'))} | {_fmt((candidate.get('answer_completeness') or 0) - (current.get('answer_completeness') or 0))} |",
        "",
        "V2 production semantic reference: `" + _fmt(output["diagnostic_rates"]["v2_production_semantic_reference"].get("value")) + "`; V2 production lexical false-negative rate: `" + _fmt(output["diagnostic_rates"]["v2_production_metric_false_negative_rate"].get("rate")) + "`. Both are retained as non-exclusive diagnostic references, not as the sole gate signal.",
        "",
        "## 4. Retrieval-aware generation metrics",
        "",
        "| Evidence stratum | N | CURRENT semantic | AWARE semantic | CURRENT correctness | AWARE correctness | CURRENT faithfulness | AWARE faithfulness |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stratum in ("GOLD_EVIDENCE_RETRIEVED", "PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"):
        current_stratum = retrieval["CURRENT"].get(stratum, {})
        candidate_stratum = retrieval["COMPLETENESS_AWARE"].get(stratum, {})
        lines.append(
            f"| {stratum} | {current_stratum.get('count', 0)} | {_fmt(current_stratum.get('semantic_claim_coverage'))} | {_fmt(candidate_stratum.get('semantic_claim_coverage'))} | {_fmt(current_stratum.get('answer_correctness'))} | {_fmt(candidate_stratum.get('answer_correctness'))} | {_fmt(current_stratum.get('faithfulness'))} | {_fmt(candidate_stratum.get('faithfulness'))} |"
        )
    lines += [
        "",
        f"- Frozen production retrieval-limited: `{output['diagnostic_rates']['retrieval_limited']['count']}/45` = `{_fmt(output['diagnostic_rates']['retrieval_limited']['rate'])}`.",
        f"- V2 gold-oracle generation-limited reference: `{output['diagnostic_rates']['v2_gold_oracle_generation_limited']['count']}/45` = `{_fmt(output['diagnostic_rates']['v2_gold_oracle_generation_limited']['rate'])}`; this is not charged to production retrieval.",
        "- These diagnostic rates are intentionally not treated as a mutually exclusive partition: retrieval absence, generator omission, and metric false negatives can overlap at different observations.",
        "",
        "## 5. No-answer and missing-evidence safety",
        "",
        f"- CURRENT no-answer accuracy: `{no_answer['CURRENT']['correct']}/{no_answer['CURRENT']['count']}` = `{_fmt(no_answer['CURRENT']['accuracy'])}`.",
        f"- COMPLETENESS_AWARE no-answer accuracy: `{no_answer['COMPLETENESS_AWARE']['correct']}/{no_answer['COMPLETENESS_AWARE']['count']}` = `{_fmt(no_answer['COMPLETENESS_AWARE']['accuracy'])}`.",
        "",
        "| Variant | Missing-evidence cases | Unsupported expansion cases | Unsupported factual claims | Faithfulness failures | Invalid/fake citation cases | Safe refusal cases |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ("CURRENT", "COMPLETENESS_AWARE"):
        missing = output["safety_checks"][variant]["missing_evidence"]
        lines.append(
            f"| {variant} | {missing['count']} | {missing['unsupported_expansion_count']} | {missing['unsupported_claim_count']} | {missing['faithfulness_failure_count']} | {missing['fake_or_invalid_citation_count']} | {missing['safe_refusal_count']} |"
        )
    lines += [
        "",
        "The critical safety condition is zero candidate unsupported factual expansion in `REQUIRED_EVIDENCE_MISSING`; list headings and evidence-qualified limitations remain visible in raw diagnostics but are not factual hallucinations. Evidence insufficiency must not trigger model-knowledge completion.",
        "",
        "## 6. Evidence utilization",
        "",
        "| Variant | Evidence support rate | Evidence utilization rate | Lexical claim utilization | Semantic claim utilization | Available-but-underused cases |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant in ("CURRENT", "COMPLETENESS_AWARE"):
        aggregate = output["evidence_utilization"][variant]["aggregate"]
        lines.append(
            f"| {variant} | {_fmt(aggregate.get('evidence_support_rate'))} | {_fmt(aggregate.get('evidence_utilization_rate'))} | {_fmt(aggregate.get('claim_utilization_rate_lexical'))} | {_fmt(aggregate.get('claim_utilization_rate_semantic'))} | {aggregate.get('evidence_available_but_underused_count', 0)} |"
        )
    lines += [
        "",
        "## 7. Semantic/rule audit (20 cases)",
        "",
        f"- Method: `{audit['method']}`.",
        f"- Classification counts: `{audit['classification_counts']}`.",
        "- The JSON artifact retains query, gold answer/claims, full provided evidence, both answers, lexical support diagnostics, semantic claim decisions, and the classification for each selected case.",
        "",
        "| Query | Stratum | Classification | CURRENT semantic | AWARE semantic | CURRENT tokens | AWARE tokens |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for item in audit["cases"]:
        lines.append(
            f"| {item['query_id']} | {item['retrieval_stratum']} | {item['classification']} | {_fmt(item['current'].get('semantic_coverage'))} | {_fmt(item['completeness_aware'].get('semantic_coverage'))} | {item['current'].get('completion_tokens') or 'n/a'} | {item['completeness_aware'].get('completion_tokens') or 'n/a'} |"
        )
    lines += [
        "",
        "## 8. Latency and token A/B",
        "",
        "| Metric | CURRENT p50 | CURRENT p95 | AWARE p50 | AWARE p95 | Delta/ratio |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Generation latency (ms) | {_fmt(current.get('p50_generation_latency_ms'))} | {_fmt(current.get('p95_generation_latency_ms'))} | {_fmt(candidate.get('p50_generation_latency_ms'))} | {_fmt(candidate.get('p95_generation_latency_ms'))} | {_fmt(gate['delta'].get('p95_generation_latency_delta_ms'))} ms |",
        f"| Total RAG latency (ms, historical retrieval + generation) | {_fmt(current.get('p50_total_rag_latency_ms'))} | {_fmt(current.get('p95_total_rag_latency_ms'))} | {_fmt(candidate.get('p50_total_rag_latency_ms'))} | {_fmt(candidate.get('p95_total_rag_latency_ms'))} | {_fmt(gate['delta'].get('p95_total_rag_latency_delta_ms'))} ms |",
        f"| Input prompt tokens | {_fmt(current.get('p50_prompt_tokens'))} | {_fmt(current.get('p95_prompt_tokens'))} | {_fmt(candidate.get('p50_prompt_tokens'))} | {_fmt(candidate.get('p95_prompt_tokens'))} | ratio {_fmt(_ratio(candidate.get('prompt_tokens'), current.get('prompt_tokens')))} |",
        f"| Output tokens | {_fmt(current.get('p50_completion_tokens'))} | {_fmt(current.get('p95_completion_tokens'))} | {_fmt(candidate.get('p50_completion_tokens'))} | {_fmt(candidate.get('p95_completion_tokens'))} | ratio {_fmt(gate['delta'].get('answer_tokens_ratio'))} |",
        f"- V1 reference generation p50/p95 was 2253/3902 ms; V3 records fresh provider observations in the JSON artifact and does not optimize them in this phase.",
        "",
        "## 9. FIRST_BAD_STATE and failure families",
        "",
        "| FIRST_BAD_STATE / family | CURRENT count | AWARE count |",
        "|---|---:|---:|",
    ]
    states = sorted(set(output["first_bad_state_distribution"]["CURRENT"]) | set(output["first_bad_state_distribution"]["COMPLETENESS_AWARE"]))
    for state in states:
        lines.append(
            f"| {state} | {output['first_bad_state_distribution']['CURRENT'].get(state, {}).get('count', 0)} | {output['first_bad_state_distribution']['COMPLETENESS_AWARE'].get(state, {}).get('count', 0)} |"
        )
    lines += [
        "",
        "The protected ordering remains `POST_RETRIEVAL -> CHUNK_RETRIEVAL` for cases where required evidence is absent. Only after evidence reaches generation can a completeness or citation failure be charged to the generator boundary.",
        "",
        "## 10. Acceptance gate",
        "",
        f"- Gate passes: `{gate['passes']}`.",
    ]
    for name, value in gate["checks"].items():
        lines.append(f"- `{name}`: `{value}`.")
    lines += [
        "",
        "## 11. Production boundary and final state",
        "",
        f"- Production files changed at evaluation start: `0`.",
        f"- Production files changed after optional application: `{output.get('production_files_changed') or []}`.",
        f"- Prompt applied: `{output.get('post_evaluation_production_state', {}).get('prompt_applied')}`.",
        f"- Focused regression status: `{output.get('post_evaluation_production_state', {}).get('focused_regression_status') or 'not run; candidate not applied'}`.",
        f"- Dirty files: `{', '.join(output['files'].get('dirty_files', [])) or '(none)'}`.",
        "- Retrieval, chunking, embedding, Qdrant, hybrid search, evidence selection, schema, model configuration, Memory, Runtime, MCP, and Java were not modified.",
        "",
        "## 12. Verdict and recommendation",
        "",
        f"`{verdict['value']}` — {verdict['reason']}",
        "",
        output["next_recommendation"],
        "",
        "Remaining accepted limitations: post/chunk retrieval quality remains the dominant production limitation; semantic judge results are auxiliary and provider-dependent; latency uses frozen/historical retrieval observations and is a baseline, not an optimization result.",
    ]
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--v1-results", type=Path, default=V1_OUTPUT)
    parser.add_argument("--v2-results", type=Path, default=DEFAULT_V2_OUTPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--v2-commit", default=V2_CHECKPOINT_COMMIT)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--recompute-existing", action="store_true")
    parser.add_argument("--production-regression-status", default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    for name in ("dataset", "snapshot", "runs", "v1_results", "v2_results", "output", "report"):
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
