from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.infrastructure.sqlalchemy import CreatorBase


class CreatorProjectRow(CreatorBase):
    __tablename__ = "creator_projects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "name",
            name="uq_creator_projects_name",
        ),
        Index(
            "ix_creator_projects_scope_updated",
            "tenant_id",
            "creator_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorProjectTaskRow(CreatorBase):
    __tablename__ = "creator_project_tasks"

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorMaterialRow(CreatorBase):
    __tablename__ = "creator_materials"
    __table_args__ = (
        Index(
            "ix_creator_materials_scope_updated",
            "tenant_id",
            "creator_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorTaskMaterialRow(CreatorBase):
    __tablename__ = "creator_task_materials"

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    material_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_materials.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorSuggestionRow(CreatorBase):
    __tablename__ = "creator_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "idempotency_key_hash",
            name="uq_creator_suggestions_idempotency",
        ),
        Index(
            "ix_creator_suggestions_draft_created",
            "draft_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    replacement_text: Mapped[str] = mapped_column(Text, nullable=False)
    prefix_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suffix_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    risk_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class CreatorDraftBranchRow(CreatorBase):
    __tablename__ = "creator_draft_branches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "draft_id",
            name="uq_creator_draft_branch_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorChannelVariantRow(CreatorBase):
    __tablename__ = "creator_channel_variants"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "creator_id",
            "idempotency_key_hash",
            name="uq_creator_channel_variants_idempotency",
        ),
        Index(
            "ix_creator_channel_variants_draft_created",
            "draft_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    adaptation_note: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreatorFeedbackRow(CreatorBase):
    __tablename__ = "creator_feedback_events"
    __table_args__ = (
        Index(
            "ix_creator_feedback_scope_created",
            "tenant_id",
            "creator_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_drafts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    suggestion_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_suggestions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
