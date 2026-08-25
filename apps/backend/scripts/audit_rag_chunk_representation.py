"""Offline A/B/C chunk representation audit for RAG diagnosis.

This is deliberately an evaluation-only script.  It keeps the live Top10
candidate posts from the chunk-retrieval diagnosis, reads canonical chunk
text/title/tags from MySQL, and compares exact cosine ranking for:

  A: content
  B: title + content
  C: title + tags + content

The current Qdrant vector is also reported as ``C_PRODUCTION``.  Production
currently includes the bounded description field in addition to title/tags/
content; that vector is read-only and is not rewritten by this script.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


REPRESENTATIONS = {
    "A_CONTENT_ONLY": "A",
    "B_TITLE_CONTENT": "B",
    "C_TITLE_TAGS_CONTENT": "C",
}


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _normalize(values: list[float]) -> list[float]:
    norm = sum(value * value for value in values) ** 0.5
    if norm == 0.0:
        raise ValueError("zero vector")
    return [value / norm for value in values]


def _truncate(value: Any, max_chars: int) -> str:
    text = "" if value is None else str(value).strip()
    return text[:max_chars]


def _text(row: dict[str, Any], representation: str) -> str:
    content = str(row.get("content") or "")
    title = _truncate(row.get("title"), 256)
    tags = _truncate(row.get("tags"), 512)
    description = _truncate(row.get("description"), 768)
    if representation == "A":
        return content
    if representation == "B":
        return f"title: {title}\ncontent: {content}"
    if representation == "C":
        return f"title: {title}\ntags: {tags}\ncontent: {content}"
    if representation == "C_PRODUCTION":
        return (
            f"title: {title}\n"
            f"tags: {tags}\n"
            f"description: {description}\n"
            f"content: {content}"
        )
    raise ValueError(f"unknown representation: {representation}")


def _mysql_chunks(
    container: str,
    user: str,
    password: str,
    database: str,
    post_ids: list[str],
) -> list[dict[str, Any]]:
    numeric_ids = [value for value in post_ids if value.isdigit()]
    if not numeric_ids:
        return []
    values = ",".join(numeric_ids)
    query = f"""
SELECT JSON_OBJECT(
  'chunk_id', c.chunk_id,
  'post_id', CAST(c.post_id AS CHAR),
  'chunk_index', c.chunk_index,
  'content', c.content,
  'title', COALESCE(p.title, ''),
  'tags', COALESCE(CAST(p.tags AS CHAR), ''),
  'description', COALESCE(p.description, ''),
  'event_version', c.event_version
)
FROM post_chunks c
JOIN know_posts p ON p.id = c.post_id
WHERE c.post_id IN ({values})
  AND p.status = 'published'
  AND p.visible = 'public'
ORDER BY c.post_id, c.chunk_index
"""
    command = [
        "docker", "exec", container, "mysql",
        f"--user={user}", f"--password={password}", f"--database={database}",
        "--default-character-set=utf8mb4",
        "--batch", "--raw", "--skip-column-names", "-e", query,
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            "MySQL read-only fixture query failed: "
            + (result.stderr or result.stdout)[-2000:]
        )
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict) and str(value.get("content") or "").strip():
                rows.append(value)
    return rows


def _qdrant_vectors(
    client: httpx.Client,
    qdrant_url: str,
    post_ids: list[str],
) -> dict[str, list[float]]:
    numeric_ids = [int(value) for value in post_ids if value.isdigit()]
    if not numeric_ids:
        return {}
    vectors: dict[str, list[float]] = {}
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 1000,
            "with_payload": True,
            "with_vector": True,
            "filter": {"must": [{"key": "post_id", "match": {"any": numeric_ids}}]},
        }
        if offset is not None:
            body["offset"] = offset
        response = client.post(
            f"{qdrant_url.rstrip('/')}/collections/post_chunks_multilingual_v1/points/scroll",
            json=body,
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        points = result.get("points", []) if isinstance(result, dict) else []
        for point in points:
            if not isinstance(point, dict):
                continue
            payload = point.get("payload") or {}
            chunk_id = str(payload.get("chunk_id", point.get("id", "")))
            vector = point.get("vector")
            if chunk_id and isinstance(vector, list) and len(vector) == 384:
                vectors[chunk_id] = _normalize([float(value) for value in vector])
        next_offset = result.get("next_page_offset") if isinstance(result, dict) else None
        if next_offset is None or not points:
            break
        offset = next_offset
    return vectors


def _embed(client: httpx.Client, endpoint: str, text: str) -> list[float]:
    response = client.post(endpoint, json={"text": text, "input_type": "document"})
    response.raise_for_status()
    values = response.json().get("embedding")
    if not isinstance(values, list) or len(values) != 384:
        raise ValueError("embedding contract did not return 384 dimensions")
    return _normalize([float(value) for value in values])


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _rank(
    query_vector: list[float],
    candidate_ids: set[str],
    vectors: dict[str, list[float]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    scored = [
        {"chunk_id": chunk_id, "score": round(_dot(query_vector, vector), 8)}
        for chunk_id, vector in vectors.items()
        if chunk_id in candidate_ids
    ]
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return [dict(item, rank=index + 1) for index, item in enumerate(scored[:limit])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnosis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8181/embed")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:26333")
    parser.add_argument("--mysql-container", default="greenbook-mysql")
    parser.add_argument("--mysql-user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--mysql-password", default=os.getenv("MYSQL_PASSWORD", "123456"))
    parser.add_argument("--mysql-database", default=os.getenv("MYSQL_DB", "zhiguang"))
    parser.add_argument("--max-cases", type=int, default=20)
    args = parser.parse_args()

    diagnosis = json.loads(args.diagnosis.read_text(encoding="utf-8"))
    cases = diagnosis.get("failure_cases", [])[:args.max_cases]
    records = {str(row["query_id"]): row for row in diagnosis.get("records", [])}
    if not cases:
        raise SystemExit("diagnosis has no failure cases")

    post_ids: set[str] = set()
    for case in cases:
        post_ids.update(str(value) for value in case.get("candidate_posts", []))
    chunk_rows = _mysql_chunks(
        args.mysql_container,
        args.mysql_user,
        args.mysql_password,
        args.mysql_database,
        sorted(post_ids),
    )
    chunk_by_id = {str(row["chunk_id"]): row for row in chunk_rows}
    candidate_chunk_ids_by_query = {
        str(case["query_id"]): {
            str(row["chunk_id"])
            for row in chunk_rows
            if str(row["post_id"]) in {str(value) for value in case.get("candidate_posts", [])}
        }
        for case in cases
    }

    query_ids = [str(case["query_id"]) for case in cases]
    query_vectors: dict[str, list[float]] = {}
    vectors_by_rep: dict[str, dict[str, list[float]]] = {
        key: {} for key in REPRESENTATIONS
    }
    with httpx.Client(timeout=60) as client:
        current_vectors = _qdrant_vectors(client, args.qdrant_url, sorted(post_ids))
        for query_id in query_ids:
            query_vectors[query_id] = _embed(client, args.embedding_url, records[query_id]["question"])
        total = len(chunk_rows) * len(REPRESENTATIONS)
        completed = 0
        for representation, label in REPRESENTATIONS.items():
            for row in chunk_rows:
                chunk_id = str(row["chunk_id"])
                vectors_by_rep[representation][chunk_id] = _embed(
                    client, args.embedding_url, _text(row, label)
                )
                completed += 1
                if completed % 100 == 0:
                    print(json.dumps({
                        "embedding_progress": f"{completed}/{total}",
                        "representation": representation,
                    }, ensure_ascii=False), flush=True)

    vectors_by_rep["C_PRODUCTION"] = current_vectors
    metric_rows: dict[str, list[float]] = {key: [] for key in (*REPRESENTATIONS, "C_PRODUCTION")}
    detail_rows: list[dict[str, Any]] = []
    for case in cases:
        query_id = str(case["query_id"])
        record = records[query_id]
        candidate_posts = {str(value) for value in case.get("candidate_posts", [])}
        expected = {
            str(item["chunk_id"]): str(item["post_id"])
            for item in record.get("gold_chunks", [])
            if str(item["post_id"]) in candidate_posts
        }
        candidate_ids = candidate_chunk_ids_by_query[query_id]
        details: dict[str, Any] = {
            "query_id": query_id,
            "query": record["question"],
            "candidate_post_count": len(candidate_posts),
            "candidate_chunk_count": len(candidate_ids),
            "expected_candidate_gold_chunks": list(expected),
            "representations": {},
        }
        for representation in (*REPRESENTATIONS, "C_PRODUCTION"):
            ranked = _rank(query_vectors[query_id], candidate_ids, vectors_by_rep[representation])
            ranked_ids = {item["chunk_id"] for item in ranked}
            if expected:
                metric_rows[representation].append(len(set(expected) & ranked_ids) / len(expected))
            gold_ranks: dict[str, Any] = {}
            rank_by_id = {item["chunk_id"]: item for item in ranked}
            for chunk_id in expected:
                gold_ranks[chunk_id] = rank_by_id.get(chunk_id, {"rank": None, "score": None})
            details["representations"][representation] = {
                "top10": ranked,
                "gold": gold_ranks,
            }
        detail_rows.append(details)

    metrics = {
        representation: {
            "CONDITIONAL_CHUNK_RECALL@10": _mean(values),
            "evaluated_query_count": len(values),
            "gold_post_present_query_count": len(values),
            "candidate_post_count": len(post_ids),
            "candidate_chunk_count": len(chunk_rows),
        }
        for representation, values in metric_rows.items()
    }
    output = {
        "status": "VALID",
        "generated_at": date.today().isoformat(),
        "scope": {
            "failure_case_count": len(cases),
            "query_count": len(query_ids),
            "candidate_post_count": len(post_ids),
            "candidate_chunk_count": len(chunk_rows),
            "candidate_scope": "Top10 posts from the 20 exported failure cases",
            "ranking": "offline exact cosine over the candidate-post chunk pool",
            "A": "content only",
            "B": "title + content",
            "C": "title + tags + content",
            "C_PRODUCTION": "current Qdrant vector from title + tags + description + content",
        },
        "metrics": metrics,
        "details": detail_rows,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "scope": output["scope"],
        "metrics": output["metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
