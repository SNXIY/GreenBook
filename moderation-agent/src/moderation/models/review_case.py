from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, utc_now
from moderation.models.types import enum_type
from moderation.schemas import ModerationAction, RiskType


class ModerationReviewCase(Base):
    __tablename__ = "moderation_review_case"
    __table_args__ = (
        Index("ix_moderation_review_case_lookup", "platform", "agent_risk_type", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    original_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("moderation_task.id", ondelete="CASCADE"), unique=True, index=True
    )
    content: Mapped[str] = mapped_column(Text)
    normalized_content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(64), default="default", index=True)
    agent_risk_type: Mapped[RiskType] = mapped_column(
        enum_type(RiskType, name="moderation_case_risk_type")
    )
    agent_action: Mapped[ModerationAction] = mapped_column(
        enum_type(ModerationAction, name="moderation_case_agent_action")
    )
    final_action: Mapped[ModerationAction] = mapped_column(
        enum_type(ModerationAction, name="moderation_case_final_action")
    )
    final_risk_type: Mapped[RiskType] = mapped_column(
        enum_type(RiskType, name="moderation_case_final_risk_type")
    )
    reviewer_id: Mapped[str] = mapped_column(String(128))
    reviewer_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_policy_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
