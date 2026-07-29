from __future__ import annotations

from typing import Any


class CreatorPublicationError(RuntimeError):
    code = "PUBLICATION_ERROR"

    def __init__(
        self,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorPublicationNotReadyError(CreatorPublicationError):
    code = "PUBLICATION_NOT_READY"


class CreatorPublicationArtifactError(CreatorPublicationError):
    code = "PUBLICATION_ARTIFACT_INVALID"


class CreatorPublicationLockedError(CreatorPublicationError):
    code = "PUBLICATION_LOCKED"
