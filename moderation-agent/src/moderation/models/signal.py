from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, utc_now
from database.types import enum_type
from moderation.schemas import ModerationSignalType, SignalSource


class ModerationSignal(Base):
    __tablename__ = "moderation_signal"
    __table_args__ = (Index("ix_moderation_signal_task_created", "task_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("moderation_task.id", ondelete="CASCADE"), index=True
    )
    signal_type: Mapped[ModerationSignalType] = mapped_column(
        enum_type(ModerationSignalType, name="moderation_signal_type"), index=True
    )
    source: Mapped[SignalSource] = mapped_column(
        enum_type(SignalSource, name="moderation_signal_source"), index=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
