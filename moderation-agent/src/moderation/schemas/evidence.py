from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from moderation.schemas.enums import ModerationAction, PolicySeverity, RiskType


class PolicyEvidence(BaseModel):
    policy_id: UUID
    code: str
    title: str
    excerpt: str
    score: float = Field(ge=0.0, le=1.0)
    risk_type: RiskType | None = None
    default_action: ModerationAction | None = None
    version: int | None = Field(default=None, ge=1)
    severity: PolicySeverity | None = None
    suggested_actions: list[ModerationAction] = Field(default_factory=list, max_length=4)
    applicability_conditions: list[str] = Field(default_factory=list, max_length=20)
    exclusion_conditions: list[str] = Field(default_factory=list, max_length=20)
    violation_examples: list[str] = Field(default_factory=list, max_length=20)
    safe_examples: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool | None = None
    effective_at: datetime | None = None
    expires_at: datetime | None = None


class CaseEvidence(BaseModel):
    case_id: UUID
    content_excerpt: str
    risk_type: RiskType
    final_action: ModerationAction
    reviewer_reason: str | None = None
    score: float = Field(ge=0.0, le=1.0)
