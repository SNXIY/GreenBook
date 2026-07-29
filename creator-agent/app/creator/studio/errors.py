from __future__ import annotations

from typing import Any


class CreatorStudioError(RuntimeError):
    code = "CREATOR_STUDIO_ERROR"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorStudioNotFoundError(CreatorStudioError):
    code = "STUDIO_RESOURCE_NOT_FOUND"


class CreatorStudioScopeError(CreatorStudioError):
    code = "STUDIO_SCOPE_VIOLATION"


class CreatorStudioConflictError(CreatorStudioError):
    code = "STUDIO_VERSION_CONFLICT"


class CreatorStudioSuggestionStaleError(CreatorStudioConflictError):
    code = "SUGGESTION_STALE"


class CreatorStudioInvalidSelectionError(CreatorStudioError):
    code = "INVALID_TEXT_SELECTION"


class CreatorStudioModelError(CreatorStudioError):
    code = "STUDIO_MODEL_CALL_FAILED"
    retryable = True

