from __future__ import annotations

from typing import Any


class ConversationMemory:
    """Manages conversation history, episodic and semantic memory."""

    def __init__(
        self,
        *,
        max_context_chars: int = 16000,
        episodic_enabled: bool = True,
        semantic_enabled: bool = False,
    ) -> None:
        self.max_context_chars = max_context_chars
        self.episodic_enabled = episodic_enabled
        self.semantic_enabled = semantic_enabled

    async def load_history(self, conversation_id: str) -> list[dict[str, Any]]:
        """Load recent messages for a conversation. Override for DB-backed storage."""
        return []

    async def save_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_response: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a conversation turn. Override for DB-backed storage."""
        pass
