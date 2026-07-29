"""Validate a moderation evaluation JSONL file before review or release.

Run from the repository root:
    $env:PYTHONPATH="src"
    .venv\\Scripts\\python.exe scripts\\validate_moderation_eval_dataset.py `
        evals\\candidates\\seed-v1.proposed.jsonl
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence

from evals.moderation.dedup import (
    DuplicateMatch,
    DuplicateValidationError,
    inspect_duplicates,
)
from evals.moderation.io import DatasetValidationError, load_jsonl, validate_dataset
from evals.moderation.privacy import validate_privacy


def main(args: argparse.Namespace) -> int:
    try:
        cases = load_jsonl(args.dataset)
        validate_dataset(cases, require_gold=args.gold)
        privacy_report = validate_privacy(cases)
        duplicate_report = inspect_duplicates(
            cases,
            near_threshold=args.near_threshold,
            ngram_size=args.ngram_size,
        )
        if duplicate_report.exact:
            raise DuplicateValidationError(_format_duplicates(duplicate_report.exact))
        if duplicate_report.near and args.near_duplicates == "error":
            raise DuplicateValidationError(_format_duplicates(duplicate_report.near))
    except (DatasetValidationError, OSError) as exc:
        print(f"VALIDATION FAILED\n{exc}", file=sys.stderr)
        return 1

    statuses = Counter(case.annotation.status.value for case in cases)
    risks = Counter(
        case.label.primary_risk_type.value
        for case in cases
        if case.label is not None
    )
    actions = Counter(
        case.label.expected_action.value for case in cases if case.label is not None
    )
    splits = Counter(case.split.value for case in cases)
    print(f"VALID {args.dataset}: {len(cases)} records")
    print(f"Annotation status: {_format_counter(statuses)}")
    print(f"Risk labels: {_format_counter(risks)}")
    print(f"Expected actions: {_format_counter(actions)}")
    print(f"Splits: {_format_counter(splits)}")
    print(
        f"Privacy warnings: {len(privacy_report.warnings)}; "
        f"exact duplicates: {len(duplicate_report.exact)}; "
        f"cross-scenario near duplicates: {len(duplicate_report.near)}; "
        f"within-scenario near variants: {len(duplicate_report.intentional_variants)}"
    )
    for warning in privacy_report.warnings:
        print(
            f"WARNING {warning.case_id} {warning.path}: {warning.message} "
            f"({warning.kind}={warning.redacted_value})"
        )
    if duplicate_report.near and args.near_duplicates == "warning":
        print("WARNING cross-scenario near duplicates:")
        print(_format_duplicates(duplicate_report.near))
    return 0


def _format_counter(counter: Counter[str]) -> str:
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter))


def _format_duplicates(matches: Sequence[DuplicateMatch]) -> str:
    return "\n".join(
        f"{match.kind}: {match.left_case_id} <-> {match.right_case_id} "
        f"(similarity={match.similarity:.3f})"
        for match in matches
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="JSONL dataset to validate")
    parser.add_argument(
        "--gold",
        action="store_true",
        help="Require REVIEWED/ADJUDICATED labels and assigned dataset splits",
    )
    parser.add_argument("--near-threshold", type=float, default=0.88)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument(
        "--near-duplicates",
        choices=("error", "warning"),
        default="error",
        help="Treat cross-scenario near duplicates as errors or warnings",
    )
    raise SystemExit(main(parser.parse_args()))
