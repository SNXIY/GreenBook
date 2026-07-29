"""Persist moderation task trace identifiers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.add_column(sa.Column("trace_id", sa.String(128), nullable=True))
    op.execute(
        "UPDATE moderation_task SET trace_id = CAST(id AS VARCHAR) "
        "WHERE trace_id IS NULL"
    )
    with op.batch_alter_table("moderation_task") as batch:
        batch.alter_column("trace_id", existing_type=sa.String(128), nullable=False)
        batch.create_index("ix_moderation_task_trace_id", ["trace_id"])


def downgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.drop_index("ix_moderation_task_trace_id")
        batch.drop_column("trace_id")
