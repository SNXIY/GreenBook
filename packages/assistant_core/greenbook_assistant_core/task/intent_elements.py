"""Backward-compatible import shim for the archived IntentElements path."""

from ..compatibility.intent.intent_elements import (
    ActionMention,
    ConditionMention,
    IntentElements,
    IntentSpecBuilder,
)

__all__ = [
    "ActionMention",
    "ConditionMention",
    "IntentElements",
    "IntentSpecBuilder",
]
"""Deprecated import shim.

Do not extend. Migration target: IntentSpec.
"""

