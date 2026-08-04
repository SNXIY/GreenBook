"""Persist the role of each Goal target binding."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "015_target_binding_roles"
down_revision: Union[str, Sequence[str], None] = "014_target_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_target_bindings",
        sa.Column("role", sa.String(length=24), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE assistant_target_bindings "
            "SET role = CASE "
            "WHEN target_type = 'SCHEDULE' THEN 'SCHEDULE' "
            "WHEN target_type = 'ARTIFACT' THEN 'INTERACTION' "
            "ELSE 'CONTENT' END "
            "WHERE role IS NULL"
        )
    )
    op.alter_column(
        "assistant_target_bindings",
        "role",
        existing_type=sa.String(length=24),
        nullable=False,
        server_default="CONTENT",
    )
    op.create_index(
        "ix_assistant_target_bindings_role",
        "assistant_target_bindings",
        ["role"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_target_bindings_role",
        table_name="assistant_target_bindings",
    )
    op.drop_column("assistant_target_bindings", "role")
