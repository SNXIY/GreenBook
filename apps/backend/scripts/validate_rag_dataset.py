"""Validate and freeze the RAG evidence benchmark contract.

The validator treats malformed questions, malformed qrels, and missing post or
chunk fixtures as DATASET_INVALID.  Only a valid, public/searchable fixture
entry can become a retrieval miss during evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from validate_retrieval_dataset import (
    POST_ID,
    load_fixture,
    load_jsonl,
    valid_query_text,
)


def load_chunk_fixture(path: Path | None) -> dict[str, set[int]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("chunks", raw) if isinstance(raw, dict) else raw
    if isinstance(entries, dict):
        result: dict[str, set[int]] = {}
        for post_id, value in entries.items():
            if isinstance(value, dict):
                indices = value.get("chunk_indices", [])
                if not indices and isinstance(value.get("chunk_count"), int):
                    indices = list(range(value["chunk_count"]))
            else:
                indices = value
            if not POST_ID.fullmatch(str(post_id)) or not isinstance(indices, list):
                raise ValueError(f"invalid chunk fixture entry: {post_id!r}")
            result[str(post_id)] = {int(index) for index in indices}
        return result
    if not isinstance(entries, list):
        raise ValueError("chunk fixture must be a mapping or chunks[] list")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("chunk fixture entry must be an object")
        post_id = str(entry.get("post_id", entry.get("postId", "")))
        if not POST_ID.fullmatch(post_id):
            raise ValueError(f"invalid chunk fixture post_id: {post_id!r}")
        indices = entry.get("chunk_indices", entry.get("chunkIndices", []))
        if not isinstance(indices, list):
            raise ValueError(f"invalid chunk_indices for {post_id}")
        result[post_id] = {int(index) for index in indices}
    return result


def _post_id(value: Any) -> str | None:
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return text if POST_ID.fullmatch(text) and int(text) > 0 else None


def validate(
    dataset_path: Path,
    fixture_path: Path,
    chunk_fixture_path: Path | None = None,
) -> dict[str, Any]:
    rows, file_issues = load_jsonl(dataset_path)
    fixture = load_fixture(fixture_path)
    chunk_fixture = load_chunk_fixture(chunk_fixture_path)
    seen_queries: set[str] = set()
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    missing_fixture: list[dict[str, Any]] = []
    missing_chunk_fixture: list[dict[str, Any]] = []
    non_searchable: list[dict[str, Any]] = []
    valid_qrel_count = 0

    for row in rows:
        line = row.get("__line__")
        query_id = row.get("query_id")
        errors: list[str] = []
        if not isinstance(query_id, str) or not query_id.strip():
            errors.append("MISSING_QUERY_ID")
        elif query_id in seen_queries:
            errors.append("DUPLICATE_QUERY_ID")
        else:
            seen_queries.add(query_id)

        query_ok, query_error = valid_query_text(row.get("question", row.get("query")))
        if not query_ok and query_error:
            errors.append(query_error)

        raw_qrels = row.get("qrels")
        if not isinstance(raw_qrels, dict):
            errors.append("QRELS_NOT_OBJECT")
            raw_qrels = {}
        qrels: dict[str, int] = {}
        qrel_status: dict[str, dict[str, Any]] = {}
        for raw_id, raw_grade in raw_qrels.items():
            post_id = _post_id(raw_id)
            if post_id is None:
                errors.append("INVALID_POST_ID")
                continue
            if isinstance(raw_grade, bool) or not isinstance(raw_grade, int) or raw_grade not in (0, 1, 2):
                errors.append("INVALID_RELEVANCE")
                continue
            fixture_row = fixture.get(post_id)
            if fixture_row is None or not fixture_row["exists"]:
                missing = {"query_id": query_id, "post_id": post_id, "relevance": raw_grade, "line": line}
                missing_fixture.append(missing)
                errors.append("MISSING_FIXTURE")
                continue
            qrels[post_id] = raw_grade
            qrel_status[post_id] = {
                "exists": fixture_row["exists"],
                "public": fixture_row["public"],
                "searchable": fixture_row["searchable"],
                "status": fixture_row["status"],
                "visible": fixture_row["visible"],
                "event_version": fixture_row["event_version"],
            }
            valid_qrel_count += 1
            if raw_grade > 0 and not fixture_row["searchable"]:
                non_searchable.append({
                    "query_id": query_id,
                    "post_id": post_id,
                    "relevance": raw_grade,
                    "line": line,
                    "fixture": fixture_row,
                })

        raw_gold = row.get("gold_chunks", [])
        if not isinstance(raw_gold, list):
            errors.append("GOLD_CHUNKS_NOT_ARRAY")
            raw_gold = []
        gold_chunks: list[dict[str, Any]] = []
        seen_gold: set[tuple[str, int]] = set()
        for entry in raw_gold:
            if not isinstance(entry, dict):
                errors.append("INVALID_GOLD_CHUNK")
                continue
            post_id = _post_id(entry.get("post_id", entry.get("postId")))
            indices = entry.get("chunk_indices", entry.get("chunkIndices"))
            if indices is None and "chunk_index" in entry:
                indices = [entry.get("chunk_index")]
            if post_id is None:
                errors.append("INVALID_GOLD_POST_ID")
                continue
            if not isinstance(indices, list) or not indices:
                errors.append("INVALID_GOLD_CHUNK_INDICES")
                continue
            if post_id not in qrels or qrels[post_id] <= 0:
                errors.append("GOLD_CHUNK_NOT_IN_RELEVANT_QRELS")
                continue
            # A qrel may intentionally describe a draft/private post so the
            # frozen benchmark records the truth state. It is not an expected
            # public evidence chunk and must not become a retrieval miss.
            if not qrel_status.get(post_id, {}).get("searchable", False):
                continue
            for raw_index in indices:
                if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                    errors.append("INVALID_CHUNK_INDEX")
                    continue
                key = (post_id, raw_index)
                if key in seen_gold:
                    errors.append("DUPLICATE_GOLD_CHUNK")
                    continue
                seen_gold.add(key)
                if chunk_fixture_path is not None and raw_index not in chunk_fixture.get(post_id, set()):
                    missing = {
                        "query_id": query_id,
                        "post_id": post_id,
                        "chunk_index": raw_index,
                        "line": line,
                    }
                    missing_chunk_fixture.append(missing)
                    errors.append("MISSING_CHUNK_FIXTURE")
                    continue
                gold_chunks.append({"post_id": post_id, "chunk_index": raw_index})

        if errors:
            invalid_rows.append({
                "query_id": query_id,
                "line": line,
                "errors": sorted(set(errors)),
            })
            continue

        clean = {key: value for key, value in row.items() if key != "__line__"}
        clean["question"] = str(row.get("question", row.get("query"))).strip()
        clean["qrels"] = qrels
        clean["qrel_status"] = qrel_status
        clean["gold_chunks"] = gold_chunks
        valid_rows.append(clean)

    removed_invalid = len(file_issues) + len(invalid_rows)
    return {
        "dataset": str(dataset_path),
        "fixture": str(fixture_path),
        "chunk_fixture": str(chunk_fixture_path) if chunk_fixture_path else None,
        "status": "DATASET_INVALID" if file_issues or invalid_rows else "VALID",
        "VALID_QUERY_COUNT": len(valid_rows),
        "VALID_QREL_COUNT": valid_qrel_count,
        "REMOVED_INVALID_COUNT": removed_invalid,
        "MISSING_FIXTURE_COUNT": len(missing_fixture),
        "MISSING_CHUNK_FIXTURE_COUNT": len(missing_chunk_fixture),
        "NON_SEARCHABLE_QREL_COUNT": len(non_searchable),
        "fixture_post_count": len(fixture),
        "chunk_fixture_post_count": len(chunk_fixture),
        "file_issues": file_issues,
        "invalid_rows": invalid_rows,
        "missing_fixture": missing_fixture,
        "missing_chunk_fixture": missing_chunk_fixture,
        "non_searchable_qrels": non_searchable,
        "valid_rows": valid_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--chunk-fixture", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--clean-output", type=Path)
    parser.add_argument("--fail-on-invalid", action="store_true")
    args = parser.parse_args()
    try:
        report = validate(args.dataset, args.fixture, args.chunk_fixture)
    except (OSError, ValueError, json.JSONDecodeError) as error:
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
