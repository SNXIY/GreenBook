"""Capture a read-only, self-contained RAG retrieval evaluation snapshot.

The snapshot freezes the V3 Top10 post pool and the current chunk retrieval
results for the answerable RAG Dataset V2 queries.  It contains the chunk
content needed by later offline experiments, so those experiments do not read
live MySQL or Qdrant state again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluate_rag_chunk_retrieval_v3 import (
    BASELINE_DEPTH,
    OUTPUT_DEPTH,
    _mysql_chunks,
    _qdrant_search,
)
from validate_rag_dataset_v2 import validate

SNAPSHOT_VERSION = "rag_retrieval_frozen_snapshot_v1"
V4_CHECKPOINT = "3f10819"
V4_BASELINE_CHECKPOINT = "722a072e08f98dd6c2dd8b429c8651761244e4d9"
V3_DIAGNOSIS_CHECKPOINT = "df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6"
COLLECTION = "post_chunks_multilingual_v1"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
QDRANT_LIMIT = 1000


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _summary(value: Any, limit: int = 180) -> str:
    text = " ".join(_text(value).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _qdrant_collection_info(qdrant_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{qdrant_url.rstrip('/')}/collections/{COLLECTION}",
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    result = payload.get("result") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else {}


def _strong_query_ids(corpus_audit_path: Path) -> list[str]:
    audit = json.loads(corpus_audit_path.read_text(encoding="utf-8"))
    queries = audit.get("knowledge_coverage", {}).get("queries", {})
    return sorted(
        _text(query_id)
        for query_id, item in queries.items()
        if isinstance(item, dict) and item.get("coverage") == "STRONG_COVERAGE"
    )


def _catalog_entry(row: dict[str, Any]) -> dict[str, Any]:
    content = _text(row.get("content"))
    return {
        "chunk_id": _text(row.get("chunk_id")),
        "post_id": _text(row.get("post_id")),
        "chunk_index": int(row.get("chunk_index") or 0),
        "content": content,
        "length": len(content),
        "title": _text(row.get("title")),
        "tags": _text(row.get("tags")),
        "description": _text(row.get("description")),
        "start_offset": int(row.get("start_offset") or 0),
        "end_offset": int(row.get("end_offset") or 0),
        "event_version": row.get("event_version"),
    }


def _candidate_entry(hit: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chunk_id = _text(hit.get("chunk_id"))
    chunk = catalog.get(chunk_id, {})
    content = _text(chunk.get("content"))
    return {
        "chunk_id": chunk_id,
        "post_id": _text(hit.get("post_id") or chunk.get("post_id")),
        "chunk_index": int(hit.get("chunk_index") or chunk.get("chunk_index") or 0),
        "rank": int(hit.get("rank") or 0),
        "score": round(float(hit.get("score") or 0.0), 8),
        "length": len(content),
        "content": content,
        "text_summary": _summary(content),
        "start_offset": int(chunk.get("start_offset") or 0),
        "end_offset": int(chunk.get("end_offset") or 0),
    }


def _gold_entry(item: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chunk_id = _text(item.get("chunk_id"))
    chunk = catalog.get(chunk_id, {})
    content = _text(chunk.get("content"))
    return {
        "chunk_id": chunk_id,
        "post_id": _text(item.get("post_id")),
        "chunk_index": int(item.get("chunk_index") or 0),
        "content": content,
        "length": len(content),
        "text_summary": _summary(content),
        "exists_in_catalog": bool(chunk),
        "start_offset": int(chunk.get("start_offset") or 0),
        "end_offset": int(chunk.get("end_offset") or 0),
    }


def _canonical_digest(payload: dict[str, Any]) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(stable.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("docs/evaluation/rag_evidence_dataset_v2.jsonl"))
    parser.add_argument("--diagnosis", type=Path, default=Path("docs/evaluation/rag_chunk_retrieval_v3_diagnosis.json"))
    parser.add_argument("--corpus-audit", type=Path, default=Path("docs/evaluation/rag_corpus_quality_audit.json"))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    parser.add_argument("--mysql-host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--mysql-port", type=int, default=int(os.getenv("MYSQL_PORT", "33306")))
    parser.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", "123456"))
    parser.add_argument("--mysql-database", default=os.getenv("MYSQL_DB", "zhiguang"))
    parser.add_argument("--embedding-model", default=MODEL_NAME)
    parser.add_argument("--embedding-cache", default=r"D:\tmp\greenbook-retrieval-model-cache")
    parser.add_argument("--output", type=Path, default=Path("docs/evaluation/rag_retrieval_frozen_snapshot_v1.json"))
    args = parser.parse_args()

    validation = validate(args.dataset, Path("docs/evaluation/rag_evidence_chunk_fixture_v2.json"))
    if validation["status"] != "VALID":
        raise SystemExit(json.dumps({"verdict": "RAG_RETRIEVAL_SNAPSHOT_BLOCKED", "validation": validation}, ensure_ascii=False))
    rows = [row for row in validation["valid_rows"] if row.get("category") != "no_answer"]
    if len(rows) != 45:
        raise SystemExit(f"expected 45 answerable rows, found {len(rows)}")

    diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
    traces = {
        _text(item["query_id"]): item
        for item in diagnosis.get("traces", [])
        if isinstance(item, dict) and item.get("query_id")
    }
    query_ids = {_text(row["query_id"]) for row in rows}
    if query_ids != set(traces):
        raise SystemExit("frozen V3 diagnosis does not match the 45 answerable query IDs")

    corpus_audit = json.loads(args.corpus_audit.read_text(encoding="utf-8"))
    live_post_ids = [
        _text(item.get("id"))
        for item in corpus_audit.get("live_corpus", {}).get("posts", [])
        if isinstance(item, dict) and item.get("id")
    ]
    candidate_posts_by_query = {
        _text(row["query_id"]): [
            {
                "post_id": _text(item["post_id"]),
                "rank": index,
            }
            for index, item in enumerate(traces[_text(row["query_id"])].get("candidate_posts", [])[:BASELINE_DEPTH], 1)
            if isinstance(item, dict) and item.get("post_id")
        ]
        for row in rows
    }
    candidate_post_ids = {
        item["post_id"]
        for posts in candidate_posts_by_query.values()
        for item in posts
    }
    all_post_ids = sorted(set(live_post_ids) | candidate_post_ids)
    chunk_rows = _mysql_chunks(
        args.mysql_host,
        args.mysql_port,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
        all_post_ids,
    )
    catalog_entries = [_catalog_entry(row) for row in chunk_rows]
    catalog = {item["chunk_id"]: item for item in catalog_entries if item["chunk_id"]}

    try:
        from fastembed import TextEmbedding
    except ImportError as error:
        raise SystemExit(
            "fastembed is required; run with uv --with fastembed --with onnxruntime==1.20.1"
        ) from error
    embedder = TextEmbedding(model_name=args.embedding_model, cache_dir=args.embedding_cache)
    query_vectors = list(embedder.embed([_text(row["query"]) for row in rows]))

    query_snapshots: list[dict[str, Any]] = []
    capture_drift_queries: list[str] = []
    missing_candidate_chunks: list[dict[str, Any]] = []
    for row, vector in zip(rows, query_vectors, strict=True):
        query_id = _text(row["query_id"])
        candidate_posts = candidate_posts_by_query[query_id]
        post_ids = [item["post_id"] for item in candidate_posts]
        hits = _qdrant_search(
            args.qdrant_url,
            [float(value) for value in vector],
            post_ids,
            limit=QDRANT_LIMIT,
        )
        old_ids = [
            _text(item.get("chunk_id"))
            for item in traces[query_id].get("candidate_chunks", [])[:OUTPUT_DEPTH]
            if isinstance(item, dict) and item.get("chunk_id")
        ]
        new_ids = [_text(item.get("chunk_id")) for item in hits[:OUTPUT_DEPTH]]
        if old_ids != new_ids:
            capture_drift_queries.append(query_id)
        candidate_chunks = []
        for hit in hits:
            entry = _candidate_entry(hit, catalog)
            if not entry["content"]:
                missing_candidate_chunks.append({"query_id": query_id, "chunk_id": entry["chunk_id"]})
            candidate_chunks.append(entry)
        query_snapshots.append(
            {
                "query_id": query_id,
                "query": _text(row["query"]),
                "category": row.get("category"),
                "gold_answer": _text(row.get("gold_answer")),
                "candidate_posts": candidate_posts,
                "gold_post_ids": [_text(value) for value in row.get("gold_post_ids", [])],
                "gold_chunks": [_gold_entry(item, catalog) for item in row.get("gold_chunks", []) if isinstance(item, dict)],
                "candidate_chunks": candidate_chunks,
                "query_vector_sha256": _sha256_bytes(
                    json.dumps([float(value) for value in vector], separators=(",", ":")).encode("utf-8")
                ),
                "query_vector_dimension": len(vector),
            }
        )

    if missing_candidate_chunks:
        raise SystemExit(
            json.dumps(
                {
                    "verdict": "RAG_RETRIEVAL_SNAPSHOT_BLOCKED",
                    "missing_candidate_chunks": missing_candidate_chunks[:20],
                    "missing_count": len(missing_candidate_chunks),
                },
                ensure_ascii=False,
            )
        )

    source_files = {
        str(path): _sha256_file(path)
        for path in (args.dataset, args.diagnosis, args.corpus_audit)
    }
    deterministic = {
        "snapshot_version": SNAPSHOT_VERSION,
        "v4_checkpoint": V4_CHECKPOINT,
        "v4_baseline_checkpoint": V4_BASELINE_CHECKPOINT,
        "v3_diagnosis_checkpoint": V3_DIAGNOSIS_CHECKPOINT,
        "dataset": {
            "query_count": 50,
            "answerable_query_count": len(query_snapshots),
            "gold_reference_count": sum(len(item["gold_chunks"]) for item in query_snapshots),
            "unique_gold_chunk_count": len(
                {
                    item["chunk_id"]
                    for query in query_snapshots
                    for item in query["gold_chunks"]
                }
            ),
        },
        "scope": {
            "candidate_post_depth": BASELINE_DEPTH,
            "output_chunk_depth": OUTPUT_DEPTH,
            "qdrant_limit": QDRANT_LIMIT,
            "collection": COLLECTION,
            "embedding_model": args.embedding_model,
            "candidate_post_count": len(candidate_post_ids),
            "live_public_post_count": len(live_post_ids),
            "chunk_catalog_count": len(catalog_entries),
        },
        "strong_coverage_query_ids": _strong_query_ids(args.corpus_audit),
        "source_files": source_files,
        "chunk_catalog": catalog_entries,
        "queries": query_snapshots,
    }
    snapshot = {
        **deterministic,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "capture_vs_v3_snapshot_drift_count": len(capture_drift_queries),
        "capture_vs_v3_snapshot_drift_queries": capture_drift_queries,
        "qdrant_collection_info": _qdrant_collection_info(args.qdrant_url),
        "mysql": {
            "host": args.mysql_host,
            "port": args.mysql_port,
            "database": args.mysql_database,
        },
        "snapshot_digest": _canonical_digest(deterministic),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "snapshot_version": SNAPSHOT_VERSION,
                "snapshot_digest": snapshot["snapshot_digest"],
                "queries": len(query_snapshots),
                "candidate_posts": len(candidate_post_ids),
                "chunk_catalog": len(catalog_entries),
                "candidate_chunks": sum(len(item["candidate_chunks"]) for item in query_snapshots),
                "capture_vs_v3_snapshot_drift_count": len(capture_drift_queries),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
