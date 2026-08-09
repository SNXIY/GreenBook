from __future__ import annotations

from typing import Any


class CreatorRetrievalError(RuntimeError):
    code = "CREATOR_RETRIEVAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details or {}


class CreatorRetrievalIntegrityError(CreatorRetrievalError):
    code = "CREATOR_RETRIEVAL_INTEGRITY_ERROR"


class CreatorRetrievalUnavailableError(CreatorRetrievalError):
    code = "CREATOR_RETRIEVAL_UNAVAILABLE"


class CreatorRetrievalAuthorizationError(CreatorRetrievalError):
    code = "CREATOR_RETRIEVAL_NOT_AUTHORIZED"
