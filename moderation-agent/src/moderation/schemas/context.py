from datetime import datetime

from pydantic import BaseModel, Field

from moderation.schemas.enums import ModerationAction, ModerationContentType, RiskType


class CommunityContentRecord(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    content_type: ModerationContentType
    author_id: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=20_000)
    title: str | None = Field(default=None, max_length=500)
    audit_status: str | None = Field(default=None, max_length=64)
    created_at: datetime | None = None


class CommunityContentSnapshot(BaseModel):
    current: CommunityContentRecord
    post: CommunityContentRecord | None = None
    parent_comment_required: bool = False


class ViolationRecord(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    risk_type: RiskType
    action: ModerationAction
    reason: str = Field(min_length=1, max_length=2000)
    created_at: datetime | None = None


class ReportEvidence(BaseModel):
    report_type: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    reporter_id: str = Field(min_length=1, max_length=128)
    created_at: datetime | None = None


class ModerationContextEvidence(BaseModel):
    current: CommunityContentRecord | None = None
    post: CommunityContentRecord | None = None
    parent_comment: CommunityContentRecord | None = None
    conversation_context: list[CommunityContentRecord] = Field(default_factory=list)
    author_recent_contents: list[CommunityContentRecord] = Field(default_factory=list)
    author_violation_history: list[ViolationRecord] = Field(default_factory=list)
    reports: list[ReportEvidence] = Field(default_factory=list)
    parent_comment_required: bool = False
    complete: bool = True
    errors: list[str] = Field(default_factory=list)
