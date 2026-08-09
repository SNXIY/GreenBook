from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DataAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    NOT_CONNECTED = "NOT_CONNECTED"


class UsedAngleDocument(AgentDocument):
    angle_key: str
    title: str
    angle: str = ""
    task_id: str = ""
    artifact_id: str = ""
    used_at: str = ""


class CreatorProfileDocument(AgentDocument):
    creator_id: str
    display_name: str = ""
    bio: str = ""
    expertise_tags: tuple[str, ...] = ()
    style_traits: tuple[str, ...]
    audience_hypotheses: tuple[str, ...]
    preferred_formats: tuple[str, ...]
    used_angles: tuple[UsedAngleDocument, ...] = ()
    data_availability: DataAvailability
    limitations: tuple[str, ...] = ()


class ContentAnalysisDocument(AgentDocument):
    strengths: tuple[str, ...]
    improvement_areas: tuple[str, ...]
    reusable_patterns: tuple[str, ...]
    data_availability: DataAvailability
    limitations: tuple[str, ...] = ()


class EvidenceItem(AgentDocument):
    id: str
    title: str
    summary: str
    source: str
    source_type: str
    requires_verification: bool = True
    document_id: str = ""
    source_url: str | None = None
    retrieval_channels: tuple[str, ...] = ()
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    published_at: datetime | None = None
    authority_verified: bool = False


class EvidencePackDocument(AgentDocument):
    research_question: str
    evidence: tuple[EvidenceItem, ...]
    search_gaps: tuple[str, ...]
    data_availability: DataAvailability


class TopicRecommendation(str, Enum):
    WRITE_NOW = "WRITE_NOW"
    WRITE_LATER = "WRITE_LATER"
    SKIP = "SKIP"


class TopicOption(AgentDocument):
    id: str
    title: str
    angle: str
    audience_value: str
    evidence_ids: tuple[str, ...] = ()
    comment_ids: tuple[str, ...] = ()
    risk_note: str = ""
    recommendation: TopicRecommendation = TopicRecommendation.WRITE_NOW
    why_now: str = ""
    reader_question: str = ""
    differentiation: str = ""


class TopicOptionsDocument(AgentDocument):
    options: tuple[TopicOption, ...] = Field(min_length=3, max_length=5)
    recommended_option_id: str
    recommendation_reason: str

    @model_validator(mode="after")
    def validate_recommendation(self) -> "TopicOptionsDocument":
        option_ids = {option.id for option in self.options}
        if self.recommended_option_id not in option_ids:
            raise ValueError("recommended_option_id must identify an option")
        recommended = next(
            option
            for option in self.options
            if option.id == self.recommended_option_id
        )
        if recommended.recommendation == TopicRecommendation.SKIP:
            raise ValueError("recommended_option_id cannot point to a SKIP option")
        recommendations = {option.recommendation for option in self.options}
        if len(self.options) >= 3 and len(recommendations) < 2:
            raise ValueError(
                "topic options must include at least two distinct recommendation "
                "labels among WRITE_NOW, WRITE_LATER, and SKIP"
            )
        return self


class OutlineSection(AgentDocument):
    heading: str
    purpose: str
    key_points: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()


class ContentOutlineDocument(AgentDocument):
    title: str
    thesis: str
    sections: tuple[OutlineSection, ...] = Field(min_length=3)
    call_to_action: str


class DraftCitationDocument(AgentDocument):
    claim_text: str = Field(min_length=1, max_length=2_000)
    evidence_id: str = Field(min_length=1, max_length=256)
    source_title: str = Field(default="", max_length=512)
    source_url: str | None = Field(default=None, max_length=2_000)


class DraftDocument(AgentDocument):
    title: str
    body_markdown: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    citations: tuple[DraftCitationDocument, ...] = ()
    revision_note: str = ""


class DraftSectionAnnotation(AgentDocument):
    """1-based section index aligned with the approved outline."""

    section: int = Field(ge=1, le=32)
    note: str = Field(min_length=1, max_length=2_000)


class CritiqueVerdict(str, Enum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"


class CritiqueScores(AgentDocument):
    relevance: float = Field(ge=0.0, le=1.0)
    structure: float = Field(ge=0.0, le=1.0)
    evidence: float = Field(ge=0.0, le=1.0)
    style: float = Field(ge=0.0, le=1.0)
    overall: float = Field(ge=0.0, le=1.0)


class CritiqueDocument(AgentDocument):
    reviewed_artifact_id: str
    verdict: CritiqueVerdict
    scores: CritiqueScores
    strengths: tuple[str, ...]
    issues: tuple[str, ...]
    revision_instructions: tuple[str, ...]


class EvaluationMetricDocument(AgentDocument):
    metric: str
    status: str
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    passed: bool | None = None
    evaluator: str
    evaluator_version: str
    reason: str


class EvaluationDocument(AgentDocument):
    task_success: bool
    planning_observations: tuple[str, ...]
    generation_observations: tuple[str, ...]
    quality_score: float = Field(ge=0.0, le=1.0)
    metric_status: str
    dataset_id: str = "creator-runtime-context"
    dataset_version: str = "1.0.0"
    evaluator_version: str = "phase-3-baseline"
    metrics: tuple[EvaluationMetricDocument, ...] = ()
    unevaluated_metrics: tuple[str, ...] = ()
