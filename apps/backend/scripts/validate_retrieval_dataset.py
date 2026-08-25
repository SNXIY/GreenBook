"""Validate and freeze the retrieval-quality qrels contract.

The validator deliberately separates malformed benchmark data from a provider
miss.  Fixture status is supplied as a small, versioned JSON snapshot so a
benchmark can be reproduced without treating a deleted/private/draft post as
an expected public-search hit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


POST_ID = re.compile(r"^[0-9]+$")


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load JSONL with strict UTF-8 and return rows plus structural issues."""
    issues: list[dict[str, Any]] = []
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            issues.append({"line": 1, "code": "UTF8_BOM"})
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        return [], [{"line": error.start + 1, "code": "DATASET_INVALID_UTF8",
                     "detail": str(error)}]

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            issues.append({"line": line_number, "code": "EMPTY_LINE"})
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            issues.append({"line": line_number, "code": "INVALID_JSON",
                           "detail": str(error)})
            continue
        if not isinstance(value, dict):
            issues.append({"line": line_number, "code": "ROW_NOT_OBJECT"})
            continue
        value["__line__"] = line_number
        rows.append(value)
    return rows, issues


def load_fixture(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("posts", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("fixture must be a JSON array or an object with posts[]")

    fixture: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("fixture entry must be an object")
        post_id = entry.get("post_id", entry.get("id"))
        if post_id is None or not POST_ID.fullmatch(str(post_id)):
            raise ValueError(f"fixture post_id is not numeric: {post_id!r}")
        key = str(post_id)
        status = entry.get("status")
        visible = entry.get("visible", entry.get("visibility"))
        public = bool(entry.get("public", status == "published" and visible == "public"))
        searchable = bool(entry.get("searchable", public))
        fixture[key] = {
            "post_id": key,
            "exists": bool(entry.get("exists", True)),
            "status": status,
            "visible": visible,
            "public": public,
            "searchable": searchable,
            "event_version": entry.get("event_version"),
        }
    return fixture


def valid_query_text(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, str) or not value.strip():
        return False, "EMPTY_QUERY"
    if "\ufffd" in value:
        return False, "GARBLED_QUERY_REPLACEMENT_CHAR"
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return False, "GARBLED_QUERY_CONTROL_CHAR"
    return True, None


def validate(dataset_path: Path, fixture_path: Path) -> dict[str, Any]:
    rows, file_issues = load_jsonl(dataset_path)
    fixture = load_fixture(fixture_path)
    seen_queries: set[str] = set()
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    missing_fixture: list[dict[str, Any]] = []
    non_searchable: list[dict[str, Any]] = []
    valid_qrel_count = 0
    searchable_qrel_count = 0

    for row in rows:
        line = row.get("__line__")
        query_id = row.get("query_id")
        row_errors: list[str] = []
        if not isinstance(query_id, str) or not query_id.strip():
            row_errors.append("MISSING_QUERY_ID")
        elif query_id in seen_queries:
            row_errors.append("DUPLICATE_QUERY_ID")
        else:
            seen_queries.add(query_id)

        text_ok, text_error = valid_query_text(row.get("query"))
        if not text_ok and text_error:
            row_errors.append(text_error)

        qrels = row.get("qrels")
        if not isinstance(qrels, dict):
            row_errors.append("QRELS_NOT_OBJECT")
            qrels = {}

        normalized_qrels: dict[str, int] = {}
        row_missing: list[dict[str, Any]] = []
        row_non_searchable: list[dict[str, Any]] = []
        for raw_post_id, raw_grade in qrels.items():
            post_id = str(raw_post_id)
            if not POST_ID.fullmatch(post_id) or int(post_id) <= 0:
                row_errors.append("INVALID_POST_ID")
                continue
            if isinstance(raw_grade, bool) or not isinstance(raw_grade, int) \
                    or raw_grade not in (0, 1, 2):
                row_errors.append("INVALID_RELEVANCE")
                continue
            fixture_row = fixture.get(post_id)
            if fixture_row is None or not fixture_row["exists"]:
                missing = {"query_id": query_id, "post_id": post_id,
                           "relevance": raw_grade, "line": line}
                missing_fixture.append(missing)
                row_missing.append(missing)
                continue
            normalized_qrels[post_id] = raw_grade
            valid_qrel_count += 1
            if fixture_row["searchable"]:
                searchable_qrel_count += 1
            else:
                status = {"query_id": query_id, "post_id": post_id,
                          "relevance": raw_grade, "line": line,
                          "fixture": fixture_row}
                non_searchable.append(status)
                row_non_searchable.append(status)

        if row_missing:
            row_errors.append("MISSING_FIXTURE")
        if row_errors:
            invalid_rows.append({"query_id": query_id, "line": line,
                                 "errors": sorted(set(row_errors)),
                                 "missing_fixture": row_missing})
            continue

        clean_row = {key: value for key, value in row.items() if key != "__line__"}
        clean_row["qrels"] = normalized_qrels
        valid_rows.append(clean_row)

    invalid_count = len(file_issues) + len(invalid_rows)
    report = {
        "dataset": str(dataset_path),
        "fixture": str(fixture_path),
        "status": "DATASET_INVALID" if file_issues or invalid_rows else "VALID",
        "VALID_QUERY_COUNT": len(valid_rows),
        "VALID_QREL_COUNT": valid_qrel_count,
        "SEARCHABLE_QREL_COUNT": searchable_qrel_count,
        "REMOVED_INVALID_COUNT": invalid_count,
        "MISSING_FIXTURE_COUNT": len(missing_fixture),
        "NON_SEARCHABLE_QREL_COUNT": len(non_searchable),
        "fixture_post_count": len(fixture),
        "file_issues": file_issues,
        "invalid_rows": invalid_rows,
        "missing_fixture": missing_fixture,
        "non_searchable_qrels": non_searchable,
        "valid_rows": valid_rows,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--clean-output", type=Path,
                        help="Optional JSONL output for valid rows only")
    parser.add_argument("--fail-on-invalid", action="store_true")
    args = parser.parse_args()

    try:
        report = validate(args.dataset, args.fixture)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "DATASET_INVALID", "error": str(error)},
                         ensure_ascii=False, indent=2))
        return 2

    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
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
