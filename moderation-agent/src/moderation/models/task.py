from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, utc_now
from moderation.models.types import enum_type
from moderation.schemas import (
    ModerationAction,
    ModerationContentType,
    ModerationTaskStatus,
    RiskType,
)


class ModerationTask(Base):
    __tablename__ = "moderation_task"
    __table_args__ = (
        Index("ix_moderation_task_status_created_at", "status", "created_at"),
        Index("ix_moderation_task_platform_content_id", "platform", "content_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[ModerationContentType] = mapped_column(
        enum_type(ModerationContentType, name="moderation_content_type"),
        default=ModerationContentType.TEXT,
        index=True,
    )
    normalized_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    platform: Mapped[str] = mapped_column(String(64), default="default", index=True)
    creator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    status: Mapped[ModerationTaskStatus] = mapped_column(
        enum_type(ModerationTaskStatus, name="moderation_task_status"),
        default=ModerationTaskStatus.PENDING,
        index=True,
    )
    risk_type: Mapped[RiskType | None] = mapped_column(
        enum_type(RiskType, name="moderation_risk_type"), nullable=True, index=True
    )
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    agent_action: Mapped[ModerationAction | None] = mapped_column(
        enum_type(ModerationAction, name="moderation_agent_action"), nullable=True
    )
    agent_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_decision: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    adversarial_review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    policy_rag: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    human_action: Mapped[ModerationAction | None] = mapped_column(
        enum_type(ModerationAction, name="moderation_human_action"), nullable=True
    )
    human_reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    human_risk_type: Mapped[RiskType | None] = mapped_column(
        enum_type(RiskType, name="moderation_human_risk_type"), nullable=True
    )
    human_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_decision: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_action: Mapped[ModerationAction | None] = mapped_column(
        enum_type(ModerationAction, name="moderation_final_action"), nullable=True, index=True
    )
    final_risk_type: Mapped[RiskType | None] = mapped_column(
        enum_type(RiskType, name="moderation_final_risk_type"), nullable=True, index=True
    )
    review_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )

    version: Mapped[int] = mapped_column(Integer, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
