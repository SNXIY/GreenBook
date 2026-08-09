"""Persist turn-level IntentDelta state for Goal-aware execution."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "012_intent_deltas"
down_revision: Union[str, Sequence[str], None] = "011_goal_target_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_intent_deltas",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("target_ref", sa.String(length=160), nullable=True),
        sa.Column("delta", sa.JSON(), nullable=False),
        sa.Column("preserve", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["assistant_conversation_goals.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["assistant_runs.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["assistant_messages.id"]),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_assistant_intent_deltas_goal_id", "assistant_intent_deltas", ["goal_id"])
    op.create_index("ix_assistant_intent_deltas_run_id", "assistant_intent_deltas", ["run_id"])
    op.create_index("ix_assistant_intent_deltas_message_id", "assistant_intent_deltas", ["message_id"])
    op.create_index("ix_assistant_intent_deltas_operation", "assistant_intent_deltas", ["operation"])
    op.create_index("ix_assistant_intent_deltas_target_ref", "assistant_intent_deltas", ["target_ref"])
    op.create_index("ix_assistant_intent_deltas_status", "assistant_intent_deltas", ["status"])


def downgrade() -> None:
    op.drop_index("ix_assistant_intent_deltas_status", table_name="assistant_intent_deltas")
    op.drop_index("ix_assistant_intent_deltas_target_ref", table_name="assistant_intent_deltas")
    op.drop_index("ix_assistant_intent_deltas_operation", table_name="assistant_intent_deltas")
    op.drop_index("ix_assistant_intent_deltas_message_id", table_name="assistant_intent_deltas")
    op.drop_index("ix_assistant_intent_deltas_run_id", table_name="assistant_intent_deltas")
    op.drop_index("ix_assistant_intent_deltas_goal_id", table_name="assistant_intent_deltas")
    op.drop_table("assistant_intent_deltas")
