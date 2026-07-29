from __future__ import annotations

from typing import Any


class CreatorCommunityError(RuntimeError):
    code = "COMMUNITY_PROVIDER_ERROR"
    retryable = False

    def __init__(
        self,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorCommunityNotFoundError(CreatorCommunityError):
    code = "COMMUNITY_RESOURCE_NOT_FOUND"


class CreatorCommunityScopeError(CreatorCommunityError):
    code = "COMMUNITY_SCOPE_VIOLATION"


class CreatorCommunityUnavailableError(CreatorCommunityError):
    code = "COMMUNITY_PROVIDER_UNAVAILABLE"
    retryable = True


class CreatorCommunityCapabilityError(CreatorCommunityError):
    code = "COMMUNITY_CAPABILITY_UNAVAILABLE"
