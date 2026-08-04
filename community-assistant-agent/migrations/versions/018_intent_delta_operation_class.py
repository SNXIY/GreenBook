"""Classify IntentDelta operations by their state-change boundary."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "018_intent_delta_operation_class"
down_revision: Union[str, Sequence[str], None] = "017_artifact_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_intent_deltas",
        sa.Column("operation_class", sa.String(length=16), nullable=True),
    )
    op.execute(
        """
        UPDATE assistant_intent_deltas
        SET operation_class = CASE
            WHEN operation IN ('UPDATE_SCHEDULE', 'PUBLISH_NOW', 'CANCEL_SCHEDULE')
                THEN 'SIDE_EFFECT'
            ELSE 'WRITE'
        END
        """
    )
    op.alter_column(
        "assistant_intent_deltas",
        "operation_class",
        existing_type=sa.String(length=16),
        nullable=False,
    )
    op.create_index(
        "ix_assistant_intent_deltas_operation_class",
        "assistant_intent_deltas",
        ["operation_class"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_intent_deltas_operation_class",
        table_name="assistant_intent_deltas",
    )
    op.drop_column("assistant_intent_deltas", "operation_class")
