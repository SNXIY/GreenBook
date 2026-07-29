"""Add Evidence Reviewer task summary storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.add_column(sa.Column("evidence_review", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.drop_column("evidence_review")
