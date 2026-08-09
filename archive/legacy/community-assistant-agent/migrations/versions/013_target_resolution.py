"""Persist human target clarification state on ConversationGoal."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "013_target_resolution"
down_revision: Union[str, Sequence[str], None] = "012_intent_deltas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversation_goals",
        sa.Column("pending_clarification", sa.JSON(), nullable=True),
    )
    op.add_column(
        "assistant_conversation_goals",
        sa.Column("pending_delta_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_assistant_conversation_goals_pending_delta_id",
        "assistant_conversation_goals",
        ["pending_delta_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_conversation_goals_pending_delta_id",
        table_name="assistant_conversation_goals",
    )
    op.drop_column("assistant_conversation_goals", "pending_delta_id")
    op.drop_column("assistant_conversation_goals", "pending_clarification")
