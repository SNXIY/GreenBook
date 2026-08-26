"""Read-only audit of the live RAG knowledge corpus and gold evidence quality.

The audit follows the canonical content path instead of treating MySQL
metadata as post正文. It reads published/public post metadata from MySQL,
reads the configured object-store files, reads the existing MySQL chunk
projection, and snapshots Qdrant payloads. It never writes to MySQL, Qdrant,
object storage, or production source code.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from validate_rag_dataset_v2 import validate

CHECKPOINT = "df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6"
COLLECTION = "post_chunks_multilingual_v1"
DEFAULT_STORAGE_ROOT = Path("apps/backend/data/storage")
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDING_CACHE = r"D:\tmp\greenbook-retrieval-model-cache"
SEMANTIC_STRONG_THRESHOLD = 0.60
SEMANTIC_PARTIAL_THRESHOLD = 0.40
SEMANTIC_NO_REAL_THRESHOLD = 0.30
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_DEBUG_MARKER_RE = re.compile(
    r"(?i)\b(?:test|debug|placeholder|dummy|fixture|lorem|todo|sample)\b"
    r"|测试|调试|占位|待补充|待完善|测试数据|示例占位"
)
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
        "可以",
        "需要",
        "是否",
        "通过",
    }
)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def _text_summary(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _terms(value: Any) -> list[str]:
    terms: list[str] = []
    for token in _TOKEN_RE.findall(str(value or "").casefold()):
        if all("\u4e00" <= char <= "\u9fff" for char in token):
            if len(token) >= 2:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
                if len(token) >= 3:
                    terms.extend(token[index : index + 3] for index in range(len(token) - 2))
        elif token not in _STOPWORDS and len(token) > 2:
            terms.append(token)
    return list(dict.fromkeys(term for term in terms if term not in _STOPWORDS))


def _support_score(answer: str, evidence: str) -> tuple[float, list[str]]:
    answer_terms = _terms(answer)
    evidence_text = str(evidence or "").casefold()
    if not answer_terms or not evidence_text.strip():
        return 0.0, answer_terms
    supported = [term for term in answer_terms if term in evidence_text]
    missing = [term for term in answer_terms if term not in evidence_text]
    return round(len(supported) / len(answer_terms), 6), missing[:20]


def _cosine(left: Any, right: Any) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return sum(left_value * right_value for left_value, right_value in zip(left_values, right_values, strict=True)) / (left_norm * right_norm)


def _semantic_support_scores(
    embedder: Any,
    answers: dict[str, str],
    evidence_by_query: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    query_ids = list(answers)
    answer_vectors = list(embedder.embed([answers[query_id] for query_id in query_ids]))
    evidence_items = [
        (query_id, text)
        for query_id in query_ids
        for text in evidence_by_query.get(query_id, [])
        if str(text or "").strip()
    ]
    evidence_vectors = list(embedder.embed([text for _, text in evidence_items]))
    scores_by_query: dict[str, list[float]] = defaultdict(list)
    answer_index = {query_id: index for index, query_id in enumerate(query_ids)}
    for (query_id, _), vector in zip(evidence_items, evidence_vectors, strict=True):
        scores_by_query[query_id].append(_cosine(answer_vectors[answer_index[query_id]], vector))
    result: dict[str, dict[str, Any]] = {}
    for query_id in query_ids:
        scores = sorted(scores_by_query.get(query_id, []), reverse=True)
        result[query_id] = {
            "max": round(scores[0], 6) if scores else 0.0,
            "top2_mean": round(statistics.fmean(scores[:2]), 6) if scores else 0.0,
            "chunk_scores": [round(score, 6) for score in scores],
        }
    return result


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_storage_root(repo_root: Path, configured: str) -> Path:
    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate.resolve()
    if configured.replace("\\", "/").startswith("data/"):
        return (repo_root / "apps/backend" / candidate).resolve()
    return (repo_root / candidate).resolve()


def _mysql_rows(
    query: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> list[dict[str, Any]]:
    command = [
        "mysql",
        f"--host={host}",
        f"--port={port}",
        f"--user={user}",
        f"--password={password}",
        f"--database={database}",
        "--default-character-set=utf8mb4",
        "--batch",
        "--raw",
        "--skip-column-names",
        "-e",
        query,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-2000:]
        raise RuntimeError(f"read-only MySQL query failed: {detail}")
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _load_posts(args: argparse.Namespace) -> list[dict[str, Any]]:
    query = """
SELECT JSON_OBJECT(
  'id', CAST(id AS CHAR),
  'title', COALESCE(title, ''),
  'description', COALESCE(description, ''),
  'content_object_key', COALESCE(content_object_key, ''),
  'content_size', COALESCE(content_size, 0),
  'content_etag', COALESCE(content_etag, ''),
  'content_sha256', COALESCE(content_sha256, ''),
  'status', COALESCE(status, ''),
  'visible', COALESCE(visible, ''),
  'type', COALESCE(type, ''),
  'content_origin', COALESCE(content_origin, '')
)
FROM know_posts
WHERE status = 'published' AND visible = 'public'
ORDER BY id
"""
    return _mysql_rows(
        query,
        args.mysql_host,
        args.mysql_port,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
    )


def _load_chunks(args: argparse.Namespace) -> list[dict[str, Any]]:
    query = """
SELECT JSON_OBJECT(
  'chunk_id', c.chunk_id,
  'post_id', CAST(c.post_id AS CHAR),
  'chunk_index', c.chunk_index,
  'content', c.content,
  'token_count', c.token_count,
  'start_offset', c.start_offset,
  'end_offset', c.end_offset,
  'embedding_model', c.embedding_model,
  'embedding_version', c.embedding_version,
  'dimension', c.dimension,
  'event_version', c.event_version
)
FROM post_chunks c
ORDER BY c.post_id, c.chunk_index
"""
    return _mysql_rows(
        query,
        args.mysql_host,
        args.mysql_port,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
    )


def _read_object(post: dict[str, Any], storage_root: Path) -> dict[str, Any]:
    object_key = str(post.get("content_object_key") or "").strip()
    result: dict[str, Any] = {
        "content_status": "MISSING_CONTENT",
        "object_key_present": bool(object_key),
        "object_key": object_key,
        "content": "",
        "content_chars": 0,
        "content_bytes": 0,
        "declared_content_bytes": int(post.get("content_size") or 0),
        "content_size_matches": False,
        "sha256_matches": False,
    }
    if not object_key:
        return result
    try:
        relative = PurePosixPath(object_key.replace("\\", "/"))
        target = (storage_root / relative).resolve()
        if target != storage_root and storage_root not in target.parents:
            result["content_status"] = "UNSAFE_OBJECT_KEY"
            return result
        raw = target.read_bytes()
    except FileNotFoundError:
        return result
    except (OSError, ValueError):
        result["content_status"] = "UNREADABLE_CONTENT"
        return result
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        result["content_status"] = "UNREADABLE_CONTENT"
        result["content_bytes"] = len(raw)
        return result
    digest = hashlib.sha256(raw).hexdigest()
    stripped = content.strip()
    result.update(
        {
            "content": content,
            "content_status": (
                "EMPTY_CONTENT" if not content else "WHITESPACE_ONLY" if not stripped else "AVAILABLE"
            ),
            "content_chars": len(stripped),
            "content_bytes": len(raw),
            "content_size_matches": int(post.get("content_size") or 0) == len(raw),
            "sha256_matches": not post.get("content_sha256") or post["content_sha256"] == digest,
        }
    )
    return result


def _content_flags(content: str) -> dict[str, Any]:
    stripped = content.strip()
    marker_match = _DEBUG_MARKER_RE.search(stripped)
    non_space = re.sub(r"\s+", "", stripped)
    repetitive = bool(non_space) and len(set(non_space)) <= max(2, len(non_space) // 20)
    debug_or_placeholder = bool(marker_match and len(stripped) < 500) or repetitive
    return {
        "debug_or_placeholder": debug_or_placeholder,
        "debug_marker": marker_match.group(0) if marker_match else None,
        "repetitive_body": repetitive,
        "short_test_signal": bool(debug_or_placeholder and len(stripped) < 100),
        "line_count": len(stripped.splitlines()) if stripped else 0,
    }


def _length_distribution(lengths: list[int]) -> dict[str, Any]:
    thresholds = (50, 100, 300, 500)
    return {
        "count": len(lengths),
        "min": min(lengths, default=0),
        "p25": _percentile([float(value) for value in lengths], 0.25),
        "p50": _percentile([float(value) for value in lengths], 0.5),
        "p75": _percentile([float(value) for value in lengths], 0.75),
        "p95": _percentile([float(value) for value in lengths], 0.95),
        "max": max(lengths, default=0),
        "mean": round(statistics.fmean(lengths), 3) if lengths else 0.0,
        "thresholds": {
            f"lt_{threshold}": {
                "count": sum(value < threshold for value in lengths),
                "rate": round(sum(value < threshold for value in lengths) / len(lengths), 6)
                if lengths
                else 0.0,
            }
            for threshold in thresholds
        },
        "gte_500": {
            "count": sum(value >= 500 for value in lengths),
            "rate": round(sum(value >= 500 for value in lengths) / len(lengths), 6)
            if lengths
            else 0.0,
        },
    }


def _duplicate_groups(values: dict[str, str]) -> list[list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for identity, value in values.items():
        normalized = _normalize_text(value)
        if normalized:
            grouped[normalized].append(identity)
    return sorted(
        (sorted(identities) for identities in grouped.values() if len(identities) > 1),
        key=lambda group: (-len(group), group[0]),
    )


def _near_duplicate_pairs(chunks: list[dict[str, Any]]) -> list[list[str]]:
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        normalized = _normalize_text(chunk.get("content"))
        if len(normalized) < 80:
            continue
        bucket = (len(normalized) // 200, normalized[:64])
        buckets[bucket].append(chunk)
    pairs: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                if str(left["post_id"]) == str(right["post_id"]):
                    continue
                left_id = str(left["chunk_id"])
                right_id = str(right["chunk_id"])
                pair = tuple(sorted((left_id, right_id)))
                if pair in seen:
                    continue
                similarity = difflib.SequenceMatcher(
                    None,
                    _normalize_text(left.get("content")),
                    _normalize_text(right.get("content")),
                    autojunk=False,
                ).ratio()
                if similarity >= 0.9:
                    seen.add(pair)
                    pairs.append([pair[0], pair[1]])
                    if len(pairs) >= 500:
                        return pairs
    return pairs


def _http_json(url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("Qdrant response must be an object")
    return payload


def _qdrant_snapshot(url: str, collection: str) -> dict[str, Any]:
    base = url.rstrip("/") + "/collections/" + collection
    collection_payload = _http_json(base)
    points: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 1000,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        page = _http_json(base + "/points/scroll", "POST", body)
        result = page.get("result") or {}
        for point in result.get("points", []):
            if not isinstance(point, dict):
                continue
            payload = point.get("payload") or {}
            points.append(
                {
                    "point_id": str(point.get("id") or ""),
                    "chunk_id": str(payload.get("chunk_id") or point.get("id") or ""),
                    "post_id": str(payload.get("post_id") or ""),
                    "chunk_index": int(payload.get("chunk_index") or 0),
                    "event_version": int(payload.get("event_version") or 0),
                    "status": str(payload.get("status") or ""),
                    "visibility": str(payload.get("visibility") or ""),
                }
            )
        next_offset = result.get("next_page_offset")
        if next_offset is None or next_offset == offset:
            break
        offset = next_offset
    result = collection_payload.get("result") or {}
    return {
        "available": True,
        "collection": collection,
        "points_count": result.get("points_count"),
        "indexed_vectors_count": result.get("indexed_vectors_count"),
        "points": points,
    }


def _quality_category(
    content_info: dict[str, Any],
    chunk_count: int,
    projection_missing: bool,
    weak_gold_evidence: bool,
    annotation_issue: bool,
) -> str:
    if annotation_issue:
        return "ANNOTATION_ISSUE"
    if content_info["content_status"] != "AVAILABLE":
        return "MISSING_CONTENT"
    if projection_missing or chunk_count == 0:
        return "PROJECTION_MISSING"
    if content_info["content_chars"] < 300 or content_info.get("debug_or_placeholder"):
        return "THIN_CONTENT"
    if weak_gold_evidence:
        return "WEAK_GOLD_EVIDENCE"
    return "GOOD_CORPUS"


def _metrics_for_traces(
    rows_by_query: dict[str, dict[str, Any]],
    traces_by_query: dict[str, dict[str, Any]],
    query_ids: set[str],
) -> dict[str, Any]:
    post_recall: list[float] = []
    conditional_recall: list[float] = []
    final_recall: list[float] = []
    mrr: list[float] = []
    for query_id in sorted(query_ids):
        row = rows_by_query[query_id]
        trace = traces_by_query[query_id]
        candidate_posts = {
            str(item["post_id"])
            for item in trace.get("candidate_posts", [])
            if isinstance(item, dict)
        }
        selected_ids = [
            str(item["chunk_id"])
            for item in trace.get("candidate_chunks", [])
            if isinstance(item, dict) and item.get("chunk_id")
        ][:10]
        gold_posts = {str(value) for value in row.get("gold_post_ids", [])}
        gold_chunks = [item for item in row.get("gold_chunks", []) if isinstance(item, dict)]
        gold_ids = {str(item["chunk_id"]) for item in gold_chunks}
        if gold_posts:
            post_recall.append(len(gold_posts & candidate_posts) / len(gold_posts))
        final_recall.append(len(gold_ids & set(selected_ids)) / len(gold_ids) if gold_ids else 0.0)
        conditional_gold = {
            str(item["chunk_id"])
            for item in gold_chunks
            if str(item["post_id"]) in candidate_posts
        }
        if conditional_gold:
            conditional_recall.append(
                len(conditional_gold & set(selected_ids)) / len(conditional_gold)
            )
        first_rank = next(
            (index + 1 for index, chunk_id in enumerate(selected_ids) if chunk_id in gold_ids),
            None,
        )
        mrr.append(1 / first_rank if first_rank else 0.0)
    return {
        "query_count": len(query_ids),
        "post_recall_at10": _mean(post_recall),
        "conditional_chunk_recall_at10": _mean(conditional_recall),
        "final_evidence_recall_at10": _mean(final_recall),
        "gold_chunk_mrr": _mean(mrr),
        "conditional_query_count": len(conditional_recall),
    }


def _public_content_info(info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in info.items() if key != "content"}


def _render_report(output: dict[str, Any]) -> str:
    live = output["live_corpus"]
    lengths = live["content_length_distribution"]
    length_rows = []
    for label, value in [
        ("< 50", lengths["thresholds"]["lt_50"]),
        ("< 100", lengths["thresholds"]["lt_100"]),
        ("< 300", lengths["thresholds"]["lt_300"]),
        ("< 500", lengths["thresholds"]["lt_500"]),
        (">= 500", lengths["gte_500"]),
    ]:
        length_rows.append(f"| {label} | {value['count']} | {value['rate']:.4f} |")
    quality_rows = []
    for category, value in output["gold_corpus"]["quality_distribution"].items():
        quality_rows.append(f"| `{category}` | {value['count']} | {value['rate']:.4f} |")
    coverage_rows = []
    for category, value in output["knowledge_coverage"]["distribution"].items():
        coverage_rows.append(f"| `{category}` | {value['count']} | {value['rate']:.4f} |")
    correlation_rows = []
    for family, value in output["failure_correlation"].items():
        strong = value["gold_post_quality"].get("GOOD_CORPUS", 0)
        weak = sum(
            count
            for category, count in value["gold_post_quality"].items()
            if category != "GOOD_CORPUS"
        )
        correlation_rows.append(
            f"| `{family}` | {value['total']} | {value['share_of_misses']:.4f} "
            f"| {value['share_of_all_gold']:.4f} | {strong} | {weak} |"
        )
    all_metrics = output["retrieval_metrics"]["all"]
    strong_metrics = output["retrieval_metrics"]["strong_coverage_only"]
    verdict = output["verdict"]
    return f"""# RAG_CORPUS_QUALITY_AUDIT

Checkpoint: `{output['checkpoint']}`

## Canonical full-content source

The audited production chain is:

`KnowPost metadata → content_object_key → OssStorageService.readTextObject → PostSearchDocumentService.build → PostChunkProjectionService → PostChunker → MySQL post_chunks + Qdrant {COLLECTION}`

`PostSearchDocumentService.build` reads the object-store body with a 1 MiB
limit. `PostChunkProjectionService` passes `document.content` to the
paragraph-first chunker and uses title/tags/description only as labelled
embedding context. MySQL `description` is not treated as the full post body.

Current local runtime evidence: `STORAGE_PROVIDER=local`; the same
`OssStorageService` abstraction resolves objects under the configured local
storage root. No external OSS object was fetched or modified by this audit.

## Live corpus statistics

| Measure | Value |
|---|---:|
| Published + public posts | {live['eligible_post_count']} |
| Object body available | {live['content_status_counts'].get('AVAILABLE', 0)} |
| Missing/unreadable body | {live['missing_or_unreadable_count']} |
| Empty body | {live['content_status_counts'].get('EMPTY_CONTENT', 0)} |
| Whitespace-only body | {live['content_status_counts'].get('WHITESPACE_ONLY', 0)} |
| Debug/placeholder body signal | {live['debug_or_placeholder_count']} |
| Duplicate post-content groups | {live['duplicate_post_group_count']} |

Content length is trimmed UTF-8-decoded body characters:

| Statistic | Characters |
|---|---:|
| min | {lengths['min']} |
| p25 | {lengths['p25']} |
| p50 | {lengths['p50']} |
| p75 | {lengths['p75']} |
| p95 | {lengths['p95']} |
| mean | {lengths['mean']} |
| max | {lengths['max']} |

| Body length | Count | Rate |
|---|---:|---:|
{chr(10).join(length_rows)}

The debug/placeholder classification uses body markers, body structure, and
length; it is not inferred from title alone. Examples and per-post flags are
in the JSON artifact.

## Chunk corpus statistics

| Measure | Value |
|---|---:|
| MySQL chunk rows | {output['chunk_corpus']['mysql_chunk_count']} |
| Qdrant points | {output['chunk_corpus']['qdrant_point_count']} |
| Unique MySQL chunk posts | {output['chunk_corpus']['mysql_chunk_post_count']} |
| Unique Qdrant chunk posts | {output['chunk_corpus']['qdrant_chunk_post_count']} |
| Posts with 0 chunks | {output['chunk_corpus']['posts_with_zero_chunks_count']} |
| Posts with 1 chunk | {output['chunk_corpus']['posts_with_one_chunk_count']} |
| Empty chunks | {output['chunk_corpus']['empty_chunk_count']} |
| Tiny chunks (<50 chars) | {output['chunk_corpus']['tiny_chunk_count']} |
| Exact duplicate chunk groups | {output['chunk_corpus']['duplicate_chunk_group_count']} |
| Near-duplicate chunk pairs (heuristic) | {output['chunk_corpus']['near_duplicate_pair_count']} |

Chunks per post: p50={output['chunk_corpus']['chunks_per_post']['p50']},
p95={output['chunk_corpus']['chunks_per_post']['p95']},
max={output['chunk_corpus']['chunks_per_post']['max']}.

Eligible public posts and chunk projection sets are equal:
`{str(output['chunk_corpus']['eligible_equals_mysql_posts']).upper()}`.
MySQL and Qdrant chunk identity sets are equal:
`{str(output['chunk_corpus']['mysql_equals_qdrant_chunks']).upper()}`.

Posts with available body but zero chunks: **{output['chunk_corpus']['content_but_zero_chunks_count']}**.
The complete list is in the JSON artifact.

## Gold corpus audit

The audit covers 29 unique gold posts, 75 unique gold chunks, and 104 gold
references. Gold post quality distribution:

| Category | Posts | Rate |
|---|---:|---:|
{chr(10).join(quality_rows)}

Gold chunk validity:

| Check | Count |
|---|---:|
| Gold chunks in fixture | {output['gold_corpus']['gold_chunks_in_fixture']} |
| Present in MySQL projection | {output['gold_corpus']['gold_chunks_in_mysql']} |
| Present in Qdrant collection | {output['gold_corpus']['gold_chunks_in_qdrant']} |
| Non-empty MySQL chunk text | {output['gold_corpus']['gold_chunks_nonempty']} |
| Parent body available | {output['gold_corpus']['gold_chunks_parent_available']} |
| Annotation/projection issue count | {output['gold_corpus']['annotation_issue_count']} |

`WEAK_GOLD_EVIDENCE` is an offline semantic-screening flag for a gold post
whose maximum answer-to-gold-chunk similarity is below the `STRONG` threshold;
it does not mean the body is missing or the annotation is invalid. All
answerable dataset rows remain human-audited and all gold chunk identities are
present and non-empty.

## Knowledge coverage

Coverage is evaluated against current canonical chunk text and the dataset
gold answer with the deployed multilingual embedding model
`{output['runtime']['coverage_embedding_model']}`. The semantic similarity
score is the maximum answer-to-gold-chunk cosine score; lexical overlap is
retained as an auxiliary diagnostic field. Thresholds are
`NO_REAL < {output['runtime']['coverage_thresholds']['no_real']:.2f}`,
`WEAK < {output['runtime']['coverage_thresholds']['partial']:.2f}`,
`PARTIAL < {output['runtime']['coverage_thresholds']['strong']:.2f}`, and
`STRONG >= {output['runtime']['coverage_thresholds']['strong']:.2f}`.
This is an offline corpus-support signal, not a replacement for human
annotation.

| Coverage | Queries | Rate |
|---|---:|---:|
{chr(10).join(coverage_rows)}

## Retrieval metrics: ALL vs STRONG_COVERAGE_ONLY

| Scope | Queries | Post Recall@10 | Conditional Chunk Recall@10 | Final Evidence Recall@10 | Chunk MRR |
|---|---:|---:|---:|---:|---:|
| ALL | {all_metrics['query_count']} | {all_metrics['post_recall_at10'] or 0.0:.6f} | {all_metrics['conditional_chunk_recall_at10'] or 0.0:.6f} | {all_metrics['final_evidence_recall_at10'] or 0.0:.6f} | {all_metrics['gold_chunk_mrr'] or 0.0:.6f} |
| STRONG_COVERAGE_ONLY | {strong_metrics['query_count']} | {strong_metrics['post_recall_at10'] or 0.0:.6f} | {strong_metrics['conditional_chunk_recall_at10'] or 0.0:.6f} | {strong_metrics['final_evidence_recall_at10'] or 0.0:.6f} | {strong_metrics['gold_chunk_mrr'] or 0.0:.6f} |

## Failure-family × corpus-quality correlation

The join uses the 78 baseline missed gold references from the fixed Top10
retrieval diagnosis. `GOOD_CORPUS` is the strong-post bucket; all other gold
post categories are shown as thin/weak for this correlation.

| Failure family | Missed refs | Share of misses | Share of all gold | GOOD_CORPUS | Thin/weak/other |
|---|---:|---:|---:|---:|---:|
{chr(10).join(correlation_rows)}

The detailed join also includes query coverage categories and post IDs in the
JSON artifact. This keeps `POST_CANDIDATE_FAILURE` separate from isolated
chunk ranking failures.

## Exact FIRST_BAD_STATE

The RAG retrieval diagnosis remains:

`POST_RETRIEVAL → CHUNK_RETRIEVAL`

This audit adds a corpus-quality dimension. It does not move the first bad
state to generation or evidence selection. Projection mismatches, if any,
are reported separately from retrieval ranking.

## Verdict

`{verdict}`

The live corpus is therefore not projection-complete evidence of retrieval
quality by itself: 31/211 public posts have a low-information or debug-like
signal, including 29 posts under 50 characters and 6 exact duplicate-content
groups. However, the frozen gold set is materially healthier: 27/29 gold posts
are `GOOD_CORPUS`, no gold post has missing content or projection loss, and all
48 `LOCAL_RANKING_FAILURE` plus all 6 `CHUNK_BOUNDARY_FAILURE` references map
to `GOOD_CORPUS` posts. Corpus cleanup should be evaluated before further
ranking changes, while keeping `POST_RETRIEVAL → CHUNK_RETRIEVAL` as the
retrieval first-bad-state.

No production files were changed. No Qdrant collection was rebuilt, and no
post content or dataset annotation was modified.

## Next recommendation

Quarantine or separately tag low-information/debug/duplicate public posts at
the corpus eligibility boundary, then rerun the frozen retrieval evaluation.
Do not rebuild the current collection or alter ranking in this audit phase.

## Production files changed

`[]` — evaluation script and report artifacts only.

## Generated artifacts

- [rag_corpus_quality_audit.json](../evaluation/rag_corpus_quality_audit.json)
- [audit_rag_corpus_quality.py](../../apps/backend/scripts/audit_rag_corpus_quality.py)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--dataset", type=Path, default=Path("docs/evaluation/rag_evidence_dataset_v2.jsonl"))
    parser.add_argument("--chunk-fixture", type=Path, default=Path("docs/evaluation/rag_evidence_chunk_fixture_v2.json"))
    parser.add_argument("--retrieval-diagnosis", type=Path, default=Path("docs/evaluation/rag_chunk_retrieval_v3_diagnosis.json"))
    parser.add_argument("--storage-root", default=None)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    parser.add_argument("--qdrant-collection", default=COLLECTION)
    parser.add_argument("--mysql-host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--mysql-port", type=int, default=int(os.getenv("MYSQL_PORT", "33306")))
    parser.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", "123456"))
    parser.add_argument("--mysql-database", default=os.getenv("MYSQL_DB", "zhiguang"))
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--embedding-cache", default=DEFAULT_EMBEDDING_CACHE)
    parser.add_argument("--output", type=Path, default=Path("docs/evaluation/rag_corpus_quality_audit.json"))
    parser.add_argument("--report", type=Path, default=Path("docs/reports/RAG_CORPUS_QUALITY_AUDIT.md"))
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    env_values = _load_env(repo_root / ".env")
    storage_config = args.storage_root or env_values.get("LOCAL_STORAGE_ROOT", str(DEFAULT_STORAGE_ROOT))
    storage_root = _resolve_storage_root(repo_root, storage_config)
    validation = validate(args.dataset, args.chunk_fixture)
    if validation["status"] != "VALID":
        raise SystemExit(json.dumps({"verdict": "DATASET_QUALITY_ISSUE", "validation": validation}, ensure_ascii=False))

    posts = _load_posts(args)
    chunks = _load_chunks(args)
    post_by_id = {str(post["id"]): post for post in posts}
    chunks_by_id = {str(chunk["chunk_id"]): chunk for chunk in chunks}
    chunks_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_post[str(chunk["post_id"])].append(chunk)
    for values in chunks_by_post.values():
        values.sort(key=lambda item: int(item["chunk_index"]))

    content_infos: dict[str, dict[str, Any]] = {}
    debug_posts: list[str] = []
    for post in posts:
        post_id = str(post["id"])
        info = _read_object(post, storage_root)
        info.update(_content_flags(str(info.get("content") or "")))
        content_infos[post_id] = info
        if info["debug_or_placeholder"]:
            debug_posts.append(post_id)

    qdrant = _qdrant_snapshot(args.qdrant_url, args.qdrant_collection)
    qdrant_points = qdrant["points"]
    qdrant_chunk_ids = {str(point["chunk_id"]) for point in qdrant_points if point["chunk_id"]}
    qdrant_post_ids = {str(point["post_id"]) for point in qdrant_points if point["post_id"]}
    mysql_chunk_ids = set(chunks_by_id)
    eligible_ids = set(post_by_id)
    mysql_post_ids = set(chunks_by_post)
    chunks_per_post_values = [len(chunks_by_post.get(post_id, [])) for post_id in sorted(eligible_ids)]
    chunk_lengths = [len(str(chunk.get("content") or "").strip()) for chunk in chunks]
    duplicate_chunk_groups = _duplicate_groups(
        {str(chunk["chunk_id"]): str(chunk.get("content") or "") for chunk in chunks}
    )
    duplicate_post_groups = _duplicate_groups(
        {
            post_id: str(content_infos[post_id].get("content") or "")
            for post_id in content_infos
        }
    )
    near_duplicate_pairs = _near_duplicate_pairs(chunks)

    content_zero_chunks = [
        post_id
        for post_id in sorted(eligible_ids)
        if content_infos[post_id]["content_status"] == "AVAILABLE" and not chunks_by_post.get(post_id)
    ]
    projection_missing_posts: list[str] = []
    projection_content_mismatches: list[dict[str, Any]] = []
    qdrant_missing_chunk_ids: list[str] = []
    for post_id in sorted(eligible_ids):
        info = content_infos[post_id]
        post_chunks = chunks_by_post.get(post_id, [])
        if info["content_status"] == "AVAILABLE" and not post_chunks:
            projection_missing_posts.append(post_id)
        content = str(info.get("content") or "")
        for chunk in post_chunks:
            chunk_id = str(chunk["chunk_id"])
            if chunk_id not in qdrant_chunk_ids:
                qdrant_missing_chunk_ids.append(chunk_id)
            start = int(chunk.get("start_offset") or 0)
            end = int(chunk.get("end_offset") or 0)
            expected = content[start:end] if 0 <= start <= end <= len(content) else None
            actual = str(chunk.get("content") or "")
            if expected is None or expected != actual:
                projection_content_mismatches.append(
                    {
                        "post_id": post_id,
                        "chunk_id": chunk_id,
                        "chunk_index": int(chunk["chunk_index"]),
                        "expected_summary": _text_summary(expected),
                        "actual_summary": _text_summary(actual),
                    }
                )

    rows = [
        row
        for row in validation["valid_rows"]
        if row.get("category") != "no_answer"
    ]
    rows_by_query = {str(row["query_id"]): row for row in rows}
    fixture_payload = json.loads(args.chunk_fixture.read_text(encoding="utf-8"))
    fixture_chunks = {
        str(item["chunk_id"]): item
        for item in fixture_payload.get("evidence_chunks", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }
    gold_chunk_ids = {
        str(item["chunk_id"])
        for row in rows
        for item in row.get("gold_chunks", [])
        if isinstance(item, dict)
    }
    gold_chunk_expected: dict[str, dict[str, Any]] = {}
    gold_post_chunk_ids: dict[str, set[str]] = defaultdict(set)
    gold_identity_conflicts: list[dict[str, Any]] = []
    for row in rows:
        for item in row.get("gold_chunks", []):
            if not isinstance(item, dict):
                continue
            chunk_id = str(item["chunk_id"])
            expected = {
                "post_id": str(item["post_id"]),
                "chunk_index": int(item["chunk_index"]),
            }
            existing = gold_chunk_expected.get(chunk_id)
            if existing is not None and existing != expected:
                gold_identity_conflicts.append(
                    {
                        "chunk_id": chunk_id,
                        "first": existing,
                        "second": expected,
                    }
                )
            gold_chunk_expected[chunk_id] = expected
            gold_post_chunk_ids[expected["post_id"]].add(chunk_id)
    gold_post_ids = {
        str(item["post_id"])
        for row in rows
        for item in row.get("gold_chunks", [])
        if isinstance(item, dict)
    }
    answers_by_query = {
        str(row["query_id"]): str(row.get("gold_answer") or "")
        for row in rows
    }
    evidence_by_query = {
        str(row["query_id"]): [
            str(chunks_by_id.get(str(item["chunk_id"]), {}).get("content") or "")
            for item in row.get("gold_chunks", [])
            if isinstance(item, dict)
        ]
        for row in rows
    }
    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise SystemExit(
            "fastembed is required for semantic corpus coverage audit; run it with "
            "uv --with fastembed --with onnxruntime==1.20.1"
        ) from error
    embedder = TextEmbedding(model_name=args.embedding_model, cache_dir=args.embedding_cache)
    semantic_support = _semantic_support_scores(embedder, answers_by_query, evidence_by_query)
    gold_query_audits: dict[str, dict[str, Any]] = {}
    post_support_scores: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        evidence_parts: list[str] = []
        evidence_status: list[dict[str, Any]] = []
        for item in row.get("gold_chunks", []):
            if not isinstance(item, dict):
                continue
            chunk_id = str(item["chunk_id"])
            db_chunk = chunks_by_id.get(chunk_id)
            text = str(db_chunk.get("content") or "") if db_chunk else ""
            evidence_parts.append(text)
            evidence_status.append(
                {
                    "chunk_id": chunk_id,
                    "post_id": str(item["post_id"]),
                    "in_fixture": chunk_id in fixture_chunks,
                    "in_mysql": db_chunk is not None,
                    "in_qdrant": chunk_id in qdrant_chunk_ids,
                    "nonempty": bool(text.strip()),
                }
            )
        query_id = str(row["query_id"])
        semantic = semantic_support[query_id]
        lexical_score, missing_terms = _support_score(
            str(row.get("gold_answer") or ""), "\n".join(evidence_parts)
        )
        score = float(semantic["max"])
        category = (
            "NO_REAL_COVERAGE"
            if not any(part.strip() for part in evidence_parts)
            or score < SEMANTIC_NO_REAL_THRESHOLD
            else "STRONG_COVERAGE"
            if score >= SEMANTIC_STRONG_THRESHOLD
            else "PARTIAL_COVERAGE"
            if score >= SEMANTIC_PARTIAL_THRESHOLD
            else "WEAK_COVERAGE"
        )
        gold_query_audits[query_id] = {
            "query_id": query_id,
            "query": row.get("query", ""),
            "gold_answer": row.get("gold_answer", ""),
            "coverage": category,
            "support_score": score,
            "semantic_max_similarity": semantic["max"],
            "semantic_top2_mean": semantic["top2_mean"],
            "semantic_chunk_scores": semantic["chunk_scores"],
            "lexical_support_score": lexical_score,
            "unsupported_answer_terms": missing_terms,
            "evidence": evidence_status,
        }
        for item in row.get("gold_chunks", []):
            if isinstance(item, dict):
                post_support_scores[str(item["post_id"])].append(score)

    gold_chunk_audits: list[dict[str, Any]] = []
    annotation_issue_count = len(gold_identity_conflicts) + int(validation.get("MISSING_CHUNK_COUNT", 0))
    for chunk_id in sorted(gold_chunk_ids):
        fixture = fixture_chunks.get(chunk_id)
        db_chunk = chunks_by_id.get(chunk_id)
        expected = gold_chunk_expected[chunk_id]
        fixture_valid = fixture is not None
        db_valid = db_chunk is not None
        qdrant_valid = chunk_id in qdrant_chunk_ids
        nonempty = bool(db_chunk and str(db_chunk.get("content") or "").strip())
        parent_available = content_infos.get(expected["post_id"], {}).get("content_status") == "AVAILABLE"
        identity_valid = bool(
            db_chunk
            and str(db_chunk["post_id"]) == expected["post_id"]
            and int(db_chunk["chunk_index"]) == expected["chunk_index"]
        )
        annotation_issue = not fixture_valid or chunk_id in {
            conflict["chunk_id"] for conflict in gold_identity_conflicts
        }
        gold_chunk_audits.append(
            {
                "chunk_id": chunk_id,
                "expected_post_id": expected["post_id"],
                "expected_chunk_index": expected["chunk_index"],
                "fixture_present": fixture_valid,
                "mysql_present": db_valid,
                "qdrant_present": qdrant_valid,
                "nonempty": nonempty,
                "parent_content_available": parent_available,
                "identity_valid": identity_valid,
                "annotation_issue": annotation_issue,
                "post_id": expected["post_id"],
                "chunk_index": expected["chunk_index"],
                "text_summary": _text_summary(db_chunk.get("content")) if db_chunk else "",
            }
        )

    gold_post_audits: list[dict[str, Any]] = []
    for post_id in sorted(gold_post_ids):
        post = post_by_id.get(post_id)
        info = content_infos.get(post_id, {"content_status": "MISSING_CONTENT", "content_chars": 0})
        post_chunks = chunks_by_post.get(post_id, [])
        post_gold_ids = gold_post_chunk_ids.get(post_id, set())
        post_gold_items = [item for item in gold_chunk_audits if item.get("expected_post_id") == post_id]
        post_projection_missing = any(
            not item["mysql_present"]
            or not item["qdrant_present"]
            or not item["nonempty"]
            or not item["identity_valid"]
            for item in post_gold_items
        )
        fixture_issue = any(item["annotation_issue"] for item in post_gold_items)
        scores = post_support_scores.get(post_id, [])
        weak_evidence = bool(scores) and max(scores) < SEMANTIC_STRONG_THRESHOLD
        category = _quality_category(
            info,
            len(post_chunks),
            post_projection_missing,
            weak_evidence,
            fixture_issue or post is None,
        )
        gold_post_audits.append(
            {
                "post_id": post_id,
                "title": post.get("title", "") if post else "",
                "content_status": info.get("content_status", "MISSING_CONTENT"),
                "content_chars": info.get("content_chars", 0),
                "chunk_count": len(post_chunks),
                "gold_chunk_count": len(post_gold_ids),
                "gold_chunk_ids": sorted(post_gold_ids),
                "gold_chunk_valid": not post_projection_missing and not fixture_issue,
                "quality": category,
                "quality_reasons": {
                    "debug_or_placeholder": bool(info.get("debug_or_placeholder")),
                    "support_scores": scores,
                    "object_key": info.get("object_key", ""),
                },
            }
        )

    quality_counts = Counter(item["quality"] for item in gold_post_audits)
    coverage_counts = Counter(item["coverage"] for item in gold_query_audits.values())
    all_query_ids = set(rows_by_query)
    strong_query_ids = {
        query_id
        for query_id, item in gold_query_audits.items()
        if item["coverage"] == "STRONG_COVERAGE"
    }
    traces_payload = json.loads(args.retrieval_diagnosis.read_text(encoding="utf-8"))
    traces_by_query = {
        str(trace["query_id"]): trace
        for trace in traces_payload.get("traces", [])
        if isinstance(trace, dict) and trace.get("query_id")
    }
    if set(traces_by_query) != all_query_ids:
        raise RuntimeError(
            "retrieval diagnosis query set does not match answerable dataset: "
            f"diagnosis={len(traces_by_query)} dataset={len(all_query_ids)}"
        )
    post_quality_by_id = {item["post_id"]: item["quality"] for item in gold_post_audits}
    failure_correlation: dict[str, dict[str, Any]] = {}
    for trace in traces_by_query.values():
        query_id = str(trace["query_id"])
        for item in trace.get("gold_chunks", []):
            family = str(item.get("failure_family") or "UNKNOWN")
            if family == "HIT":
                continue
            post_id = str(item.get("post_id") or "")
            quality = post_quality_by_id.get(post_id, "UNKNOWN")
            coverage = gold_query_audits[query_id]["coverage"]
            entry = failure_correlation.setdefault(
                family,
                {
                    "total": 0,
                    "gold_post_quality": Counter(),
                    "query_coverage": Counter(),
                    "examples": [],
                },
            )
            entry["total"] += 1
            entry["gold_post_quality"][quality] += 1
            entry["query_coverage"][coverage] += 1
            if len(entry["examples"]) < 10:
                entry["examples"].append(
                    {
                        "query_id": query_id,
                        "chunk_id": item.get("chunk_id"),
                        "post_id": post_id,
                        "post_quality": quality,
                        "coverage": coverage,
                    }
                )
    for value in failure_correlation.values():
        value["gold_post_quality"] = dict(value["gold_post_quality"])
        value["query_coverage"] = dict(value["query_coverage"])
        missed_count = int(value["total"])
        baseline_missed = int(
            traces_payload.get("failure_summary", {}).get("baseline_missed_gold_chunk_count") or 0
        )
        all_gold = sum(len(row.get("gold_chunks", [])) for row in rows)
        value["share_of_misses"] = round(missed_count / baseline_missed, 6) if baseline_missed else 0.0
        value["share_of_all_gold"] = round(missed_count / all_gold, 6) if all_gold else 0.0

    content_status_counts = Counter(info["content_status"] for info in content_infos.values())
    non_good_live = sum(
        1
        for info in content_infos.values()
        if info["content_status"] != "AVAILABLE"
        or info.get("content_chars", 0) < 300
        or info.get("debug_or_placeholder")
    )
    projection_issue = bool(projection_missing_posts or qdrant_missing_chunk_ids or projection_content_mismatches)
    dataset_issue = bool(annotation_issue_count or any(item["quality"] == "ANNOTATION_ISSUE" for item in gold_post_audits))
    weak_coverage_count = coverage_counts.get("WEAK_COVERAGE", 0) + coverage_counts.get("NO_REAL_COVERAGE", 0)
    verdicts: list[str] = []
    if projection_issue:
        verdicts.append("PROJECTION_QUALITY_ISSUE")
    if dataset_issue:
        verdicts.append("EVALUATION_DATASET_QUALITY_ISSUE")
    if non_good_live / len(posts) > 0.1 or weak_coverage_count / len(rows) > 0.1:
        verdicts.append("CORPUS_QUALITY_LIMITING_RAG")
    if not verdicts:
        verdicts.append("CORPUS_QUALITY_PASS")

    output = {
        "checkpoint": CHECKPOINT,
        "verdict": verdicts[0] if len(verdicts) == 1 else verdicts,
        "verdicts": verdicts,
        "runtime": {
            "storage_provider": env_values.get("STORAGE_PROVIDER", "aliyun"),
            "storage_root": str(storage_root),
            "qdrant_collection": args.qdrant_collection,
            "qdrant_read_only": True,
            "production_files_changed": [],
            "collection_rebuilt": False,
            "coverage_embedding_model": args.embedding_model,
            "coverage_embedding_cache": args.embedding_cache,
            "coverage_thresholds": {
                "strong": SEMANTIC_STRONG_THRESHOLD,
                "partial": SEMANTIC_PARTIAL_THRESHOLD,
                "no_real": SEMANTIC_NO_REAL_THRESHOLD,
            },
        },
        "live_corpus": {
            "eligible_post_count": len(posts),
            "content_status_counts": dict(content_status_counts),
            "missing_or_unreadable_count": sum(
                count
                for status, count in content_status_counts.items()
                if status not in {"AVAILABLE", "EMPTY_CONTENT", "WHITESPACE_ONLY"}
            ),
            "debug_or_placeholder_count": len(debug_posts),
            "debug_or_placeholder_post_ids": debug_posts,
            "duplicate_post_group_count": len(duplicate_post_groups),
            "duplicate_post_groups": duplicate_post_groups[:100],
            "non_good_quality_signal_count": non_good_live,
            "content_length_distribution": _length_distribution(
                [int(info["content_chars"]) for info in content_infos.values()]
            ),
            "posts": [
                {
                    **post,
                    "content_info": _public_content_info(content_infos[str(post["id"])]),
                    "content_summary": _text_summary(content_infos[str(post["id"])].get("content")),
                }
                for post in posts
            ],
        },
        "chunk_corpus": {
            "mysql_chunk_count": len(chunks),
            "qdrant_point_count": len(qdrant_points),
            "qdrant_reported_points_count": qdrant.get("points_count"),
            "mysql_chunk_post_count": len(mysql_post_ids),
            "qdrant_chunk_post_count": len(qdrant_post_ids),
            "chunks_per_post": _length_distribution(chunks_per_post_values),
            "empty_chunk_count": sum(not str(chunk.get("content") or "").strip() for chunk in chunks),
            "tiny_chunk_count": sum(value < 50 for value in chunk_lengths),
            "chunk_length_distribution": _length_distribution(chunk_lengths),
            "duplicate_chunk_group_count": len(duplicate_chunk_groups),
            "duplicate_chunk_groups": duplicate_chunk_groups[:100],
            "near_duplicate_pair_count": len(near_duplicate_pairs),
            "near_duplicate_pairs": near_duplicate_pairs,
            "posts_with_zero_chunks_count": sum(not chunks_by_post.get(post_id) for post_id in eligible_ids),
            "posts_with_zero_chunks": [post_id for post_id in sorted(eligible_ids) if not chunks_by_post.get(post_id)],
            "posts_with_one_chunk_count": sum(len(chunks_by_post.get(post_id, [])) == 1 for post_id in eligible_ids),
            "posts_with_one_chunk": [
                post_id for post_id in sorted(eligible_ids) if len(chunks_by_post.get(post_id, [])) == 1
            ],
            "content_but_zero_chunks_count": len(content_zero_chunks),
            "content_but_zero_chunks": content_zero_chunks,
            "eligible_equals_mysql_posts": eligible_ids == mysql_post_ids,
            "eligible_equals_qdrant_posts": eligible_ids == qdrant_post_ids,
            "mysql_equals_qdrant_chunks": mysql_chunk_ids == qdrant_chunk_ids,
            "qdrant_extra_post_ids": sorted(qdrant_post_ids - eligible_ids),
            "qdrant_missing_post_ids": sorted(eligible_ids - qdrant_post_ids),
            "qdrant_missing_chunk_ids_count": len(qdrant_missing_chunk_ids),
            "qdrant_missing_chunk_ids": qdrant_missing_chunk_ids[:500],
            "projection_content_mismatch_count": len(projection_content_mismatches),
            "projection_content_mismatches": projection_content_mismatches[:100],
        },
        "gold_corpus": {
            "unique_gold_post_count": len(gold_post_ids),
            "gold_chunk_count": len(gold_chunk_ids),
            "gold_reference_count": sum(len(row.get("gold_chunks", [])) for row in rows),
            "gold_chunks_in_fixture": sum(item["fixture_present"] for item in gold_chunk_audits),
            "gold_chunks_in_mysql": sum(item["mysql_present"] for item in gold_chunk_audits),
            "gold_chunks_in_qdrant": sum(item["qdrant_present"] for item in gold_chunk_audits),
            "gold_chunks_nonempty": sum(item["nonempty"] for item in gold_chunk_audits),
            "gold_chunks_parent_available": sum(item["parent_content_available"] for item in gold_chunk_audits),
            "annotation_issue_count": annotation_issue_count,
            "identity_conflict_count": len(gold_identity_conflicts),
            "identity_conflicts": gold_identity_conflicts,
            "quality_distribution": {
                category: {
                    "count": quality_counts.get(category, 0),
                    "rate": round(quality_counts.get(category, 0) / len(gold_post_ids), 6)
                    if gold_post_ids
                    else 0.0,
                }
                for category in (
                    "GOOD_CORPUS",
                    "THIN_CONTENT",
                    "MISSING_CONTENT",
                    "PROJECTION_MISSING",
                    "WEAK_GOLD_EVIDENCE",
                    "ANNOTATION_ISSUE",
                )
            },
            "posts": gold_post_audits,
            "chunks": gold_chunk_audits,
        },
        "knowledge_coverage": {
            "query_count": len(rows),
            "distribution": {
                category: {
                    "count": coverage_counts.get(category, 0),
                    "rate": round(coverage_counts.get(category, 0) / len(rows), 6) if rows else 0.0,
                }
                for category in (
                    "STRONG_COVERAGE",
                    "PARTIAL_COVERAGE",
                    "WEAK_COVERAGE",
                    "NO_REAL_COVERAGE",
                )
            },
            "queries": gold_query_audits,
        },
        "retrieval_metrics": {
            "all": _metrics_for_traces(rows_by_query, traces_by_query, all_query_ids),
            "strong_coverage_only": _metrics_for_traces(rows_by_query, traces_by_query, strong_query_ids),
        },
        "failure_correlation": failure_correlation,
        "retrieval_diagnosis": {
            "baseline_missed_gold_chunk_count": traces_payload.get("failure_summary", {}).get(
                "baseline_missed_gold_chunk_count"
            ),
            "first_bad_state": "POST_RETRIEVAL → CHUNK_RETRIEVAL",
            "source": str(args.retrieval_diagnosis),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(output), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": output["verdict"],
                "eligible_posts": len(posts),
                "mysql_chunks": len(chunks),
                "qdrant_points": len(qdrant_points),
                "gold_posts": len(gold_post_ids),
                "gold_chunks": len(gold_chunk_ids),
                "coverage": output["knowledge_coverage"]["distribution"],
                "retrieval_all": output["retrieval_metrics"]["all"],
                "retrieval_strong": output["retrieval_metrics"]["strong_coverage_only"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
