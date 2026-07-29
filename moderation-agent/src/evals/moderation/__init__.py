"""Typed datasets and utilities for moderation evaluation."""

from evals.moderation.io import DatasetValidationError, load_jsonl, validate_dataset, write_jsonl
from evals.moderation.privacy import PrivacyValidationError, validate_privacy
from evals.moderation.schemas import (
    EvalAnnotation,
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalContext,
    EvalDatasetSplit,
    EvalEvidenceField,
    EvalEvidenceSpan,
    EvalInput,
    EvalLabel,
    EvalPolicyReference,
    EvalPolicySnapshot,
    EvalPrivacyDeclaration,
    EvalPrivacyMode,
    ModerationEvalCase,
)

__all__ = [
    "DatasetValidationError",
    "EvalAnnotation",
    "EvalAnnotationStatus",
    "EvalCaseSource",
    "EvalContext",
    "EvalDatasetSplit",
    "EvalEvidenceField",
    "EvalEvidenceSpan",
    "EvalInput",
    "EvalLabel",
    "EvalPolicyReference",
    "EvalPolicySnapshot",
    "EvalPrivacyDeclaration",
    "EvalPrivacyMode",
    "ModerationEvalCase",
    "PrivacyValidationError",
    "load_jsonl",
    "validate_dataset",
    "validate_privacy",
    "write_jsonl",
]
