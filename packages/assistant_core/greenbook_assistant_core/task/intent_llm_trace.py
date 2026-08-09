"""LLM transport and parsing diagnostics for Direct IntentSpec calls."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IntentLLMTrace(BaseModel):
    """One raw LLM response and its local parsing outcome."""

    raw_response_content: str = ""
    finish_reason: str | None = None
    model: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    parse_status: str = "NOT_PARSED"
