from pydantic import BaseModel, Field

from moderation.schemas.enums import ModerationAction, ModerationTaskStatus, RiskType


class ModerationStatistics(BaseModel):
    total_tasks: int = 0
    pending_review: int = 0
    agent_human_disagreements: int = 0
    by_status: dict[ModerationTaskStatus, int] = Field(default_factory=dict)
    by_risk_type: dict[RiskType, int] = Field(default_factory=dict)
    by_action: dict[ModerationAction, int] = Field(default_factory=dict)
