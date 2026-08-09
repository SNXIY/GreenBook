"""Backward-compatible import shim for the archived IntentDraft path."""

from ..compatibility.intent.intent_draft import IntentCompiler, IntentDraft

__all__ = ["IntentCompiler", "IntentDraft"]
"""Deprecated import shim.

Do not extend. Migration target: IntentSpec.
"""

