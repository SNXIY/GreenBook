"""Add ConversationGoal and TargetBinding control-plane state."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "011_goal_target_binding"
down_revision: Union[str, Sequence[str], None] = "010_adaptive_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversation_goals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="zhiguang"),
        sa.Column("intent", sa.String(length=64), nullable=False, server_default="UNKNOWN"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="DISCOVERING"),
        sa.Column("active_target_ref", sa.String(length=160), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"]),
        sa.UniqueConstraint("conversation_id", "version"),
    )
    op.create_index(
        "ix_assistant_conversation_goals_conversation_id",
        "assistant_conversation_goals",
        ["conversation_id"],
    )
    op.create_index(
        "ix_assistant_conversation_goals_status",
        "assistant_conversation_goals",
        ["status"],
    )
    op.create_index(
        "ix_assistant_conversation_goals_active_target_ref",
        "assistant_conversation_goals",
        ["active_target_ref"],
    )

    op.create_table(
        "assistant_target_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=128), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("resolution_method", sa.String(length=32), nullable=False, server_default="ACTIVE_TARGET"),
        sa.Column("schedule_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["assistant_conversation_goals.id"]),
        sa.UniqueConstraint("goal_id", "version"),
    )
    op.create_index(
        "ix_assistant_target_bindings_goal_id",
        "assistant_target_bindings",
        ["goal_id"],
    )
    op.create_index(
        "ix_assistant_target_bindings_target_id",
        "assistant_target_bindings",
        ["target_id"],
    )
    op.create_index(
        "ix_assistant_target_bindings_artifact_id",
        "assistant_target_bindings",
        ["artifact_id"],
    )

    op.add_column(
        "assistant_runs",
        sa.Column("goal_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_assistant_runs_goal_id",
        "assistant_runs",
        "assistant_conversation_goals",
        ["goal_id"],
        ["id"],
    )
    op.create_index("ix_assistant_runs_goal_id", "assistant_runs", ["goal_id"])


def downgrade() -> None:
    op.drop_index("ix_assistant_runs_goal_id", table_name="assistant_runs")
    op.drop_constraint("fk_assistant_runs_goal_id", "assistant_runs", type_="foreignkey")
    op.drop_column("assistant_runs", "goal_id")
    op.drop_index("ix_assistant_target_bindings_artifact_id", table_name="assistant_target_bindings")
    op.drop_index("ix_assistant_target_bindings_target_id", table_name="assistant_target_bindings")
    op.drop_index("ix_assistant_target_bindings_goal_id", table_name="assistant_target_bindings")
    op.drop_table("assistant_target_bindings")
    op.drop_index("ix_assistant_conversation_goals_active_target_ref", table_name="assistant_conversation_goals")
    op.drop_index("ix_assistant_conversation_goals_status", table_name="assistant_conversation_goals")
    op.drop_index("ix_assistant_conversation_goals_conversation_id", table_name="assistant_conversation_goals")
    op.drop_table("assistant_conversation_goals")
