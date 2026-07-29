from __future__ import annotations

from typing import Any


class CreatorDraftError(RuntimeError):
    code = "CREATOR_DRAFT_ERROR"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorDraftNotFoundError(CreatorDraftError):
    code = "DRAFT_NOT_FOUND"


class CreatorDraftTaskNotFoundError(CreatorDraftError):
    code = "DRAFT_TASK_NOT_FOUND"


class CreatorDraftScopeError(CreatorDraftError):
    code = "DRAFT_SCOPE_VIOLATION"


class CreatorDraftVersionConflictError(CreatorDraftError):
    code = "DRAFT_VERSION_CONFLICT"


class CreatorDraftIdempotencyError(CreatorDraftError):
    code = "DRAFT_IDEMPOTENCY_KEY_REUSED"


class CreatorDraftSourceArtifactError(CreatorDraftError):
    code = "DRAFT_SOURCE_ARTIFACT_INVALID"


class CreatorDraftPersistenceError(CreatorDraftError):
    code = "DRAFT_PERSISTENCE_CONFLICT"
    retryable = True
