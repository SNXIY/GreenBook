from pydantic import BaseModel, Field, model_validator

from moderation.schemas.cascade import ReasoningCascadeAudit
from moderation.schemas.context import ModerationContextEvidence
from moderation.schemas.enums import ModerationAction, RiskType
from moderation.schemas.evidence import CaseEvidence, PolicyEvidence
from moderation.schemas.policy_engine import EvidenceLedger, PolicyEngineResult
from moderation.schemas.signal import ModerationSignalEvidence
from moderation.schemas.tool_calling import EvidenceCollectionAudit


class RiskClassification(BaseModel):
    risk_type: RiskType
    risk_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    indicators: list[str] = Field(default_factory=list, max_length=10)
    fallback_used: bool = False


class AgentDecision(BaseModel):
    risk_type: RiskType
    risk_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: ModerationAction
    reason: str = Field(min_length=1, max_length=2000)
    matched_policies: list[PolicyEvidence] = Field(default_factory=list)
    similar_cases: list[CaseEvidence] = Field(default_factory=list)
    signals: list[ModerationSignalEvidence] = Field(default_factory=list)
    context_evidence: ModerationContextEvidence | None = None
    source_evidence: list[str] = Field(default_factory=list, max_length=20)
    needs_context_review: bool = False
    evidence_complete: bool = False
    evidence_collection: EvidenceCollectionAudit | None = None
    reasoning_cascade: ReasoningCascadeAudit | None = None
    evidence_ledger: EvidenceLedger | None = None
    policy_engine: PolicyEngineResult | None = None
    model_fallback_used: bool = False


class HumanDecision(BaseModel):
    action: ModerationAction
    risk_type: RiskType | None = None
    reviewer_id: str = Field(min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_final_action(self) -> "HumanDecision":
        if self.action == ModerationAction.HUMAN_REVIEW:
            raise ValueError("A human decision must be PASS, REJECT, or LIMIT")
        return self
