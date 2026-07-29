"""Add durable moderation result callback outbox."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_callback_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["moderation_task.id"],
            name="fk_moderation_callback_outbox_task_id_moderation_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_callback_outbox"),
        sa.UniqueConstraint(
            "task_id",
            name="uq_moderation_callback_outbox_task_id",
        ),
    )
    op.create_index(
        "ix_moderation_callback_outbox_task_id",
        "moderation_callback_outbox",
        ["task_id"],
    )
    op.create_index(
        "ix_moderation_callback_outbox_status",
        "moderation_callback_outbox",
        ["status"],
    )
    op.create_index(
        "ix_moderation_callback_outbox_available_at",
        "moderation_callback_outbox",
        ["available_at"],
    )
    op.create_index(
        "ix_moderation_callback_outbox_lease_expires_at",
        "moderation_callback_outbox",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_moderation_callback_ready",
        "moderation_callback_outbox",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_table("moderation_callback_outbox")
