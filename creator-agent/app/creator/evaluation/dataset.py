from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.creator.evaluation.errors import CreatorEvaluationDatasetError
from app.creator.evaluation.hashing import canonical_sha256
from app.creator.evaluation.models import (
    EvaluationDataset,
    EvaluationObservationSet,
)


_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "email",
    "password",
    "phone",
    "refresh_token",
    "secret",
}


def load_evaluation_dataset(path: str | Path) -> EvaluationDataset:
    return _load_model(path, EvaluationDataset)


def load_evaluation_observations(path: str | Path) -> EvaluationObservationSet:
    return _load_model(path, EvaluationObservationSet)


def dataset_sha256(dataset: EvaluationDataset) -> str:
    return canonical_sha256(dataset)


def _load_model(path: str | Path, model_type):
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CreatorEvaluationDatasetError(
            f"Could not load evaluation file {source}",
            details={"path": str(source), "error": type(exc).__name__},
        ) from exc
    _reject_sensitive_fields(payload, location="$")
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise CreatorEvaluationDatasetError(
            f"Evaluation file {source} does not match its schema",
            details={
                "path": str(source),
                "errors": exc.errors(include_input=False),
            },
        ) from exc


def _reject_sensitive_fields(value: Any, *, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in _SENSITIVE_KEYS:
                raise CreatorEvaluationDatasetError(
                    f"Sensitive field {key!r} is not allowed in evaluation data",
                    details={"location": location, "field": str(key)},
                )
            _reject_sensitive_fields(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_fields(child, location=f"{location}[{index}]")
