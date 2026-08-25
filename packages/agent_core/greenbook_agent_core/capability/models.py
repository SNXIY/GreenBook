"""Semantic capability catalog models.

Capabilities describe what the Agent can do.  Concrete tool policy is owned
by ``greenbook_contracts.ToolMetadata`` and is intentionally absent here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CapabilityCategory(StrEnum):
    SEARCH = "SEARCH"
    ANALYZE = "ANALYZE"
    CREATE = "CREATE"
    VALIDATE = "VALIDATE"
    PUBLISH = "PUBLISH"
    INTERACT = "INTERACT"


class CapabilityInput(BaseModel):
    """Semantic input names expected by a capability."""

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    """A semantic unit of work the GreenBook Agent can perform."""

    name: str
    description: str
    category: CapabilityCategory
    tools: list[str] = Field(default_factory=list)
    is_llm_step: bool = False
    inputs: CapabilityInput = Field(default_factory=CapabilityInput)
    output_artifact_type: str = ""
    tags: list[str] = Field(default_factory=list)
    parallelizable: bool = False
    # Explicit completion kind (DIRECT_RESULT | RESOURCE_MUTATION |
    # GROUNDED_SYNTHESIS).  When set, it wins over the is_llm_step default so
    # the result semantics live with the capability, not a heuristic.
    result_requirement: str = ""


class CapabilityMatch(BaseModel):
    """Result of resolving a requirement to a capability."""

    requirement: dict[str, Any] = Field(default_factory=dict)
    capability: Capability | None = None
    confidence: float = 0.0
    error: str = ""
