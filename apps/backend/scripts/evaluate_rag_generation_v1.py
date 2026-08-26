"""Evaluation-only RAG generation audit V1.

This harness deliberately enters the existing production generation boundary
(``community.answer_from_knowledge``) with frozen evidence.  It does not call
Java, Qdrant, MySQL, or the live retriever.  The answerable production context
comes from ``rag_retrieval_frozen_snapshot_v1.json``; the five no-answer
contexts come from the already captured retrieval run because the frozen
retrieval snapshot intentionally contains answerable queries only.

The oracle run changes only the evidence input: it supplies the dataset's
gold chunks to the same production function, prompt, schema, and model.  It is
diagnostic and must never be treated as a production retrieval proposal.
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
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_java_client.models import KnowledgeEvidenceResponse
from greenbook_mcp_server.context import ToolContext
from greenbook_mcp_server.tools import community
from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "docs/evaluation/rag_evidence_dataset_v2.jsonl"
DEFAULT_SNAPSHOT = REPO_ROOT / "docs/evaluation/rag_retrieval_frozen_snapshot_v1.json"
DEFAULT_RUNS = REPO_ROOT / "docs/evaluation/rag_evidence_runs_20260825.jsonl"
DEFAULT_V7_RESULTS = REPO_ROOT / "docs/evaluation/rag_semantic_ranking_v7_results.json"
DEFAULT_OUTPUT = REPO_ROOT / "docs/evaluation/rag_generation_evaluation_v1_results.json"
DEFAULT_REPORT = REPO_ROOT / "docs/reports/RAG_GENERATION_EVALUATION_V1.md"

V7_CHECKPOINT_COMMIT = "19f6f8e"
CANONICAL_TOP_POSTS = 10
CANONICAL_TOP_CHUNKS = 10
PRODUCTION_MAX_EVIDENCE = 10
SUPPORT_TERM_RECALL_THRESHOLD = 0.25
MIN_SHARED_SUPPORT_TERMS = 2
CORRECTNESS_FAILURE_THRESHOLD = 0.50
COMPLETENESS_FAILURE_THRESHOLD = 0.50
FAITHFULNESS_FAILURE_THRESHOLD = 0.75
CITATION_COMPLETENESS_FAILURE_THRESHOLD = 0.75
FAILURE_CATEGORIES = (
    "POST_RETRIEVAL_FAILURE",
    "CHUNK_RETRIEVAL_FAILURE",
    "EVIDENCE_SELECTION_FAILURE",
    "CONTEXT_CONSTRUCTION_FAILURE",
    "GENERATION_FAITHFULNESS_FAILURE",
    "GENERATION_COMPLETENESS_FAILURE",
    "CITATION_FAILURE",
    "NO_ANSWER_FAILURE",
    "DATASET_ISSUE",
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
        "when",
        "which",
        "with",
    }
)


def _text(value: Any) -> str:
    return str(value or "")


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(_text(value).encode("utf-8")).hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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


def _split_claims(value: Any) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[。！？!?;；\n]+", _text(value))
        if item.strip()
    ]


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 6)


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": _mean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": round(min(values), 6) if values else None,
        "max": round(max(values), 6) if values else None,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _git_short_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _catalog_evidence(
    raw: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    *,
    default_score: float = 1.0,
) -> dict[str, Any]:
    chunk_id = _text(raw.get("chunk_id") or raw.get("chunkId"))
    catalog = catalog_by_id.get(chunk_id, {})
    post_id = _text(raw.get("post_id") or raw.get("postId") or catalog.get("post_id"))
    content = _text(raw.get("content") or catalog.get("content"))
    title = _text(raw.get("title") or catalog.get("title"))
    return {
        "chunkId": chunk_id,
        "postId": post_id,
        "title": title,
        "content": content,
        "score": float(raw.get("score", default_score) or default_score),
        "startOffset": int(raw.get("start_offset", raw.get("startOffset", catalog.get("start_offset", 0))) or 0),
        "endOffset": int(raw.get("end_offset", raw.get("endOffset", catalog.get("end_offset", len(content)))) or 0),
        "eventVersion": int(raw.get("event_version", raw.get("eventVersion", catalog.get("event_version", 0))) or 0),
    }


def _validate_inputs(
    dataset_rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    retrieval_runs: list[dict[str, Any]],
    v7_results: dict[str, Any],
    *,
    dataset_path: Path,
    snapshot_path: Path,
    runs_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    dataset_by_id = {_text(row.get("query_id")): row for row in dataset_rows}
    snapshot_by_id = {_text(row.get("query_id")): row for row in snapshot.get("queries", [])}
    runs_by_id = {_text(row.get("query_id")): row for row in retrieval_runs}
    catalog_by_id = {
        _text(row.get("chunk_id")): row for row in snapshot.get("chunk_catalog", []) if row.get("chunk_id")
    }
    errors: list[str] = []
    answerable = [row for row in dataset_rows if row.get("gold_chunk_ids")]
    no_answer = [row for row in dataset_rows if not row.get("gold_chunk_ids")]
    if len(dataset_rows) != 50:
        errors.append(f"dataset row count={len(dataset_rows)}, expected 50")
    if len(answerable) != 45 or len(no_answer) != 5:
        errors.append(f"answerable/no-answer={len(answerable)}/{len(no_answer)}, expected 45/5")
    if set(snapshot_by_id) != {_text(row.get("query_id")) for row in answerable}:
        errors.append("frozen snapshot query IDs do not exactly cover the 45 answerable rows")
    if not all(query_id in runs_by_id for query_id in dataset_by_id):
        errors.append("retrieval run artifact does not cover all 50 dataset rows")
    for row in dataset_rows:
        for chunk_id in row.get("gold_chunk_ids", []):
            if _text(chunk_id) not in catalog_by_id:
                errors.append(f"gold chunk missing from snapshot catalog: {chunk_id}")
    for row in answerable:
        query_id = _text(row["query_id"])
        for candidate in snapshot_by_id[query_id].get("candidate_chunks", [])[:CANONICAL_TOP_CHUNKS]:
            if _text(candidate.get("chunk_id")) not in catalog_by_id:
                errors.append(f"candidate chunk missing from snapshot catalog: {query_id}/{candidate.get('chunk_id')}")
    for row in no_answer:
        query_id = _text(row["query_id"])
        for evidence in runs_by_id[query_id].get("evidence", [])[:PRODUCTION_MAX_EVIDENCE]:
            if _text(evidence.get("chunk_id")) not in catalog_by_id:
                errors.append(f"no-answer evidence missing from snapshot catalog: {query_id}/{evidence.get('chunk_id')}")

    v7_snapshot = v7_results.get("snapshot", {})
    current_snapshot_sha = _sha256_bytes(snapshot_path)
    expected_snapshot_sha = _text(v7_snapshot.get("snapshot_file_sha256"))
    if expected_snapshot_sha and current_snapshot_sha != expected_snapshot_sha:
        errors.append("frozen snapshot file SHA differs from the V7 checkpoint")
    if not snapshot.get("snapshot_digest"):
        errors.append("frozen snapshot has no snapshot_digest")
    if v7_results.get("verdict") != "RAG_SEMANTIC_RANKING_NO_GAIN":
        errors.append(f"unexpected V7 verdict={v7_results.get('verdict')!r}")

    validation = {
        "valid": not errors,
        "errors": errors,
        "dataset_row_count": len(dataset_rows),
        "answerable_count": len(answerable),
        "no_answer_count": len(no_answer),
        "dataset_file_sha256": _sha256_bytes(dataset_path),
        "snapshot_file_sha256": current_snapshot_sha,
        "snapshot_file_sha256_matches_v7": (not expected_snapshot_sha or current_snapshot_sha == expected_snapshot_sha),
        "snapshot_digest": snapshot.get("snapshot_digest"),
        "snapshot_query_count": len(snapshot_by_id),
        "snapshot_candidate_post_depth": len(snapshot.get("queries", [{}])[0].get("candidate_posts", [])) if snapshot.get("queries") else 0,
        "snapshot_candidate_chunk_depth_used": CANONICAL_TOP_CHUNKS,
        "snapshot_chunk_catalog_count": len(catalog_by_id),
        "retrieval_runs_file_sha256": _sha256_bytes(runs_path),
        "retrieval_runs_used_for": "the five no-answer contexts and historical latency baseline only",
        "snapshot_drift": 0 if not errors and current_snapshot_sha == expected_snapshot_sha else None,
    }
    return validation, dataset_by_id, snapshot_by_id, runs_by_id | {"__catalog__": catalog_by_id}


class FrozenEvidenceJava:
    """Minimal Java boundary double; it never contacts a downstream service."""

    def __init__(self, evidence: list[dict[str, Any]], *, candidate_post_count: int, latencies: dict[str, Any]) -> None:
        self.evidence = evidence
        self.candidate_post_count = candidate_post_count
        self.latencies = latencies
        self.calls: list[dict[str, Any]] = []

    async def retrieve_knowledge_evidence(self, question: str, **kwargs: Any) -> ToolResult[Any]:
        self.calls.append(
            {
                "question_sha256": _sha256_text(question),
                "top_posts": kwargs.get("top_posts"),
                "top_chunks": kwargs.get("top_chunks"),
                "trace_id": kwargs.get("trace_id"),
            }
        )
        response = KnowledgeEvidenceResponse.model_validate(
            {
                "chunks": self.evidence,
                "candidatePostCount": self.candidate_post_count,
                "embeddingLatencyMs": int(self.latencies.get("embedding", 0) or 0),
                "chunkRetrievalLatencyMs": int(self.latencies.get("chunk_retrieval", 0) or 0),
                "degraded": False,
            }
        )
        return ToolResult.success(response, trace_id=kwargs.get("trace_id"))


class CapturingLLM:
    """OpenAI-compatible wrapper that records metrics without storing prompts."""

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self.base_url = getattr(client, "base_url", "")
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        messages = kwargs.get("messages") or []
        system_content = _text(messages[0].get("content")) if messages else ""
        user_content = _text(messages[1].get("content")) if len(messages) > 1 else ""
        user_payload: dict[str, Any] = {}
        try:
            parsed = json.loads(user_content)
            if isinstance(parsed, dict):
                user_payload = parsed
        except json.JSONDecodeError:
            pass
        evidence = user_payload.get("evidence") if isinstance(user_payload.get("evidence"), list) else []
        request_record: dict[str, Any] = {
            "model": _text(kwargs.get("model")),
            "temperature": kwargs.get("temperature"),
            "response_format": (kwargs.get("response_format") or {}).get("type"),
            "max_tokens": kwargs.get("max_tokens"),
            "system_prompt_sha256": _sha256_text(system_content),
            "system_prompt_chars": len(system_content),
            "user_payload_sha256": _sha256_text(user_content),
            "user_payload_chars": len(user_content),
            "question_sha256": _sha256_text(user_payload.get("question")),
            "evidence_ids": [_text(item.get("chunkId")) for item in evidence if isinstance(item, dict)],
            "evidence_count": len(evidence),
            "evidence_content_chars": sum(len(_text(item.get("content"))) for item in evidence if isinstance(item, dict)),
        }
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            request_record.update(
                {
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_type": type(exc).__name__,
                    "error": _text(exc)[:500],
                }
            )
            self.calls.append(request_record)
            raise
        usage = getattr(response, "usage", None)
        request_record.update(
            {
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        )
        self.calls.append(request_record)
        return response


def _make_context(llm: CapturingLLM, model: str, query_id: str, java: FrozenEvidenceJava) -> ToolContext:
    user_id = "rag-generation-evaluation"
    tenant_id = "rag-generation-evaluation"
    return ToolContext(
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=[],
            timezone="Asia/Shanghai",
            raw_access_token="evaluation-token",
        ),
        session=SessionContext(
            conversation_id=f"rag-generation-evaluation:{query_id}",
            user_id=user_id,
            tenant_id=tenant_id,
            timezone="Asia/Shanghai",
        ),
        java=java,  # type: ignore[arg-type]
        trace_id=f"rag-generation-v1:{query_id}",
        conversation_id=f"rag-generation-evaluation:{query_id}",
        llm=llm,
        model=model,
    )


async def _invoke_production_generation(
    *,
    query_id: str,
    question: str,
    evidence: list[dict[str, Any]],
    candidate_post_count: int,
    retrieval_latencies: dict[str, Any],
    llm: CapturingLLM,
    model: str,
    mode: str,
) -> dict[str, Any]:
    java = FrozenEvidenceJava(
        evidence,
        candidate_post_count=candidate_post_count,
        latencies=retrieval_latencies,
    )
    call_start = len(llm.calls)
    started = time.perf_counter()
    result: ToolResult[Any] | None = None
    error: str | None = None
    try:
        result = await community.answer_from_knowledge(
            _make_context(llm, model, f"{query_id}:{mode}", java),
            question,
            top_posts=CANONICAL_TOP_POSTS,
            top_chunks=CANONICAL_TOP_CHUNKS,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary for live provider failures
        error = f"{type(exc).__name__}: {_text(exc)[:500]}"
    calls = llm.calls[call_start:]
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    answer = _text(data.get("answer"))
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    state = result.state if result is not None and isinstance(result.state, dict) else {}
    return {
        "query_id": query_id,
        "mode": mode,
        "question_sha256": _sha256_text(question),
        "input_evidence_ids": [_text(item.get("chunkId")) for item in evidence],
        "input_evidence_count": len(evidence),
        "input_evidence_content_chars": sum(len(_text(item.get("content"))) for item in evidence),
        "candidate_post_count": candidate_post_count,
        "retrieval_latencies_ms": retrieval_latencies,
        "tool_ok": bool(result.ok) if result is not None else False,
        "tool_code": result.code if result is not None else "EVALUATION_EXCEPTION",
        "tool_error": error or (_text(result.message)[:500] if result is not None and not result.ok else None),
        "answer": answer,
        "sources": sources,
        "state": state,
        "wall_latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "llm_calls": calls,
        "llm_call_count": len(calls),
        "generation_latency_ms": round(
            sum(float(call.get("latency_ms") or 0) for call in calls),
            3,
        ),
        "prompt_tokens": (
            sum(int(call["prompt_tokens"]) for call in calls if call.get("prompt_tokens") is not None)
            or None
        ),
        "completion_tokens": (
            sum(int(call["completion_tokens"]) for call in calls if call.get("completion_tokens") is not None)
            or None
        ),
        "context_tokens_estimate": round(
            sum(len(_text(item.get("content")).encode("utf-8")) for item in evidence) / 4,
            3,
        ),
        "java_call": java.calls[0] if java.calls else None,
        "production_prompt_sha256": _sha256_text(community._GROUNDED_ANSWER_PROMPT),
    }


def _response_metrics(
    *,
    dataset_row: dict[str, Any],
    run: dict[str, Any],
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    candidate_posts: set[str],
    mode: str,
) -> dict[str, Any]:
    answer = _text(result.get("answer"))
    source_rows = result.get("sources") if isinstance(result.get("sources"), list) else []
    evidence_by_id = {_text(item.get("chunkId")): item for item in evidence}
    evidence_ids = set(evidence_by_id)
    gold_ids = {_text(item) for item in dataset_row.get("gold_chunk_ids", [])}
    gold_posts = {_text(item) for item in dataset_row.get("gold_post_ids", [])}
    exact_gold_ids = gold_ids & evidence_ids
    if not gold_ids:
        retrieval_stratum = "NO_REQUIRED_EVIDENCE"
    elif exact_gold_ids == gold_ids:
        retrieval_stratum = "GOLD_EVIDENCE_RETRIEVED"
    elif exact_gold_ids:
        retrieval_stratum = "PARTIAL_EVIDENCE_RETRIEVED"
    else:
        retrieval_stratum = "REQUIRED_EVIDENCE_MISSING"

    gold_answer = _text(dataset_row.get("gold_answer"))
    gold_terms = _terms(gold_answer)
    answer_terms = _terms(answer)
    answer_is_insufficient = answer == community._INSUFFICIENT_EVIDENCE
    overlap = gold_terms & answer_terms
    gold_term_recall = _rate(len(overlap), len(gold_terms))
    answer_term_precision = _rate(len(overlap), len(answer_terms))
    correctness_f1 = (
        round(2 * gold_term_recall * answer_term_precision / (gold_term_recall + answer_term_precision), 6)
        if gold_term_recall + answer_term_precision
        else 0.0
    )
    gold_claims = [
        _text(item.get("claim")) if isinstance(item, dict) else _text(item)
        for item in dataset_row.get("evidence_claims", [])
    ]
    gold_claim_coverage_values: list[float] = []
    for claim in gold_claims or [gold_answer]:
        claim_terms = _terms(claim)
        gold_claim_coverage_values.append(_rate(len(claim_terms & answer_terms), len(claim_terms)))
    gold_claim_coverage = _mean(gold_claim_coverage_values) if gold_claim_coverage_values else 0.0
    evidence_terms: set[str] = set()
    for item in evidence:
        evidence_terms |= _terms(item.get("content"))
    claims = [] if answer_is_insufficient else _split_claims(answer)
    claim_support: list[dict[str, Any]] = []
    for claim in claims:
        claim_terms = _terms(claim)
        shared = claim_terms & evidence_terms
        ratio = _rate(len(shared), len(claim_terms))
        supported = bool(shared) and (
            len(shared) >= MIN_SHARED_SUPPORT_TERMS or ratio >= SUPPORT_TERM_RECALL_THRESHOLD
        )
        claim_support.append(
            {
                "claim": claim[:240],
                "term_count": len(claim_terms),
                "shared_evidence_terms": len(shared),
                "evidence_term_recall": ratio,
                "supported": supported,
            }
        )
    supported_claims = sum(1 for item in claim_support if item["supported"])
    factual_claim_count = len(claim_support)
    # A safe refusal contains no factual claim.  It is a completeness failure
    # for an answerable query, not an unsupported claim or hallucination.
    faithfulness = _rate(supported_claims, factual_claim_count) if factual_claim_count else 1.0
    hallucination_rate = round(1 - faithfulness, 6) if factual_claim_count else 0.0

    source_ids = [_text(item.get("chunkId") or item.get("chunk_id")) for item in source_rows if isinstance(item, dict)]
    unique_source_ids = set(source_ids)
    valid_source_ids = [source_id for source_id in source_ids if source_id in evidence_by_id]
    canonical_source_ids = [
        source_id
        for source_id, source in zip(source_ids, source_rows, strict=False)
        if isinstance(source, dict)
        and source_id in evidence_by_id
        and _text(source.get("postId") or source.get("post_id")) == _text(evidence_by_id[source_id].get("postId"))
        and _text(source.get("title")) == _text(evidence_by_id[source_id].get("title"))
    ]
    if answer_is_insufficient:
        citation_correctness = 1.0 if not source_ids else 0.0
    elif source_ids:
        citation_correctness = _rate(len(canonical_source_ids), len(source_ids))
    else:
        citation_correctness = 0.0
    cited_terms: set[str] = set()
    for source_id in unique_source_ids & evidence_ids:
        cited_terms |= _terms(evidence_by_id[source_id].get("content"))
    cited_supported_claims = 0
    for claim in claims:
        claim_terms = _terms(claim)
        shared = claim_terms & cited_terms
        if shared and (len(shared) >= MIN_SHARED_SUPPORT_TERMS or _rate(len(shared), len(claim_terms)) >= SUPPORT_TERM_RECALL_THRESHOLD):
            cited_supported_claims += 1
    citation_completeness = _rate(cited_supported_claims, factual_claim_count) if factual_claim_count else 1.0
    no_answer_correct = None
    if not gold_ids:
        no_answer_correct = float(answer_is_insufficient and not source_ids)

    if not result.get("tool_ok"):
        first_bad_state = "GENERATION_FAITHFULNESS_FAILURE" if mode != "no_answer_empty_control" else "NO_ANSWER_FAILURE"
        failure_family = first_bad_state
    elif not gold_ids:
        first_bad_state = None if no_answer_correct else "NO_ANSWER_FAILURE"
        failure_family = first_bad_state
    elif not gold_posts & candidate_posts:
        first_bad_state = "POST_RETRIEVAL_FAILURE"
        failure_family = first_bad_state
    elif not gold_ids <= evidence_ids:
        first_bad_state = "CHUNK_RETRIEVAL_FAILURE"
        failure_family = first_bad_state
    elif result.get("context_integrity_failure"):
        first_bad_state = "CONTEXT_CONSTRUCTION_FAILURE"
        failure_family = first_bad_state
    elif answer_is_insufficient:
        first_bad_state = "GENERATION_COMPLETENESS_FAILURE"
        failure_family = first_bad_state
    elif faithfulness < FAITHFULNESS_FAILURE_THRESHOLD:
        first_bad_state = "GENERATION_FAITHFULNESS_FAILURE"
        failure_family = first_bad_state
    elif gold_term_recall < COMPLETENESS_FAILURE_THRESHOLD or correctness_f1 < CORRECTNESS_FAILURE_THRESHOLD:
        first_bad_state = "GENERATION_COMPLETENESS_FAILURE"
        failure_family = first_bad_state
    elif citation_correctness < 1.0 or citation_completeness < CITATION_COMPLETENESS_FAILURE_THRESHOLD:
        first_bad_state = "CITATION_FAILURE"
        failure_family = first_bad_state
    else:
        first_bad_state = None
        failure_family = None

    retrieval_latencies = result.get("retrieval_latencies_ms") or {}
    return {
        "query_id": _text(dataset_row.get("query_id")),
        "mode": mode,
        "answerable": bool(gold_ids),
        "retrieval_stratum": retrieval_stratum,
        "candidate_post_count": len(candidate_posts),
        "gold_post_count": len(gold_posts),
        "gold_chunk_count": len(gold_ids),
        "exact_gold_evidence_count": len(exact_gold_ids),
        "gold_evidence_coverage": _rate(len(exact_gold_ids), len(gold_ids)) if gold_ids else 1.0,
        "answer_is_insufficient": answer_is_insufficient,
        "answerable_refusal": bool(gold_ids and answer_is_insufficient),
        "answer_correctness_f1": correctness_f1 if gold_ids else None,
        "gold_claim_coverage": gold_claim_coverage if gold_ids else None,
        "gold_term_recall": gold_term_recall if gold_ids else None,
        "answer_term_precision": answer_term_precision if gold_ids else None,
        "answer_completeness": gold_term_recall if gold_ids else None,
        "faithfulness": faithfulness if gold_ids else None,
        "hallucination_rate": hallucination_rate if gold_ids else None,
        "factual_claim_count": factual_claim_count,
        "supported_claim_count": supported_claims,
        "citation_correctness": citation_correctness,
        "citation_completeness": citation_completeness,
        "source_count": len(source_ids),
        "valid_source_count": len(valid_source_ids),
        "canonical_source_count": len(canonical_source_ids),
        "source_ids": source_ids,
        "claim_support": claim_support,
        "no_answer_correctness": no_answer_correct,
        "first_bad_state": first_bad_state,
        "failure_family": failure_family,
        "candidate_posts_include_gold_post": bool(gold_posts & candidate_posts),
        "production_prompt_sha256": result.get("production_prompt_sha256"),
        "generation_latency_ms": result.get("generation_latency_ms"),
        "wall_latency_ms": result.get("wall_latency_ms"),
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": result.get("completion_tokens"),
        "context_tokens_estimate": result.get("context_tokens_estimate"),
        "retrieval_embedding_latency_ms": retrieval_latencies.get("embedding"),
        "retrieval_chunk_latency_ms": retrieval_latencies.get("chunk_retrieval"),
        "retrieval_total_latency_ms": retrieval_latencies.get("total"),
        "answer": _text(result.get("answer")),
        "tool_code": result.get("tool_code"),
        "tool_error": result.get("tool_error"),
        "evidence_ids": list(result.get("input_evidence_ids") or []),
        "gold_answer": gold_answer,
        "evidence_claims": dataset_row.get("evidence_claims", []),
        "run_artifact": run.get("query_id"),
    }


def _check_context_integrity(result: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    expected_ids = [_text(item.get("chunkId")) for item in evidence]
    calls = result.get("llm_calls") or []
    if not calls:
        return False
    for call in calls:
        if call.get("evidence_ids") != expected_ids:
            return True
        if call.get("evidence_count") != len(evidence):
            return True
    return False


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [float(record[key]) for record in records if record.get(key) is not None]

    return {
        "count": len(records),
        "answer_correctness": _mean(values("answer_correctness_f1")),
        "gold_claim_coverage": _mean(values("gold_claim_coverage")),
        "faithfulness": _mean(values("faithfulness")),
        "citation_correctness": _mean(values("citation_correctness")),
        "citation_completeness": _mean(values("citation_completeness")),
        "hallucination_rate": _mean(values("hallucination_rate")),
        "answer_completeness": _mean(values("answer_completeness")),
        "correctness_pass_rate": _rate(
            sum(float(record.get("answer_correctness_f1") or 0) >= CORRECTNESS_FAILURE_THRESHOLD for record in records),
            len(records),
        ),
        "faithfulness_pass_rate": _rate(
            sum(float(record.get("faithfulness") or 0) >= FAITHFULNESS_FAILURE_THRESHOLD for record in records),
            len(records),
        ),
        "completeness_pass_rate": _rate(
            sum(float(record.get("answer_completeness") or 0) >= COMPLETENESS_FAILURE_THRESHOLD for record in records),
            len(records),
        ),
        "citation_failure_rate": _rate(
            sum(
                float(record.get("citation_correctness") or 0) < 1.0
                or float(record.get("citation_completeness") or 0) < CITATION_COMPLETENESS_FAILURE_THRESHOLD
                for record in records
            ),
            len(records),
        ),
    }


def _no_answer_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    correct = sum(float(record.get("no_answer_correctness") or 0) == 1.0 for record in records)
    return {
        "count": len(records),
        "accuracy": _rate(correct, len(records)),
        "correct": correct,
        "false_answer_count": len(records) - correct,
        "sentinel": community._INSUFFICIENT_EVIDENCE,
    }


def _latency_summary(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return _distribution([float(record[key]) for record in records if record.get(key) is not None])


def _historical_retrieval_latency(runs: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, list[float]] = {"embedding": [], "chunk_retrieval": [], "total": []}
    for run in runs:
        latencies = run.get("latencies_ms") or {}
        for key in values:
            raw = latencies.get(key)
            if isinstance(raw, list):
                values[key].extend(float(item) for item in raw if item is not None)
            elif raw is not None:
                values[key].append(float(raw))
    return {
        "source": "docs/evaluation/rag_evidence_runs_20260825.jsonl",
        "used_for_evidence": False,
        "note": "Historical retrieval observations; frozen answerable evidence was not re-retrieved during this evaluation.",
        **{f"{key}_ms": _distribution(items) for key, items in values.items()},
    }


def _metric_delta(production: dict[str, Any], oracle: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "answer_correctness",
        "gold_claim_coverage",
        "faithfulness",
        "citation_correctness",
        "citation_completeness",
        "hallucination_rate",
        "answer_completeness",
    )
    result: dict[str, Any] = {}
    for key in keys:
        left = production.get(key)
        right = oracle.get(key)
        result[key] = round(float(right) - float(left), 6) if left is not None and right is not None else None
    return result


def _failure_distribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {failure: 0 for failure in FAILURE_CATEGORIES}
    for record in records:
        failure = record.get("failure_family")
        if failure:
            counts[failure] = counts.get(failure, 0) + 1
    total = len(records)
    return {
        key: {"count": count, "rate": _rate(count, total)}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    }


def _select_verdict(
    validation: dict[str, Any],
    production: dict[str, Any],
    oracle: dict[str, Any],
    no_answer: dict[str, Any],
    production_records: list[dict[str, Any]],
) -> tuple[str, str]:
    if not validation.get("valid"):
        return "BLOCKED", "Frozen input validation failed; no generation verdict is safe."
    if no_answer.get("accuracy") != 1.0:
        return "RAG_GENERATION_QUALITY_ISSUE", "The generator answered at least one no-answer query instead of refusing."
    production_missing = sum(
        record.get("retrieval_stratum") in {"PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"}
        for record in production_records
    )
    missing_rate = _rate(production_missing, len(production_records))
    correctness_delta = float(oracle.get("answer_correctness") or 0) - float(production.get("answer_correctness") or 0)
    completeness_delta = float(oracle.get("answer_completeness") or 0) - float(production.get("answer_completeness") or 0)
    if (correctness_delta >= 0.10 or completeness_delta >= 0.10) and missing_rate >= 0.20:
        return "RAG_RETRIEVAL_LIMITED", "Gold evidence materially improves the same generator while production strata show missing/partial evidence."
    if float(oracle.get("faithfulness") or 0) < FAITHFULNESS_FAILURE_THRESHOLD or float(oracle.get("answer_completeness") or 0) < COMPLETENESS_FAILURE_THRESHOLD:
        return "RAG_GENERATION_QUALITY_ISSUE", "The same generator remains weak even when gold evidence is supplied."
    if float(production.get("citation_correctness") or 0) < 1.0 or float(production.get("citation_completeness") or 0) < CITATION_COMPLETENESS_FAILURE_THRESHOLD:
        return "RAG_CITATION_QUALITY_ISSUE", "Generated answers contain citation structure or attribution failures after retrieval is available."
    return "RAG_GENERATION_V1_PASS", "No dominant retrieval, generation, no-answer, or citation failure was observed under deterministic checks."


def _case_table(records: list[dict[str, Any]], limit: int = 12) -> str:
    failures = [record for record in records if record.get("first_bad_state")]
    failures.sort(key=lambda record: (_text(record.get("first_bad_state")), _text(record.get("query_id"))))
    lines = [
        "| Query | Stratum | FIRST_BAD_STATE | Correctness | Faithfulness | Completeness | Answer |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for record in failures[:limit]:
        answer = re.sub(r"\s+", " ", _text(record.get("answer"))).strip()[:100]
        lines.append(
            f"| {record.get('query_id')} | {record.get('retrieval_stratum')} | {record.get('first_bad_state')} | "
            f"{float(record.get('answer_correctness_f1') or 0):.3f} | {float(record.get('faithfulness') or 0):.3f} | "
            f"{float(record.get('answer_completeness') or 0):.3f} | {answer.replace('|', '/') or '(empty)'} |"
        )
    return "\n".join(lines) if len(lines) > 2 else "No failed answerable cases under the configured deterministic thresholds."


def _render_report(output: dict[str, Any]) -> str:
    checkpoint = output["checkpoint"]
    chain = output["production_generation_chain"]
    dataset = output["dataset"]
    metrics = output["metrics"]
    verdict = output["verdict"]
    lines = [
        "# RAG_GENERATION_EVALUATION_V1",
        "",
        f"**Verdict:** `{verdict['value']}`  ",
        f"**Reason:** {verdict['reason']}",
        "",
        "## 1. Checkpoint and scope",
        "",
        f"- V7 checkpoint commit: `{checkpoint['v7_commit']}`.",
        f"- V7 verdict: `{checkpoint['v7_verdict']}`; production retrieval unchanged.",
        f"- Frozen snapshot: `{checkpoint['snapshot_path']}`; digest `{checkpoint['snapshot_digest']}`.",
        f"- Snapshot file SHA matches V7: `{checkpoint['snapshot_file_sha256_matches_v7']}`; drift: `{checkpoint['snapshot_drift']}`.",
        "- Evaluation is evidence-only. Java, MySQL, Qdrant, Hybrid Search, and production retrieval were not called.",
        "",
        "## 2. Real production generation chain",
        "",
        f"`{chain['entrypoint']}` uses the canonical path:",
        "",
        "`community.answer_from_knowledge` → `ctx.java.retrieve_knowledge_evidence` → evidence payload → `structured_call` → `_grounded_payload` → `_validated_sources` → response.",
        "",
        f"- Evidence is passed in returned order as user JSON `{chain['user_payload_shape']}`; the evaluator did not reorder or truncate beyond the canonical Top10 input.",
        f"- Production function/schema defaults are `top_posts={chain['production_defaults']['top_posts']}`, `top_chunks={chain['production_defaults']['top_chunks']}`. To reproduce the frozen canonical Top10 baseline, this evaluation explicitly passed `top_posts={chain['top_posts']}`, `top_chunks={chain['top_chunks']}`; max evidence in this evaluation: `{chain['max_evidence']}`.",
        f"- System prompt constant: `{chain['prompt_constant']}`, SHA256 `{chain['prompt_sha256']}`; temperature `0.0`; model `{chain['model']}`.",
        f"- Citation mapping: exact supplied `chunkId` lookup, deduplication, and canonical `postId/title`; inline claim-position citations are not generated (`{chain['inline_citation_behavior']}`).",
        f"- Insufficient evidence: empty Java evidence returns `{chain['insufficient_evidence_behavior']}` without an LLM call; malformed output or no valid sources fails closed to the same sentinel.",
        "",
        "## 3. Dataset and frozen inputs",
        "",
        f"- Dataset: `{dataset['dataset_path']}`; rows `{dataset['row_count']}`; answerable `{dataset['answerable_count']}`; no-answer `{dataset['no_answer_count']}`.",
        f"- Gold references: `{dataset['gold_chunk_reference_count']}`; unique gold chunks `{dataset['unique_gold_chunk_count']}`; annotation status remains human-audited.",
        "- Answerable production evidence: frozen Top10 `candidate_chunks[:10]` from the V1 snapshot.",
        f"- No-answer production evidence: existing `{dataset['no_answer_retrieval_artifact']}` only; it was not used to alter the frozen answerable snapshot and is called out as a historical captured input.",
        "- Oracle: exact gold chunks from Dataset V2, same production function/prompt/model; diagnostic only.",
        "",
        "## 4. Deterministic metric definitions",
        "",
        "No LLM judge was used. Correctness/completeness use normalized lexical term and claim coverage; faithfulness/hallucination use sentence-level overlap with supplied evidence; citation checks use exact IDs and canonical metadata. These are deterministic audit proxies, not semantic proof of equivalence.",
        "",
        "- Answer correctness: F1 of gold-answer terms and generated-answer terms.",
        "- Gold claim coverage: lexical term recall for each human-audited evidence claim, averaged per query.",
        "- Answer completeness: gold-answer term recall.",
        "- Faithfulness: factual answer claims supported by supplied evidence terms.",
        "- Hallucination rate: unsupported factual claims / factual claims.",
        "- Citation correctness: returned source IDs exist in evidence and post/title match the canonical evidence row.",
        "- Citation completeness: generated claims supported by the cited evidence subset.",
        "",
        "## 5. Overall answerable metrics",
        "",
        "| Run | N | Correctness | Gold claim coverage | Faithfulness | Citation correctness | Citation completeness | Hallucination | Completeness |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, value in (("PRODUCTION_EVIDENCE", metrics["production_answerable"]), ("GOLD_EVIDENCE_ORACLE", metrics["oracle_answerable"])):
        lines.append(
            f"| {label} | {value['count']} | {value['answer_correctness']:.3f} | {value['gold_claim_coverage']:.3f} | "
            f"{value['faithfulness']:.3f} | {value['citation_correctness']:.3f} | {value['citation_completeness']:.3f} | "
            f"{value['hallucination_rate']:.3f} | {value['answer_completeness']:.3f} |"
        )
    lines += [
        "",
        "## 6. Retrieval-aware generation metrics",
        "",
        "Exact gold chunk presence is intentionally conservative: equivalent paraphrase evidence is not silently counted as gold retrieved.",
        "",
        "| Evidence stratum | N | Correctness | Gold claim coverage | Faithfulness | Citation completeness | Hallucination | Completeness |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stratum, value in output["retrieval_aware_metrics"].items():
        lines.append(
            f"| {stratum} | {value['count']} | {value['answer_correctness']:.3f} | {value['gold_claim_coverage']:.3f} | "
            f"{value['faithfulness']:.3f} | {value['citation_completeness']:.3f} | {value['hallucination_rate']:.3f} | "
            f"{value['answer_completeness']:.3f} |"
        )
    delta = metrics["production_vs_oracle_delta"]
    lines += [
        "",
        "## 7. No-answer behavior",
        "",
        f"- Production no-answer accuracy: `{metrics['no_answer_production']['accuracy']:.3f}` ({metrics['no_answer_production']['correct']}/{metrics['no_answer_production']['count']}).",
        f"- Canonical sentinel: `{metrics['no_answer_production']['sentinel']}`; correct means exact sentinel and empty sources.",
        "- The five captured no-answer contexts contained retrieved chunks, so they exercised the actual generation refusal rule. Empty-context control follows the production short-circuit and is recorded separately in JSON.",
        "",
        "## 8. Production versus gold-evidence oracle",
        "",
        "Delta is `oracle - production`; positive correctness/completeness/faithfulness is an oracle improvement, while positive hallucination is worse.",
        "",
        "| Correctness Δ | Gold claim coverage Δ | Faithfulness Δ | Citation correctness Δ | Citation completeness Δ | Hallucination Δ | Completeness Δ |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {delta['answer_correctness']:.3f} | {delta['gold_claim_coverage']:.3f} | {delta['faithfulness']:.3f} | {delta['citation_correctness']:.3f} | {delta['citation_completeness']:.3f} | {delta['hallucination_rate']:.3f} | {delta['answer_completeness']:.3f} |",
        "",
        "## 9. Failure families and FIRST_BAD_STATE",
        "",
        "Failures are classified at the earliest observed boundary, preserving retrieval failures instead of charging them to generation.",
        "",
        "### FIRST_BAD_STATE distribution",
        "",
        "| FIRST_BAD_STATE / failure family | Count | Rate of 45 answerable cases |",
        "|---|---:|---:|",
    ]
    for state, value in output["failure_distribution"].items():
        lines.append(f"| {state} | {value['count']} | {value['rate']:.3f} |")
    lines += [
        "",
        "### Representative failed cases",
        "",
        _case_table(output["case_metrics"]["production_answerable"]),
        "",
        "## 10. Latency and token baseline",
        "",
        "| Run/metric | p50 | p95 | Notes |",
        "|---|---:|---:|---|",
        f"| Production generation latency (ms) | {output['latency']['production_generation_ms']['p50']} | {output['latency']['production_generation_ms']['p95']} | live provider calls through production function |",
        f"| Oracle generation latency (ms) | {output['latency']['oracle_generation_ms']['p50']} | {output['latency']['oracle_generation_ms']['p95']} | same generator, synthetic gold evidence |",
        f"| Production prompt tokens | {output['latency']['production_prompt_tokens']['p50']} | {output['latency']['production_prompt_tokens']['p95']} | provider usage when returned |",
        f"| Production output tokens | {output['latency']['production_completion_tokens']['p50']} | {output['latency']['production_completion_tokens']['p95']} | provider usage when returned |",
        f"| Evidence context token estimate | {output['latency']['production_context_tokens_estimate']['p50']} | {output['latency']['production_context_tokens_estimate']['p95']} | UTF-8 bytes / 4 estimate |",
        f"| Historical retrieval total (ms) | {output['latency']['historical_retrieval']['total_ms']['p50']} | {output['latency']['historical_retrieval']['total_ms']['p95']} | captured artifact; not rerun or used for evidence |",
        f"| Estimated production total RAG (ms) | {output['latency']['estimated_production_total_ms']['p50']} | {output['latency']['estimated_production_total_ms']['p95']} | historical retrieval total + generation latency |",
        "",
        "## 11. Diagnosis",
        "",
        "- Exact FIRST_BAD_STATE for the canonical retrieval path remains `POST_RETRIEVAL → CHUNK_RETRIEVAL` when required gold evidence is absent.",
        "- Generation-limited cases are those with `GOLD_EVIDENCE_RETRIEVED` where deterministic correctness/faithfulness/completeness/citation thresholds still fail; their distribution is in the JSON artifact.",
        f"- Oracle result: `{output['oracle_interpretation']}`.",
        "- No ranking, reranking, chunking, embedding, prompt, generator, or production implementation change was made.",
        "",
        "## 12. Files and next recommendation",
        "",
        "- Production files changed: `0`.",
        f"- Evaluation files: `{output['files']['evaluation_script']}`, `{output['files']['results']}`, `{output['files']['report']}`.",
        f"- Dirty files at evaluation completion: `{', '.join(output['files']['dirty_files']) or '(none)'}`.",
        f"- Recommendation: {output['next_recommendation']}",
        "",
        "## Reproducibility notes",
        "",
        "- The live provider is nondeterministic across time even with temperature 0; model, prompt hash, input evidence IDs, request counts, usage, and errors are recorded per case.",
        "- No prompt/output was sent to a separate judge. Detailed case records contain answers and hashes, but no API credentials or full prompts.",
    ]
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(REPO_ROOT / ".env")
    dataset_path = args.dataset
    snapshot_path = args.snapshot
    runs_path = args.runs
    v7_path = args.v7_results
    dataset_rows = _load_jsonl(dataset_path)
    snapshot = _load_json(snapshot_path)
    retrieval_runs = _load_jsonl(runs_path)
    v7_results = _load_json(v7_path)
    validation, dataset_by_id, snapshot_by_id, auxiliary = _validate_inputs(
        dataset_rows,
        snapshot,
        retrieval_runs,
        v7_results,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        runs_path=runs_path,
    )
    catalog_by_id = auxiliary.pop("__catalog__")
    if not validation["valid"]:
        raise RuntimeError("Input validation failed: " + "; ".join(validation["errors"]))

    model = args.model or os.getenv("LLM_MODEL") or "deepseek-v4-flash"
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY or OPENAI_API_KEY is required for generation evaluation")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout)
    llm = CapturingLLM(client)
    answerable_rows = [row for row in dataset_rows if row.get("gold_chunk_ids")]
    no_answer_rows = [row for row in dataset_rows if not row.get("gold_chunk_ids")]
    production_results: list[dict[str, Any]] = []
    oracle_results: list[dict[str, Any]] = []
    production_metrics: list[dict[str, Any]] = []
    oracle_metrics: list[dict[str, Any]] = []
    no_answer_metrics: list[dict[str, Any]] = []
    empty_control_metrics: list[dict[str, Any]] = []

    try:
        for index, row in enumerate(answerable_rows, start=1):
            query_id = _text(row["query_id"])
            snapshot_row = snapshot_by_id[query_id]
            production_evidence = [
                _catalog_evidence(item, catalog_by_id)
                for item in snapshot_row.get("candidate_chunks", [])[:PRODUCTION_MAX_EVIDENCE]
            ]
            retrieval_run = {"query_id": query_id}
            candidate_posts = {
                _text(item.get("post_id")) for item in snapshot_row.get("candidate_posts", [])
            }
            production = await _invoke_production_generation(
                query_id=query_id,
                question=_text(row.get("question") or row.get("query")),
                evidence=production_evidence,
                candidate_post_count=len(candidate_posts),
                retrieval_latencies={"embedding": None, "chunk_retrieval": None, "total": None},
                llm=llm,
                model=model,
                mode="production_evidence",
            )
            production["context_integrity_failure"] = _check_context_integrity(production, production_evidence)
            production_results.append(production)
            production_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run=retrieval_run,
                    result=production,
                    evidence=production_evidence,
                    candidate_posts=candidate_posts,
                    mode="production_evidence",
                )
            )

            oracle_evidence = [
                _catalog_evidence(
                    {"chunk_id": chunk_id},
                    catalog_by_id,
                    default_score=1.0,
                )
                for chunk_id in row.get("gold_chunk_ids", [])
            ]
            oracle = await _invoke_production_generation(
                query_id=query_id,
                question=_text(row.get("question") or row.get("query")),
                evidence=oracle_evidence,
                candidate_post_count=len(row.get("gold_post_ids", [])),
                retrieval_latencies={"embedding": 0, "chunk_retrieval": 0, "total": 0},
                llm=llm,
                model=model,
                mode="gold_evidence_oracle",
            )
            oracle["context_integrity_failure"] = _check_context_integrity(oracle, oracle_evidence)
            oracle_results.append(oracle)
            oracle_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run={"query_id": query_id},
                    result=oracle,
                    evidence=oracle_evidence,
                    candidate_posts=set(row.get("gold_post_ids", [])),
                    mode="gold_evidence_oracle",
                )
            )
            print(f"[{index:02d}/{len(answerable_rows)}] {query_id} production+oracle", flush=True)

        for index, row in enumerate(no_answer_rows, start=1):
            query_id = _text(row["query_id"])
            retrieval_run = auxiliary.get(query_id) or {}
            production_evidence = [
                _catalog_evidence(item, catalog_by_id)
                for item in retrieval_run.get("evidence", [])[:PRODUCTION_MAX_EVIDENCE]
            ]
            production = await _invoke_production_generation(
                query_id=query_id,
                question=_text(row.get("question") or row.get("query")),
                evidence=production_evidence,
                candidate_post_count=int(retrieval_run.get("candidate_post_count") or 0),
                retrieval_latencies={
                    "embedding": (retrieval_run.get("latencies_ms") or {}).get("embedding", [None])[0],
                    "chunk_retrieval": (retrieval_run.get("latencies_ms") or {}).get("chunk_retrieval", [None])[0],
                    "total": (retrieval_run.get("latencies_ms") or {}).get("total", [None])[0],
                },
                llm=llm,
                model=model,
                mode="no_answer_production_evidence",
            )
            production["context_integrity_failure"] = _check_context_integrity(production, production_evidence)
            production_results.append(production)
            no_answer_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run=retrieval_run,
                    result=production,
                    evidence=production_evidence,
                    candidate_posts={_text(item.get("post_id")) for item in retrieval_run.get("evidence", [])},
                    mode="no_answer_production_evidence",
                )
            )

            empty_control = await _invoke_production_generation(
                query_id=query_id,
                question=_text(row.get("question") or row.get("query")),
                evidence=[],
                candidate_post_count=0,
                retrieval_latencies={"embedding": 0, "chunk_retrieval": 0, "total": 0},
                llm=llm,
                model=model,
                mode="no_answer_empty_control",
            )
            empty_control["context_integrity_failure"] = False
            empty_control_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run={"query_id": query_id},
                    result=empty_control,
                    evidence=[],
                    candidate_posts=set(),
                    mode="no_answer_empty_control",
                )
            )
            print(f"[{index:02d}/{len(no_answer_rows)}] {query_id} no-answer", flush=True)
    finally:
        await client.close()

    production_answerable = _aggregate(production_metrics)
    oracle_answerable = _aggregate(oracle_metrics)
    no_answer = _no_answer_metrics(no_answer_metrics)
    empty_control = _no_answer_metrics(empty_control_metrics)
    retrieval_aware: dict[str, Any] = {}
    for stratum in (
        "GOLD_EVIDENCE_RETRIEVED",
        "PARTIAL_EVIDENCE_RETRIEVED",
        "REQUIRED_EVIDENCE_MISSING",
    ):
        retrieval_aware[stratum] = _aggregate(
            [record for record in production_metrics if record.get("retrieval_stratum") == stratum]
        )
    production_missing = sum(
        record.get("retrieval_stratum") in {"PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"}
        for record in production_metrics
    )
    verdict_value, verdict_reason = _select_verdict(
        validation,
        production_answerable,
        oracle_answerable,
        no_answer,
        production_metrics,
    )
    historical_retrieval = _historical_retrieval_latency(retrieval_runs)
    run_latency_by_id = {
        _text(run.get("query_id")): float((run.get("latencies_ms") or {}).get("total", [0])[0] or 0)
        for run in retrieval_runs
    }
    estimated_totals = [
        run_latency_by_id.get(_text(record.get("query_id")), 0) + float(record.get("generation_latency_ms") or 0)
        for record in production_metrics
    ]
    production_gen_latency_records = [record for record in production_metrics if record.get("mode") == "production_evidence"]
    oracle_gen_latency_records = oracle_metrics
    production_prompt_tokens = [float(record["prompt_tokens"]) for record in production_results if record.get("prompt_tokens") is not None and record.get("mode") == "production_evidence"]
    production_completion_tokens = [float(record["completion_tokens"]) for record in production_results if record.get("completion_tokens") is not None and record.get("mode") == "production_evidence"]
    production_context_tokens = [float(record["context_tokens_estimate"]) for record in production_results if record.get("context_tokens_estimate") is not None and record.get("mode") == "production_evidence"]
    latency = {
        "production_generation_ms": _latency_summary(production_gen_latency_records, "generation_latency_ms"),
        "oracle_generation_ms": _latency_summary(oracle_gen_latency_records, "generation_latency_ms"),
        "production_prompt_tokens": _distribution(production_prompt_tokens),
        "production_completion_tokens": _distribution(production_completion_tokens),
        "production_context_tokens_estimate": _distribution(production_context_tokens),
        "historical_retrieval": historical_retrieval,
        "estimated_production_total_ms": _distribution(estimated_totals),
    }
    if oracle_answerable["answer_correctness"] is not None and production_answerable["answer_correctness"] is not None:
        oracle_interpretation = (
            "oracle is materially better than production; generation quality should be interpreted as retrieval-limited first"
            if float(oracle_answerable["answer_correctness"]) - float(production_answerable["answer_correctness"]) >= 0.10
            else "oracle does not materially exceed production on the deterministic correctness proxy"
        )
    else:
        oracle_interpretation = "oracle comparison unavailable"

    output: dict[str, Any] = {
        "evaluation": "RAG_GENERATION_EVALUATION_V1",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checkpoint": {
            "v7_commit": V7_CHECKPOINT_COMMIT,
            "current_head": _git_short_head(),
            "v7_verdict": v7_results.get("verdict"),
            "v7_first_bad_state": v7_results.get("first_bad_state"),
            "snapshot_path": str(snapshot_path.relative_to(REPO_ROOT)),
            "snapshot_digest": snapshot.get("snapshot_digest"),
            "snapshot_file_sha256_matches_v7": validation["snapshot_file_sha256_matches_v7"],
            "snapshot_drift": validation["snapshot_drift"],
        },
        "validation": validation,
        "dataset": {
            "dataset_path": str(dataset_path.relative_to(REPO_ROOT)),
            "dataset_file_sha256": validation["dataset_file_sha256"],
            "row_count": len(dataset_rows),
            "answerable_count": len(answerable_rows),
            "no_answer_count": len(no_answer_rows),
            "gold_chunk_reference_count": sum(len(row.get("gold_chunk_ids", [])) for row in dataset_rows),
            "unique_gold_chunk_count": len({chunk_id for row in dataset_rows for chunk_id in row.get("gold_chunk_ids", [])}),
            "no_answer_retrieval_artifact": str(runs_path.relative_to(REPO_ROOT)),
        },
        "production_generation_chain": {
            "entrypoint": "community.answer_from_knowledge",
            "evidence_retrieval": "ctx.java.retrieve_knowledge_evidence (replaced by frozen Java boundary double in this evaluation)",
            "user_payload_shape": "{question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}",
            "top_posts": CANONICAL_TOP_POSTS,
            "top_chunks": CANONICAL_TOP_CHUNKS,
            "max_evidence": PRODUCTION_MAX_EVIDENCE,
            "production_defaults": {"top_posts": 8, "top_chunks": 8},
            "prompt_constant": "community._GROUNDED_ANSWER_PROMPT",
            "prompt_sha256": _sha256_text(community._GROUNDED_ANSWER_PROMPT),
            "model": model,
            "structured_call": "greenbook_agent_core.llm_compat.structured_call",
            "citation_rewrite": "community._validated_sources",
            "inline_citation_behavior": "global sources array only; no claim-position inline marker",
            "insufficient_evidence_behavior": "exact community._INSUFFICIENT_EVIDENCE and empty sources",
        },
        "metrics": {
            "production_answerable": production_answerable,
            "oracle_answerable": oracle_answerable,
            "no_answer_production": no_answer,
            "no_answer_empty_context_control": empty_control,
            "production_vs_oracle_delta": _metric_delta(production_answerable, oracle_answerable),
            "retrieval_limited_case_count": production_missing,
            "retrieval_limited_case_rate": _rate(production_missing, len(production_metrics)),
        },
        "retrieval_aware_metrics": retrieval_aware,
        "failure_distribution": _failure_distribution(production_metrics),
        "oracle_failure_distribution": _failure_distribution(oracle_metrics),
        "case_metrics": {
            "production_answerable": production_metrics,
            "oracle_answerable": oracle_metrics,
            "no_answer_production": no_answer_metrics,
            "no_answer_empty_control": empty_control_metrics,
        },
        "raw_generation_runs": {
            "production": production_results,
            "oracle": oracle_results,
        },
        "latency": latency,
        "verdict": {"value": verdict_value, "reason": verdict_reason},
        "oracle_interpretation": oracle_interpretation,
        "thresholds": {
            "support_term_recall_threshold": SUPPORT_TERM_RECALL_THRESHOLD,
            "minimum_shared_support_terms": MIN_SHARED_SUPPORT_TERMS,
            "correctness_failure_threshold": CORRECTNESS_FAILURE_THRESHOLD,
            "completeness_failure_threshold": COMPLETENESS_FAILURE_THRESHOLD,
            "faithfulness_failure_threshold": FAITHFULNESS_FAILURE_THRESHOLD,
            "citation_completeness_failure_threshold": CITATION_COMPLETENESS_FAILURE_THRESHOLD,
        },
        "llm_judge": {"used": False, "reason": "Deterministic checks were sufficient for this diagnostic pass."},
        "production_files_changed": [],
        "next_recommendation": (
            "Keep production retrieval and generation unchanged for this phase. Use the oracle delta and retrieval strata to decide whether the next checkpoint is retrieval-focused or a separately scoped generation-quality diagnosis; do not implement a fix from this report alone."
        ),
        "files": {
            "evaluation_script": str(Path(__file__).resolve().relative_to(REPO_ROOT)),
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
    parser.add_argument("--v7-results", type=Path, default=DEFAULT_V7_RESULTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--recompute-existing", action="store_true")
    return parser.parse_args()


def _git_dirty_files() -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    files: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip())
    return files


def _recompute_from_existing(args: argparse.Namespace) -> dict[str, Any]:
    """Recompute deterministic metrics from saved provider observations.

    This path is useful when the audit metric definition changes after a live
    run. It never calls the provider again and never changes saved answers or
    evidence IDs.
    """
    output = _load_json(args.output)
    dataset_rows = _load_jsonl(args.dataset)
    snapshot = _load_json(args.snapshot)
    retrieval_runs = _load_jsonl(args.runs)
    catalog_by_id = {
        _text(row.get("chunk_id")): row
        for row in snapshot.get("chunk_catalog", [])
        if row.get("chunk_id")
    }
    snapshot_by_id = {_text(row.get("query_id")): row for row in snapshot.get("queries", [])}
    runs_by_id = {_text(row.get("query_id")): row for row in retrieval_runs}
    raw_production = {
        (_text(row.get("query_id")), _text(row.get("mode"))): row
        for row in output.get("raw_generation_runs", {}).get("production", [])
    }
    raw_oracle = {
        _text(row.get("query_id")): row
        for row in output.get("raw_generation_runs", {}).get("oracle", [])
    }
    production_metrics: list[dict[str, Any]] = []
    oracle_metrics: list[dict[str, Any]] = []
    no_answer_metrics: list[dict[str, Any]] = []
    for row in dataset_rows:
        query_id = _text(row.get("query_id"))
        if row.get("gold_chunk_ids"):
            snapshot_row = snapshot_by_id[query_id]
            production_evidence = [
                _catalog_evidence(item, catalog_by_id)
                for item in snapshot_row.get("candidate_chunks", [])[:PRODUCTION_MAX_EVIDENCE]
            ]
            candidate_posts = {_text(item.get("post_id")) for item in snapshot_row.get("candidate_posts", [])}
            production_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run={"query_id": query_id},
                    result=raw_production[(query_id, "production_evidence")],
                    evidence=production_evidence,
                    candidate_posts=candidate_posts,
                    mode="production_evidence",
                )
            )
            oracle_evidence = [
                _catalog_evidence({"chunk_id": chunk_id}, catalog_by_id)
                for chunk_id in row.get("gold_chunk_ids", [])
            ]
            oracle_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run={"query_id": query_id},
                    result=raw_oracle[query_id],
                    evidence=oracle_evidence,
                    candidate_posts=set(row.get("gold_post_ids", [])),
                    mode="gold_evidence_oracle",
                )
            )
        else:
            retrieval_run = runs_by_id[query_id]
            production_evidence = [
                _catalog_evidence(item, catalog_by_id)
                for item in retrieval_run.get("evidence", [])[:PRODUCTION_MAX_EVIDENCE]
            ]
            no_answer_metrics.append(
                _response_metrics(
                    dataset_row=row,
                    run=retrieval_run,
                    result=raw_production[(query_id, "no_answer_production_evidence")],
                    evidence=production_evidence,
                    candidate_posts={_text(item.get("post_id")) for item in retrieval_run.get("evidence", [])},
                    mode="no_answer_production_evidence",
                )
            )
    production_aggregate = _aggregate(production_metrics)
    oracle_aggregate = _aggregate(oracle_metrics)
    no_answer = _no_answer_metrics(no_answer_metrics)
    existing_empty = output.get("case_metrics", {}).get("no_answer_empty_control", [])
    empty_control = _no_answer_metrics(existing_empty)
    retrieval_aware = {
        stratum: _aggregate([record for record in production_metrics if record.get("retrieval_stratum") == stratum])
        for stratum in (
            "GOLD_EVIDENCE_RETRIEVED",
            "PARTIAL_EVIDENCE_RETRIEVED",
            "REQUIRED_EVIDENCE_MISSING",
        )
    }
    retrieval_limited_count = sum(
        record.get("retrieval_stratum") in {"PARTIAL_EVIDENCE_RETRIEVED", "REQUIRED_EVIDENCE_MISSING"}
        for record in production_metrics
    )
    verdict_value, verdict_reason = _select_verdict(
        output.get("validation", {}),
        production_aggregate,
        oracle_aggregate,
        no_answer,
        production_metrics,
    )
    output["metrics"] = {
        "production_answerable": production_aggregate,
        "oracle_answerable": oracle_aggregate,
        "no_answer_production": no_answer,
        "no_answer_empty_context_control": empty_control,
        "production_vs_oracle_delta": _metric_delta(production_aggregate, oracle_aggregate),
        "retrieval_limited_case_count": retrieval_limited_count,
        "retrieval_limited_case_rate": _rate(retrieval_limited_count, len(production_metrics)),
    }
    output["retrieval_aware_metrics"] = retrieval_aware
    output["failure_distribution"] = _failure_distribution(production_metrics)
    output["oracle_failure_distribution"] = _failure_distribution(oracle_metrics)
    output["case_metrics"] = {
        "production_answerable": production_metrics,
        "oracle_answerable": oracle_metrics,
        "no_answer_production": no_answer_metrics,
        "no_answer_empty_control": existing_empty,
    }
    output["verdict"] = {"value": verdict_value, "reason": verdict_reason}
    output["production_generation_chain"]["production_defaults"] = {"top_posts": 8, "top_chunks": 8}
    output["files"]["dirty_files"] = []
    return output


def main() -> None:
    args = _parse_args()
    args.dataset = args.dataset.resolve()
    args.snapshot = args.snapshot.resolve()
    args.runs = args.runs.resolve()
    args.v7_results = args.v7_results.resolve()
    args.output = args.output.resolve()
    args.report = args.report.resolve()
    output = _recompute_from_existing(args) if args.recompute_existing else asyncio.run(_run(args))
    _write_json(args.output, output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(output), encoding="utf-8")
    output["files"]["dirty_files"] = _git_dirty_files()
    _write_json(args.output, output)
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(json.dumps({"verdict": output["verdict"], "output": str(args.output), "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
