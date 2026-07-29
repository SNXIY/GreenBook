from __future__ import annotations


class CreatorMemoryError(RuntimeError):
    code = "CREATOR_MEMORY_ERROR"

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorMemoryConflictError(CreatorMemoryError):
    code = "CREATOR_MEMORY_VERSION_CONFLICT"


class CreatorMemoryUnavailableError(CreatorMemoryError):
    code = "CREATOR_MEMORY_UNAVAILABLE"


class CreatorMemoryIntegrityError(CreatorMemoryError):
    code = "CREATOR_MEMORY_INTEGRITY_ERROR"
