"""Track scheduled capability identity for cancellation compensation.

Revision ID: 004_capability_revocation
Revises: 003_saga_capabilities
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "004_capability_revocation"
down_revision: Union[str, Sequence[str], None] = "003_saga_capabilities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE assistant_scheduled_actions "
        "ADD COLUMN IF NOT EXISTS capability_id VARCHAR(36)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_assistant_scheduled_actions_capability_id "
        "ON assistant_scheduled_actions (capability_id)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_assistant_scheduled_actions_capability_id"
    )
    op.execute(
        "ALTER TABLE assistant_scheduled_actions "
        "DROP COLUMN IF EXISTS capability_id"
    )
