"""add publication handoffs

Revision ID: c8f3d2b05a21
Revises: b7e2c1a94f10
Create Date: 2026-07-25

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8f3d2b05a21"
down_revision: Union[str, Sequence[str], None] = "b7e2c1a94f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creator_publication_handoffs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("creator_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("draft_id", sa.String(length=64), nullable=False),
        sa.Column("content_origin", sa.String(length=32), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=128), nullable=False),
        sa.Column("source_artifact_revision", sa.Integer(), nullable=False),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("external_draft_id", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["creator_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["creator_tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "creator_id",
            "task_id",
            "source_artifact_id",
            name="uq_creator_publication_handoff_artifact",
        ),
    )
    with op.batch_alter_table("creator_publication_handoffs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_creator_publication_handoffs_creator_id"),
            ["creator_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_creator_publication_handoffs_draft_id"),
            ["draft_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_creator_publication_handoffs_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_creator_publication_handoffs_task_id"),
            ["task_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_creator_publication_handoffs_tenant_id"),
            ["tenant_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("creator_publication_handoffs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_creator_publication_handoffs_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_creator_publication_handoffs_task_id"))
        batch_op.drop_index(batch_op.f("ix_creator_publication_handoffs_status"))
        batch_op.drop_index(batch_op.f("ix_creator_publication_handoffs_draft_id"))
        batch_op.drop_index(batch_op.f("ix_creator_publication_handoffs_creator_id"))
    op.drop_table("creator_publication_handoffs")
