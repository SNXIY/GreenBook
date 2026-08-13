"""InteractionStore — in-memory persistence for HumanInteractionRequest.

Phase 6.5: in-memory.  Phase 7+ migrates to PostgreSQL.
"""

from __future__ import annotations

from .models import HumanInteractionRequest, HumanInteractionResponse


class InteractionStore:
    """CRUD for HumanInteractionRequest."""

    def __init__(self) -> None:
        self._requests: dict[str, HumanInteractionRequest] = {}

    def save(self, request: HumanInteractionRequest) -> HumanInteractionRequest:
        self._requests[request.interaction_id] = request
        return request

    def find_by_id(self, interaction_id: str) -> HumanInteractionRequest | None:
        return self._requests.get(interaction_id)

    def find_by_execution(self, execution_id: str) -> list[HumanInteractionRequest]:
        return [
            r for r in self._requests.values()
            if r.execution_id == execution_id
        ]

    def find_pending(self) -> list[HumanInteractionRequest]:
        from .models import InteractionStatus
        return [
            r for r in self._requests.values()
            if r.status == InteractionStatus.PENDING
        ]

    def update(
        self, interaction_id: str, response: HumanInteractionResponse,
    ) -> HumanInteractionRequest | None:
        req = self._requests.get(interaction_id)
        if req is None:
            return None
        from .models import InteractionStatus
        req.status = InteractionStatus.RESPONDED
        # Store response data in context for resume
        req.context["decision"] = response.decision
        req.context["selected_value"] = response.selected_value
        req.context["response_content"] = response.content
        return req

    def expire(self, interaction_id: str) -> HumanInteractionRequest | None:
        req = self._requests.get(interaction_id)
        if req is None:
            return None
        from .models import InteractionStatus
        req.status = InteractionStatus.EXPIRED
        return req

    def count(self) -> int:
        return len(self._requests)

    def clear(self) -> None:
        self._requests.clear()
