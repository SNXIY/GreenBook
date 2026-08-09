"""Observability records for IntentSpec validation and targeted repair."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IntentValidationTrace(BaseModel):
    """One Direct IntentSpec validation/repair attempt."""

    raw_intent_spec: dict[str, Any] = Field(default_factory=dict)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    repair_triggered: bool = False
    repair_prompt: str | None = None
    repair_response: str | None = None
    final_result: dict[str, Any] | None = None
