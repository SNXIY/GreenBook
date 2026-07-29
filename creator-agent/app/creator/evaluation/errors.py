from __future__ import annotations

from typing import Any


class CreatorEvaluationError(RuntimeError):
    code = "CREATOR_EVALUATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details or {}


class CreatorEvaluationDatasetError(CreatorEvaluationError):
    code = "CREATOR_EVALUATION_DATASET_ERROR"


class CreatorEvaluationConflictError(CreatorEvaluationError):
    code = "CREATOR_EVALUATION_CONFLICT"


class CreatorEvaluationSnapshotError(CreatorEvaluationError):
    code = "CREATOR_EVALUATION_SNAPSHOT_ERROR"


class CreatorEvaluationJudgeError(CreatorEvaluationError):
    code = "CREATOR_EVALUATION_JUDGE_ERROR"
