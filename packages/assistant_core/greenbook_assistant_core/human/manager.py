"""HumanInteractionManager — pause and resume execution for human input.

Phase 6.5: infrastructure only — no integration with Runtime yet.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .models import (
    HumanInteractionRequest,
    HumanInteractionResponse,
    InteractionStatus,
    InteractionType,
)
from .store import InteractionStore

logger = logging.getLogger(__name__)


class HumanInteractionManager:
    """Manage pause/resume lifecycle for human interactions."""

    def __init__(self, store: InteractionStore | None = None) -> None:
        self._store = store or InteractionStore()

    # ── pause ───────────────────────────────────────────────────

    def pause(
        self,
        *,
        execution_id: str = "",
        task_id: str = "",
        step_id: str = "",
        type: InteractionType = InteractionType.APPROVAL,
        question: str = "",
        options: list[dict] | None = None,
        context: dict | None = None,
        timeout_minutes: int = 5,
    ) -> HumanInteractionRequest:
        """Create a pending interaction and pause execution."""
        from datetime import timedelta

        request = HumanInteractionRequest(
            execution_id=execution_id,
            task_id=task_id,
            step_id=step_id,
            type=type,
            question=question,
            options=options or [],
            context=context or {},
            expires_at=(
                datetime.now(UTC) + timedelta(minutes=timeout_minutes)
            ).isoformat(),
        )
        return self._store.save(request)

    # ── resume ──────────────────────────────────────────────────

    def resume(
        self, interaction_id: str, response: HumanInteractionResponse,
    ) -> HumanInteractionRequest | None:
        """Resume after user response. Returns the original request."""
        request = self._store.find_by_id(interaction_id)
        if request is None:
            logger.warning("Interaction not found: %s", interaction_id)
            return None

        if request.status != InteractionStatus.PENDING:
            logger.warning(
                "Interaction %s already resolved: %s",
                interaction_id, request.status.value,
            )
            return None

        if self._is_expired(request):
            self._store.expire(interaction_id)
            logger.info("Interaction %s expired", interaction_id)
            return None

        return self._store.update(interaction_id, response)

    # ── expiry ──────────────────────────────────────────────────

    def expire_stale(self) -> list[str]:
        """Expire all pending interactions past their expires_at.
        Returns list of expired interaction_ids.
        """
        expired: list[str] = []
        for req in self._store.find_pending():
            if self._is_expired(req):
                self._store.expire(req.interaction_id)
                expired.append(req.interaction_id)
        return expired

    @staticmethod
    def _is_expired(request: HumanInteractionRequest) -> bool:
        try:
            expires = datetime.fromisoformat(request.expires_at)
        except (ValueError, TypeError):
            return False
        return datetime.now(UTC) > expires

    # ── queries ─────────────────────────────────────────────────

    @property
    def store(self) -> InteractionStore:
        return self._store
