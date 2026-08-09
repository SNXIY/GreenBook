"""Capability domain models.

A Capability is a named, typed unit of work that the system can perform.
It sits *above* MCP tools — one capability may map to one tool, zero tools
(pure LLM reasoning), or multiple tools (composite).
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


class RiskLevel(StrEnum):
    READ = "READ"              # read-only, no side effects
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"  # write, safe to retry
    DESTRUCTIVE_WRITE = "DESTRUCTIVE_WRITE"  # write, requires approval


class CapabilityInput(BaseModel):
    """Description of what a capability needs as input."""
    required: list[str] = []       # ["query", "topic", …]
    optional: list[str] = []       # ["sort", "page", …]


class Capability(BaseModel):
    """A named unit of work the Assistant can perform.

    Each capability has a unique *name*, belongs to a *category*, and
    optionally maps to one or more MCP *tools*.  Capabilities with an
    empty *tools* list are pure-LLM reasoning steps (e.g. ANALYZE_CONTENT).
    """

    name: str                                        # SEARCH_COMMUNITY
    description: str                                  # human-readable
    category: CapabilityCategory

    # ── tool mapping ──
    tools: list[str] = []                             # MCP tool names (dot-format)
    is_llm_step: bool = False                         # pure-LLM reasoning step

    # ── input / output ──
    inputs: CapabilityInput = Field(default_factory=CapabilityInput)
    output_artifact_type: str = ""                    # SEARCH_RESULT | DRAFT | …

    # ── risk ──
    risk_level: RiskLevel = RiskLevel.READ
    requires_approval: bool = False
    side_effect: bool = False                         # true when external state changes

    # ── hints ──
    parallelizable: bool = False                      # can run alongside other caps


class CapabilityMatch(BaseModel):
    """Result of resolving a requirement to a capability."""
    requirement: dict[str, Any] = {}
    capability: Capability | None = None
    confidence: float = 0.0
    error: str = ""
