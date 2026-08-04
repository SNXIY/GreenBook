"""Persist operation-scoped target context on conversation goals."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "014_target_context"
down_revision: Union[str, Sequence[str], None] = "013_target_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversation_goals",
        sa.Column("target_context", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_conversation_goals", "target_context")
