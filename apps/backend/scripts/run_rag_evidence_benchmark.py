"""Run the live, retrieval-only RAG evidence benchmark.

This calls the domain evidence endpoint, never Elasticsearch or Qdrant
directly.  The generated JSONL is consumed by evaluate_rag_evidence.py.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def login(client: httpx.Client, base_url: str) -> str:
    token = os.getenv("GREENBOOK_E2E_ACCESS_TOKEN", "").strip()
    if token:
        return token
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        json={
            "identifierType": os.getenv("GREENBOOK_E2E_IDENTIFIER_TYPE", "PHONE"),
            "identifier": os.getenv("GREENBOOK_E2E_IDENTIFIER", ""),
            "password": os.getenv("GREENBOOK_E2E_PASSWORD", ""),
            "code": None,
        },
    )
    response.raise_for_status()
    return str(response.json()["token"]["accessToken"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token")
    parser.add_argument("--top-posts", type=int, default=8)
    parser.add_argument("--top-chunks", type=int, default=10)
    args = parser.parse_args()

    rows = load_rows(args.dataset)
    with httpx.Client(timeout=60) as client:
        token = args.token or login(client, args.base_url.rstrip("/"))
        runs: list[dict[str, Any]] = []
        for row in rows:
            started = time.perf_counter()
            result: dict[str, Any] = {
                "provider": "rag_evidence",
                "query_id": row["query_id"],
                "evidence": [],
                "latencies_ms": {},
            }
            try:
                response = client.post(
                    f"{args.base_url.rstrip('/')}/api/v1/agent/community/knowledge/evidence",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "question": row["question"],
                        "topPosts": args.top_posts,
                        "topChunks": args.top_chunks,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                result["evidence"] = [
                    {
                        "chunk_id": item.get("chunkId"),
                        "post_id": item.get("postId"),
                        "title": item.get("title"),
                        "score": item.get("score"),
                        "start_offset": item.get("startOffset"),
                        "end_offset": item.get("endOffset"),
                        "event_version": item.get("eventVersion"),
                    }
                    for item in payload.get("chunks", [])
                ]
                result["candidate_post_count"] = payload.get("candidatePostCount")
                result["latencies_ms"]["embedding"] = [float(payload.get("embeddingLatencyMs", 0))]
                result["latencies_ms"]["chunk_retrieval"] = [
                    float(payload.get("chunkRetrievalLatencyMs", 0))
                ]
            except (httpx.HTTPError, KeyError, ValueError) as error:
                result["error"] = str(error)[:500]
            result["latencies_ms"]["total"] = [round((time.perf_counter() - started) * 1000, 3)]
            runs.append(result)
            print(json.dumps({"query_id": row["query_id"], "evidence_count": len(result["evidence"]), "error": result.get("error")}, ensure_ascii=False))

    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in runs:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
