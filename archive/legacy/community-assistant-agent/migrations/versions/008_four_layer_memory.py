"""Add episodic memory and rebuildable semantic memory documents.

Revision ID: 008_four_layer_memory
Revises: 007_orchestration_platform
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "008_four_layer_memory"
down_revision: Union[str, Sequence[str], None] = "007_orchestration_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "assistant_memory_profiles" not in existing_tables:
        op.create_table(
            "assistant_memory_profiles",
            sa.Column("user_id", sa.String(64), primary_key=True),
            sa.Column("episodic_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("semantic_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    if "assistant_episodic_memories" not in existing_tables:
        op.create_table(
            "assistant_episodic_memories",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("intent", sa.String(64), nullable=True),
            sa.Column("goal", sa.String(1_000), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("outcome", sa.String(24), nullable=False, server_default="COMPLETED"),
            sa.Column("tool_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("artifact_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("run_id", name="uq_assistant_episodic_run"),
        )
    for column in ("user_id", "tenant_id", "run_id", "conversation_id", "occurred_at", "expires_at"):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_assistant_episodic_memories_{column} "
            f"ON assistant_episodic_memories ({column})"
        )
    if "assistant_semantic_memory_documents" not in existing_tables:
        op.create_table(
            "assistant_semantic_memory_documents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False, server_default="TASK_KNOWLEDGE"),
            sa.Column("title", sa.String(240), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("index_status", sa.String(24), nullable=False, server_default="PENDING"),
            sa.Column("index_error", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "source_type",
                "source_id",
                name="uq_assistant_semantic_memory_source",
            ),
        )
    for column in ("user_id", "tenant_id", "kind", "source_id", "index_status", "expires_at"):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_assistant_semantic_memory_documents_{column} "
            f"ON assistant_semantic_memory_documents ({column})"
        )


def downgrade() -> None:
    op.drop_table("assistant_semantic_memory_documents")
    op.drop_table("assistant_episodic_memories")
    op.drop_table("assistant_memory_profiles")
