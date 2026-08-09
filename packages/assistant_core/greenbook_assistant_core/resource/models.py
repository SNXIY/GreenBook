"""Resource domain models — ResourceRequest, ResourceTarget, ResourceResolutionResult."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ResourceOperation(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    QUERY = "QUERY"


class ResourceType(StrEnum):
    CONTENT_DRAFT = "CONTENT_DRAFT"
    SCHEDULE = "SCHEDULE"
    POST = "POST"


class ResourceRequest(BaseModel):
    """User's intent toward one resource — carried on TaskIntent.

    Represents WHAT the user wants to do, not HOW to resolve it.
    Resolution (finding concrete resource_id) happens later in
    ResourceResolver.
    """

    operation: ResourceOperation
    resource_type: ResourceType
    hint: str | None = None            # "Java文章", "刚才那个"
    task_id: str | None = None         # explicit when known


class ResourceTarget(BaseModel):
    """A resolved resource — output of ResourceResolver.

    When *operation* is CREATE, *resource_id* is None (will be created).
    When *is_ambiguous* is True, *candidates* lists alternatives.
    """

    operation: ResourceOperation
    resource_type: ResourceType
    resource_id: str | None = None     # draft_id, schedule_id, post_id, …
    task_id: str | None = None         # owning Task
    hint: str | None = None
    confidence: float = 0.0
    match_reason: str = ""
    is_ambiguous: bool = False
    candidates: list[str] = []         # other resource_ids


class ResourceResolutionResult(BaseModel):
    """Complete output of ResourceResolver.resolve()."""

    targets: list[ResourceTarget] = []
    needs_clarification: bool = False
    errors: list[str] = []
