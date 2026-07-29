from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from moderation.schemas.enums import ModerationAction, PolicySeverity, RiskType

PolicyFactText = Annotated[str, Field(min_length=1, max_length=1000)]
PolicyTag = Annotated[str, Field(min_length=1, max_length=64)]


class ModerationPolicyCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=5000)
    risk_type: RiskType
    default_action: ModerationAction
    platform: str = Field(default="default", min_length=1, max_length=64)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)
    applicability_conditions: list[PolicyFactText] = Field(default_factory=list, max_length=20)
    exclusion_conditions: list[PolicyFactText] = Field(default_factory=list, max_length=20)
    violation_examples: list[PolicyFactText] = Field(default_factory=list, max_length=20)
    safe_examples: list[PolicyFactText] = Field(default_factory=list, max_length=20)
    severity: PolicySeverity = PolicySeverity.MEDIUM
    suggested_actions: list[ModerationAction] = Field(default_factory=list, max_length=4)
    tags: list[PolicyTag] = Field(default_factory=list, max_length=20)
    effective_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_policy_facts(self) -> "ModerationPolicyCreate":
        actions = list(dict.fromkeys(self.suggested_actions))
        if self.default_action not in actions:
            if len(actions) >= 4:
                raise ValueError("suggested_actions cannot omit default_action at maximum length")
            actions.insert(0, self.default_action)
        self.suggested_actions = actions
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("expires_at must be later than effective_at")
        return self


class ModerationPolicyRead(ModerationPolicyCreate):
    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime
