"""add decision edited_payload

Revision ID: b7e2c1a94f10
Revises: 44c80083ed3e
Create Date: 2026-07-25

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7e2c1a94f10"
down_revision: Union[str, Sequence[str], None] = "44c80083ed3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("creator_human_decisions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("edited_payload_json", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("creator_human_decisions", schema=None) as batch_op:
        batch_op.drop_column("edited_payload_json")
