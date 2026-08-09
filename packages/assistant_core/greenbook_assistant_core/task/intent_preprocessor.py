"""Lightweight structural hints for Direct IntentSpec extraction.

This module does not interpret actions or construct an IntentSpec. It only
describes surface signals that help the LLM handle long, structured input.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field


class IntentContextHint(BaseModel):
    has_condition: bool = False
    has_multiple_actions: bool = False
    has_number_list: bool = False
    has_approval: bool = False
    has_time_constraint: bool = False
    has_reference: bool = False
    action_keyword_signals: list[str] = Field(default_factory=list)


_ACTION_SIGNALS = (
    "\u521b\u5efa", "\u5199", "\u751f\u6210", "\u641c\u7d22", "\u67e5\u627e",
    "\u5206\u6790", "\u603b\u7ed3", "\u4fee\u6539", "\u4f18\u5316", "\u66f4\u65b0",
    "\u53d1\u5e03", "\u67e5\u8be2", "\u67e5\u770b", "create", "write",
    "generate", "search", "analyze", "analyse", "update", "modify",
    "improve", "optimize", "publish", "query", "list",
)


def build_intent_context_hint(text: str) -> IntentContextHint:
    """Extract only surface-level structure signals from *text*."""
    condition = bool(re.search(
        r"\u5982\u679c|\u6709\u5219|\u6ca1\u6709|\u5426\u5219|\u65e0\u5219|"
        r"if\b|otherwise|else\b|when\b",
        text,
        re.IGNORECASE,
    ))
    number_list = bool(re.search(
        r"(?m)^\s*(?:\d+[.)]|[\u4e00-\u5341]+[\u3001.)])\s*",
        text,
    ))
    signals = [signal for signal in _ACTION_SIGNALS if signal.lower() in text.lower()]
    # Preserve signal order while removing duplicates.
    signals = list(dict.fromkeys(signals))
    return IntentContextHint(
        has_condition=condition,
        has_multiple_actions=number_list or len(signals) >= 2,
        has_number_list=number_list,
        has_approval=bool(re.search(
            r"\u786e\u8ba4|\u5ba1\u6838|\u901a\u8fc7\u540e|\u53d1\u5e03\u524d|"
            r"approve|approval|confirm|review",
            text,
            re.IGNORECASE,
        )),
        has_time_constraint=bool(re.search(
            r"\d+\s*(?:\u5206\u949f|\u5c0f\u65f6|\u5929|minutes?|hours?|days?)|"
            r"\u660e\u5929|\u4eca\u5929|\u4e4b\u540e|\u540e|tomorrow|tonight|"
            r"\b\d{1,2}:\d{2}\b",
            text,
            re.IGNORECASE,
        )),
        has_reference=bool(re.search(
            r"\u4e4b\u524d|\u65e7|\u521a\u624d|\u4e0a\u6b21|\u521a\u521a|\u521a\u624d|"
            r"previous|earlier|recent|last|just now",
            text,
            re.IGNORECASE,
        )),
        action_keyword_signals=signals,
    )
