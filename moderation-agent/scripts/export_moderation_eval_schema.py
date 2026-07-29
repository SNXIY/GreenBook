r"""Export the moderation evaluation record as a standalone JSON Schema.

Run from the repository root:
    $env:PYTHONPATH="src"
    .venv\Scripts\python.exe scripts\export_moderation_eval_schema.py
"""

import argparse
import json
from pathlib import Path

from evals.moderation.schemas import ModerationEvalCase


def main(output: str) -> None:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    schema = ModerationEvalCase.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:content-moderation:schemas:moderation-eval-case:v1"
    destination.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote JSON Schema to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="evals/schema/moderation-eval-case-v1.schema.json",
    )
    args = parser.parse_args()
    main(args.output)
