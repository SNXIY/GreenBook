from __future__ import annotations

from typing import Any


class CreatorApiError(RuntimeError):
    code = "CREATOR_API_ERROR"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorApiUnavailableError(CreatorApiError):
    code = "CREATOR_API_UNAVAILABLE"
    retryable = True


class CreatorEventCursorError(CreatorApiError):
    code = "EVENT_CURSOR_INVALID"


class CreatorTaskCursorError(CreatorApiError):
    code = "TASK_CURSOR_INVALID"


class CreatorArtifactNotFoundError(CreatorApiError):
    code = "ARTIFACT_NOT_FOUND"


class CreatorDraftListNotFoundError(CreatorApiError):
    code = "DRAFT_NOT_FOUND"
