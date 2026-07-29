from typing import Any

from pydantic import BaseModel, Field

from moderation.schemas.enums import ModerationSignalType, SignalSource


class ModerationSignalEvidence(BaseModel):
    signal_type: ModerationSignalType
    source: SignalSource
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)
