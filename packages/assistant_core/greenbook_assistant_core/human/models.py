"""Human-in-the-loop models — unified pause/resume for Agent interactions.

Phase 6.5: APPROVAL, CLARIFICATION, INPUT.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field


class InteractionType(StrEnum):
    APPROVAL = "APPROVAL"
    CLARIFICATION = "CLARIFICATION"
    INPUT = "INPUT"


class InteractionStatus(StrEnum):
    PENDING = "PENDING"
    RESPONDED = "RESPONDED"
    EXPIRED = "EXPIRED"


class HumanInteractionRequest(BaseModel):
    """A request for human input — pauses execution until resolved."""

    interaction_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str = ""
    task_id: str = ""
    step_id: str = ""

    type: InteractionType = InteractionType.APPROVAL
    question: str = ""
    options: list[dict] = []  # [{value, label}, ...]

    context: dict = Field(default_factory=dict)  # extra rendering info
    status: InteractionStatus = InteractionStatus.PENDING

    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    expires_at: str = Field(
        default_factory=lambda: (
            datetime.now(UTC) + timedelta(minutes=5)
        ).isoformat()
    )


class HumanInteractionResponse(BaseModel):
    """User's response to a HumanInteractionRequest."""

    interaction_id: str
    decision: str = ""       # ACCEPT | REJECT | SELECT | INPUT
    selected_value: str = ""  # CLARIFICATION: selected option value
    content: str = ""         # INPUT: free-text input
    responded_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
