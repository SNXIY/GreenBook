"""Persist the operation-scoped target role on IntentDelta."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "016_intent_delta_target_role"
down_revision: Union[str, Sequence[str], None] = "015_target_binding_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_intent_deltas",
        sa.Column("target_role", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_intent_deltas", "target_role")
