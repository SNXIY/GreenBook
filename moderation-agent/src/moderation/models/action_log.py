from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, utc_now
from moderation.models.types import enum_type
from moderation.schemas import ActionLogEvent, DecisionSource, ModerationAction


class ModerationActionLog(Base):
    __tablename__ = "moderation_action_log"
    __table_args__ = (Index("ix_moderation_action_log_task_created", "task_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("moderation_task.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[ActionLogEvent] = mapped_column(
        enum_type(ActionLogEvent, name="moderation_action_log_event")
    )
    source: Mapped[DecisionSource] = mapped_column(
        enum_type(DecisionSource, name="moderation_decision_source")
    )
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[ModerationAction | None] = mapped_column(
        enum_type(ModerationAction, name="moderation_log_action"), nullable=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
