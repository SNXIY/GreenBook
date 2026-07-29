from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from moderation.schemas.enums import ModerationAction, RiskType


class EvidenceSourceType(StrEnum):
    CONTENT = "CONTENT"
    CONTEXT = "CONTEXT"
    SIGNAL = "SIGNAL"
    TOOL = "TOOL"
    POLICY_GRADE = "POLICY_GRADE"


class EvidenceStance(StrEnum):
    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"
    NEUTRAL = "NEUTRAL"


class PolicyConditionKind(StrEnum):
    APPLICABILITY = "APPLICABILITY"
    EXCLUSION = "EXCLUSION"


class ConditionTruthValue(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class PolicyEngineDisposition(StrEnum):
    ALLOW = "ALLOW"
    ENFORCE = "ENFORCE"
    ESCALATE = "ESCALATE"


class EvidenceClaim(BaseModel):
    claim_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    claim: str = Field(min_length=1, max_length=2000)
    source_type: EvidenceSourceType
    source_id: str = Field(min_length=1, max_length=256)
    stance: EvidenceStance = EvidenceStance.NEUTRAL
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_text: str | None = Field(default=None, max_length=1000)
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    policy_id: UUID | None = None
    policy_condition: str | None = Field(default=None, max_length=1000)
    provenance: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_span(self) -> "EvidenceClaim":
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("span_start and span_end must be provided together")
        if (
            self.span_start is not None
            and self.span_end is not None
            and self.span_end <= self.span_start
        ):
            raise ValueError("span_end must be greater than span_start")
        return self


class PolicyConditionEvaluation(BaseModel):
    policy_id: UUID
    condition_kind: PolicyConditionKind
    condition: str = Field(min_length=1, max_length=1000)
    value: ConditionTruthValue
    evidence_claim_ids: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)


class EvidenceLedger(BaseModel):
    version: Literal["1.0"] = "1.0"
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=200)
    policy_conditions: list[PolicyConditionEvaluation] = Field(
        default_factory=list,
        max_length=200,
    )
    complete: bool = False
    unresolved_conditions: list[str] = Field(default_factory=list, max_length=100)
    errors: list[str] = Field(default_factory=list, max_length=100)


class PolicyEngineResult(BaseModel):
    disposition: PolicyEngineDisposition
    selected_action: ModerationAction
    risk_type: RiskType
    decision_supported: bool
    auto_action_eligible: bool
    policy_ids: list[UUID] = Field(default_factory=list, max_length=20)
    semantic_conditions_verified: bool = False
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
