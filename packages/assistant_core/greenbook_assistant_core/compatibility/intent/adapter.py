"""Deprecated adapter boundary for historical intent representations.

Do not extend the historical representations. Migration target: IntentSpec.
Active code may depend on this adapter during the compatibility window, but
must not import IntentDraft, IntentElements, or their compilers/builders.
"""

from __future__ import annotations

import json
from typing import Any

from .intent_draft import IntentCompiler, IntentDraft
from .intent_elements import IntentElements, IntentSpecBuilder


def parse_draft(raw: str) -> IntentDraft | None:
    """Parse legacy Draft JSON without exposing its model to active callers."""
    data = _parse_json_object(raw)
    if data is None:
        return None
    try:
        return IntentDraft.model_validate(data)
    except Exception:
        return None


def compile_draft(draft: IntentDraft) -> Any:
    """Compile a legacy Draft into the existing IntentSpec representation."""
    return IntentCompiler().compile(draft)


def parse_elements(raw: str) -> IntentElements | None:
    """Parse legacy Elements JSON without exposing its model to active callers."""
    data = _parse_json_object(raw)
    if data is None:
        return None
    try:
        return IntentElements.model_validate(data)
    except Exception:
        return None


def build_elements(elements: IntentElements) -> Any:
    """Build the existing IntentSpec representation from legacy Elements."""
    return IntentSpecBuilder().build(elements)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    cleaned = (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


__all__ = ["build_elements", "compile_draft", "parse_draft", "parse_elements"]

