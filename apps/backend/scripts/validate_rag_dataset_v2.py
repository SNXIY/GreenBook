"""Validate the manually annotated RAG evidence benchmark v2.

Unlike the v1 validator, this contract has no post-qrel-to-chunk fallback.
Every answerable row must carry explicit chunk IDs and an audit trail linking
claims to those chunks.  The chunk snapshot is a read-only export of the
canonical ``post_chunks`` rows used when the dataset was frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any


POST_ID = re.compile(r"^[0-9]+$")
CHUNK_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
DATASET_VERSION = "rag_evidence_v2"
NO_ANSWER = "当前社区资料不足"


def stable_chunk_id(post_id: str, chunk_index: int) -> str:
    digest = bytearray(
        hashlib.md5(f"greenbook:post-chunk:{post_id}:{chunk_index}".encode()).digest()
    )
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            issues.append({"line": line_number, "error": f"INVALID_JSON:{error.msg}"})
            continue
        if not isinstance(value, dict):
            issues.append({"line": line_number, "error": "ROW_NOT_OBJECT"})
            continue
        value["__line__"] = line_number
        rows.append(value)
    return rows, issues


def load_chunk_snapshot(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("evidence_chunks") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("v2 chunk fixture must contain evidence_chunks[]")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("evidence chunk entry must be an object")
        chunk_id = str(entry.get("chunk_id", ""))
        post_id = str(entry.get("post_id", ""))
        chunk_index = entry.get("chunk_index")
        if not CHUNK_ID.fullmatch(chunk_id):
            raise ValueError(f"invalid chunk_id: {chunk_id!r}")
        if not POST_ID.fullmatch(post_id) or int(post_id) <= 0:
            raise ValueError(f"invalid post_id for {chunk_id}: {post_id!r}")
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
            raise ValueError(f"invalid chunk_index for {chunk_id}")
        if stable_chunk_id(post_id, chunk_index).lower() != chunk_id.lower():
            raise ValueError(f"chunk_id does not match post/index: {chunk_id}")
        if chunk_id in result:
            raise ValueError(f"duplicate chunk_id: {chunk_id}")
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"empty canonical content: {chunk_id}")
        result[chunk_id] = entry
    return result


def _post_id(value: Any) -> str | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if POST_ID.fullmatch(text) and int(text) > 0 else None


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def validate(dataset_path: Path, chunk_fixture_path: Path, expected_queries: int = 50) -> dict[str, Any]:
    rows, file_issues = read_jsonl(dataset_path)
    chunks = load_chunk_snapshot(chunk_fixture_path)
    seen_query_ids: set[str] = set()
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    missing_chunks: list[dict[str, Any]] = []
    invalid_query_ids: list[str] = []
    answerable_count = 0
    gold_chunk_count = 0
    gold_index_zero_count = 0
    gold_chunk_ids: set[str] = set()
    gold_post_ids: set[str] = set()
    qrel_count = 0

    for row in rows:
        line = row.get("__line__")
        query_id = row.get("query_id")
        errors: list[str] = []
        if not isinstance(query_id, str) or not query_id.strip():
            errors.append("MISSING_QUERY_ID")
            query_id = ""
        elif query_id in seen_query_ids:
            errors.append("DUPLICATE_QUERY_ID")
        else:
            seen_query_ids.add(query_id)

        query = row.get("query")
        if not isinstance(query, str) or not query.strip():
            errors.append("QUERY_EMPTY")
        elif "\ufffd" in query or any(ord(char) < 32 and char not in "\t" for char in query):
            errors.append("QUERY_NOT_UTF8_CLEAN")
        if row.get("question") != query:
            errors.append("QUESTION_QUERY_MISMATCH")

        if row.get("dataset_version") != DATASET_VERSION:
            errors.append("DATASET_VERSION_MISMATCH")
        category = row.get("category")
        if category not in {"fact", "architecture", "multi_hop", "no_answer"}:
            errors.append("INVALID_CATEGORY")
        if row.get("annotation_method") != "manual_evidence_annotation":
            errors.append("ANNOTATION_METHOD_NOT_EXPLICIT")

        posts = _string_list(row.get("gold_post_ids"))
        chunks_list = _string_list(row.get("gold_chunk_ids"))
        if posts is None:
            errors.append("GOLD_POST_IDS_NOT_ARRAY")
            posts = []
        if chunks_list is None:
            errors.append("GOLD_CHUNK_IDS_NOT_ARRAY")
            chunks_list = []
        if len(posts) != len(set(posts)):
            errors.append("DUPLICATE_GOLD_POST_ID")
        if len(chunks_list) != len(set(chunks_list)):
            errors.append("DUPLICATE_GOLD_CHUNK_ID")
        if any(_post_id(value) is None for value in posts):
            errors.append("INVALID_GOLD_POST_ID")
        if any(not CHUNK_ID.fullmatch(value) for value in chunks_list):
            errors.append("INVALID_GOLD_CHUNK_ID")

        gold_entries = row.get("gold_chunks")
        if not isinstance(gold_entries, list):
            errors.append("GOLD_CHUNKS_NOT_ARRAY")
            gold_entries = []
        entry_ids: list[str] = []
        entry_posts: list[str] = []
        for entry in gold_entries:
            if not isinstance(entry, dict):
                errors.append("INVALID_GOLD_CHUNK_ENTRY")
                continue
            chunk_id = str(entry.get("chunk_id", ""))
            post_id = _post_id(entry.get("post_id"))
            index = entry.get("chunk_index")
            entry_ids.append(chunk_id)
            if post_id is None:
                errors.append("INVALID_GOLD_CHUNK_POST_ID")
                continue
            entry_posts.append(post_id)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                errors.append("INVALID_GOLD_CHUNK_INDEX")
                continue
            if chunk_id not in chunks:
                missing_chunks.append({"query_id": query_id, "chunk_id": chunk_id, "line": line})
                errors.append("MISSING_CHUNK_ID")
                continue
            snapshot = chunks[chunk_id]
            if str(snapshot.get("post_id")) != post_id:
                errors.append("CHUNK_POST_MISMATCH")
            if snapshot.get("chunk_index") != index:
                errors.append("CHUNK_INDEX_MISMATCH")
            if not isinstance(snapshot.get("content"), str) or not snapshot["content"].strip():
                errors.append("CHUNK_CONTENT_EMPTY")
            if snapshot.get("status") != "published" or snapshot.get("visible") != "public":
                errors.append("GOLD_CHUNK_NOT_PUBLIC_SEARCHABLE")

        if set(entry_ids) != set(chunks_list):
            errors.append("GOLD_CHUNK_ID_ENTRY_MISMATCH")
        if set(entry_posts) != set(posts):
            errors.append("GOLD_POST_ID_ENTRY_MISMATCH")

        answer = row.get("gold_answer")
        answerable = category != "no_answer"
        if answerable:
            answerable_count += 1
            if not isinstance(answer, str) or not answer.strip() or answer.strip() == NO_ANSWER:
                errors.append("GOLD_ANSWER_EMPTY")
            if not posts:
                errors.append("ANSWERABLE_WITHOUT_GOLD_POST")
            if not chunks_list:
                errors.append("ANSWERABLE_WITHOUT_GOLD_CHUNK")
            if row.get("annotation_status") != "human_audited":
                errors.append("NOT_HUMAN_AUDITED")
            claims = row.get("evidence_claims")
            if not isinstance(claims, list) or not claims:
                errors.append("EVIDENCE_CLAIMS_MISSING")
            else:
                for claim in claims:
                    if not isinstance(claim, dict) or not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
                        errors.append("INVALID_EVIDENCE_CLAIM")
                        continue
                    claim_ids = _string_list(claim.get("chunk_ids"))
                    if claim_ids is None or not claim_ids or not set(claim_ids).issubset(set(chunks_list)):
                        errors.append("CLAIM_CHUNK_NOT_IN_GOLD")
        else:
            if answer != NO_ANSWER:
                errors.append("NO_ANSWER_TEXT_MISMATCH")
            if posts or chunks_list or gold_entries:
                errors.append("NO_ANSWER_HAS_EVIDENCE")

        qrels = row.get("qrels")
        if not isinstance(qrels, dict):
            errors.append("QRELS_NOT_OBJECT")
        else:
            for raw_post_id, relevance in qrels.items():
                post_id = _post_id(raw_post_id)
                if post_id is None or isinstance(relevance, bool) or not isinstance(relevance, int) or relevance not in (0, 1, 2):
                    errors.append("INVALID_QREL")
                else:
                    qrel_count += 1
            if {str(key) for key, value in qrels.items() if value > 0} != set(posts):
                errors.append("QREL_POSTS_NOT_EXPLICIT_GOLD_POSTS")

        if errors:
            invalid_query_ids.append(query_id)
            invalid_rows.append({"query_id": query_id, "line": line, "errors": sorted(set(errors))})
            continue

        clean = {key: value for key, value in row.items() if key != "__line__"}
        valid_rows.append(clean)
        gold_chunk_count += len(chunks_list)
        gold_index_zero_count += sum(
            1 for entry in gold_entries if isinstance(entry, dict) and entry.get("chunk_index") == 0
        )
        gold_chunk_ids.update(chunks_list)
        gold_post_ids.update(posts)

    total_queries = len(rows)
    coverage = {
        "answerable_queries": answerable_count,
        "answerable_queries_with_valid_annotations": sum(
            1 for row in valid_rows if row.get("category") != "no_answer"
        ),
        "answerable_query_coverage": round(
            sum(1 for row in valid_rows if row.get("category") != "no_answer") / answerable_count,
            6,
        ) if answerable_count else 0.0,
        "gold_chunk_reference_count": gold_chunk_count,
        "unique_gold_chunk_count": len(gold_chunk_ids),
        "unique_gold_post_count": len(gold_post_ids),
    }
    status = "DATASET_INVALID" if file_issues or invalid_rows or total_queries != expected_queries else "VALID"
    if total_queries != expected_queries:
        file_issues.append({"error": f"EXPECTED_QUERY_COUNT:{expected_queries}:ACTUAL:{total_queries}"})
    return {
        "dataset_version": DATASET_VERSION,
        "status": status,
        "dataset": str(dataset_path),
        "chunk_fixture": str(chunk_fixture_path),
        "TOTAL_QUERY_COUNT": total_queries,
        "VALID_QUERY_COUNT": len(valid_rows),
        "VALID_QREL_COUNT": qrel_count,
        "GOLD_CHUNK_COUNT": gold_chunk_count,
        "INDEX_ZERO_GOLD_REFERENCE_COUNT": gold_index_zero_count,
        "NONZERO_GOLD_REFERENCE_COUNT": gold_chunk_count - gold_index_zero_count,
        "AUTOMATIC_INDEX_ZERO_MAPPING_DETECTED": bool(
            gold_chunk_count and gold_index_zero_count == gold_chunk_count
        ),
        "UNIQUE_GOLD_CHUNK_COUNT": len(gold_chunk_ids),
        "ANNOTATION_COVERAGE": coverage,
        "MISSING_FIXTURE_COUNT": 0,
        "MISSING_CHUNK_COUNT": len(missing_chunks),
        "REMOVED_INVALID_COUNT": len(file_issues) + len(invalid_rows),
        "chunk_snapshot_count": len(chunks),
        "file_issues": file_issues,
        "invalid_rows": invalid_rows,
        "missing_chunks": missing_chunks,
        "valid_rows": valid_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path, required=True)
    parser.add_argument("--expected-queries", type=int, default=50)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--clean-output", type=Path)
    parser.add_argument("--fail-on-invalid", action="store_true")
    args = parser.parse_args()
    try:
        report = validate(args.dataset, args.chunk_fixture, args.expected_queries)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        print(json.dumps({"status": "DATASET_INVALID", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.clean_output:
        with args.clean_output.open("w", encoding="utf-8", newline="\n") as output:
            for row in report["valid_rows"]:
                output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    printed = {key: value for key, value in report.items() if key != "valid_rows"}
    print(json.dumps(printed, ensure_ascii=False, indent=2))
    if args.fail_on_invalid and report["status"] != "VALID":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
