from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from moderation.schemas.adversarial import AdversarialReviewAudit
from moderation.schemas.decision import AgentDecision, HumanDecision
from moderation.schemas.enums import (
    ModerationAction,
    ModerationContentType,
    ModerationTaskStatus,
    RiskType,
)
from moderation.schemas.policy_rag import AgenticPolicyRAGAudit
from moderation.schemas.reviewer import EvidenceReviewerAudit
from moderation.schemas.signal import ModerationSignalEvidence


class ModerationTaskCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    content_type: ModerationContentType = ModerationContentType.TEXT
    content_id: str | None = Field(default=None, max_length=256)
    platform: str = Field(default="default", min_length=1, max_length=64)
    creator_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class ModerationTaskSummary(BaseModel):
    id: UUID
    thread_id: str
    trace_id: str | None = None
    status: ModerationTaskStatus
    content: str
    content_type: ModerationContentType = ModerationContentType.TEXT
    agent_decision: AgentDecision | None = None
    final_action: ModerationAction | None = None
    final_risk_type: RiskType | None = None
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class ModerationTaskDetail(ModerationTaskSummary):
    content_id: str | None = None
    platform: str
    creator_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    normalized_content: str | None = None
    adversarial_review: AdversarialReviewAudit | None = None
    policy_rag: AgenticPolicyRAGAudit | None = None
    evidence_review: EvidenceReviewerAudit | None = None
    human_decision: HumanDecision | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    signals: list[ModerationSignalEvidence] = Field(default_factory=list)


class ModerationTaskAccepted(BaseModel):
    task: ModerationTaskDetail
    requires_human_review: bool
