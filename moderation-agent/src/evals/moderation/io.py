import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import ValidationError

from evals.moderation.schemas import (
    EvalAnnotationStatus,
    EvalDatasetSplit,
    ModerationEvalCase,
)


class DatasetValidationError(ValueError):
    """Raised when a JSONL dataset violates record or collection-level constraints."""


def load_jsonl(path: str | Path) -> list[ModerationEvalCase]:
    source = Path(path)
    cases: list[ModerationEvalCase] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload: Any = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetValidationError(
                    f"{source}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            try:
                cases.append(ModerationEvalCase.model_validate(payload))
            except ValidationError as exc:
                raise DatasetValidationError(
                    f"{source}:{line_number}: schema validation failed: {exc}"
                ) from exc
    return cases


def write_jsonl(path: str | Path, cases: Iterable[ModerationEvalCase]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized_cases = list(cases)

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        try:
            for case in serialized_cases:
                handle.write(
                    json.dumps(
                        case.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, destination)


def validate_dataset(
    cases: Sequence[ModerationEvalCase],
    *,
    require_gold: bool = False,
) -> None:
    errors: list[str] = []
    seen_case_ids: dict[str, int] = {}
    group_splits: dict[str, EvalDatasetSplit] = {}

    for index, case in enumerate(cases, start=1):
        previous_index = seen_case_ids.get(case.case_id)
        if previous_index is not None:
            errors.append(
                f"record {index}: duplicate case_id {case.case_id!r} "
                f"(first seen at record {previous_index})"
            )
        else:
            seen_case_ids[case.case_id] = index

        previous_split = group_splits.get(case.scenario_group_id)
        if previous_split is not None and previous_split != case.split:
            errors.append(
                f"record {index}: scenario_group_id {case.scenario_group_id!r} "
                f"spans splits {previous_split} and {case.split}"
            )
        else:
            group_splits[case.scenario_group_id] = case.split

        if require_gold:
            if case.annotation.status not in {
                EvalAnnotationStatus.REVIEWED,
                EvalAnnotationStatus.ADJUDICATED,
            }:
                errors.append(
                    f"record {index}: golden data requires REVIEWED or ADJUDICATED status"
                )
            if case.split == EvalDatasetSplit.UNASSIGNED:
                errors.append(f"record {index}: golden data requires an assigned split")

    if not cases:
        errors.append("dataset contains no records")
    if errors:
        raise DatasetValidationError("\n".join(errors))
