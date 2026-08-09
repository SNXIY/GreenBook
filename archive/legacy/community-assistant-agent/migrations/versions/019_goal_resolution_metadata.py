"""Add durable semantic metadata used by GoalResolver."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "019_goal_resolution_metadata"
down_revision: Union[str, Sequence[str], None] = "018_intent_delta_operation_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversation_goals",
        sa.Column("summary", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "assistant_conversation_goals",
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.alter_column(
        "assistant_conversation_goals",
        "aliases",
        existing_type=sa.JSON(),
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("assistant_conversation_goals", "aliases")
    op.drop_column("assistant_conversation_goals", "summary")
