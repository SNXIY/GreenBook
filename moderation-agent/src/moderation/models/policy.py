from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, utc_now
from moderation.models.types import enum_type
from moderation.schemas import ModerationAction, PolicySeverity, RiskType


class ModerationPolicy(Base):
    __tablename__ = "moderation_policy"
    __table_args__ = (
        UniqueConstraint("platform", "code", name="uq_moderation_policy_platform_code"),
        Index(
            "ix_moderation_policy_lookup",
            "platform",
            "risk_type",
            "enabled",
            "priority",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text)
    risk_type: Mapped[RiskType] = mapped_column(
        enum_type(RiskType, name="moderation_policy_risk_type"), index=True
    )
    default_action: Mapped[ModerationAction] = mapped_column(
        enum_type(ModerationAction, name="moderation_policy_default_action")
    )
    platform: Mapped[str] = mapped_column(String(64), default="default", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    version: Mapped[int] = mapped_column(Integer, default=1)
    applicability_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclusion_conditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    violation_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    safe_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    severity: Mapped[PolicySeverity] = mapped_column(
        enum_type(PolicySeverity, name="moderation_policy_severity"),
        default=PolicySeverity.MEDIUM,
        index=True,
    )
    suggested_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
