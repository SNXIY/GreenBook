from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from moderation.schemas.enums import ActionLogEvent, DecisionSource, ModerationAction


class ModerationActionLogRead(BaseModel):
    id: UUID
    task_id: UUID
    event: ActionLogEvent
    source: DecisionSource
    actor_id: str | None = None
    action: ModerationAction | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
