from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReasoningTier(StrEnum):
    FAST = "FAST"
    STANDARD = "STANDARD"
    DEEP = "DEEP"
    LEGACY = "LEGACY"


class ReasoningCascadeAudit(BaseModel):
    enabled: bool
    tier: ReasoningTier
    reasons: list[str] = Field(default_factory=list, max_length=20)
    direct_decision: bool = False
    tool_agent_available: bool = False
    context_prefetched: bool = False


def reasoning_cascade_audit_from_state(
    state: Mapping[str, Any],
) -> ReasoningCascadeAudit | None:
    tier = state.get("reasoning_tier")
    if not tier:
        return None
    return ReasoningCascadeAudit(
        enabled=bool(state.get("adaptive_cascade_enabled", False)),
        tier=ReasoningTier(tier),
        reasons=list(state.get("cascade_reasons", [])),
        direct_decision=bool(state.get("cascade_direct_decision", False)),
        tool_agent_available=bool(
            state.get(
                "cascade_tool_agent_available",
                state.get("use_dynamic_tool_agent", False),
            )
        ),
        context_prefetched=bool(state.get("cascade_context_prefetched", False)),
    )
