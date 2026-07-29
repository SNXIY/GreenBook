r"""Materialize the deterministic 100-case proposed moderation seed dataset.

Run from the repository root:
    $env:PYTHONPATH="src"
    .venv\Scripts\python.exe scripts\build_moderation_eval_seed.py
"""

import argparse

from evals.moderation.dedup import validate_duplicates
from evals.moderation.io import validate_dataset, write_jsonl
from evals.moderation.privacy import validate_privacy
from evals.moderation.seed_data import build_seed_cases


def main(output: str, near_threshold: float) -> None:
    cases = build_seed_cases()
    validate_dataset(cases)
    privacy_report = validate_privacy(cases)
    duplicate_report = validate_duplicates(cases, near_threshold=near_threshold)
    write_jsonl(output, cases)
    print(f"Wrote {len(cases)} PROPOSED seed cases to {output}")
    print(
        f"Privacy warnings: {len(privacy_report.warnings)}; "
        f"intentional near variants: {len(duplicate_report.intentional_variants)}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="evals/candidates/seed-v1.proposed.jsonl",
        help="Destination JSONL path",
    )
    parser.add_argument("--near-threshold", type=float, default=0.88)
    args = parser.parse_args()
    main(args.output, args.near_threshold)
