"""Add adaptive execution path and workload lane.

Revision ID: 010_adaptive_execution
Revises: 009_governed_runtime
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "010_adaptive_execution"
down_revision: Union[str, Sequence[str], None] = "009_governed_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_runs",
        sa.Column(
            "execution_path",
            sa.String(24),
            nullable=False,
            server_default="ROUTING",
        ),
    )
    op.add_column(
        "assistant_runs",
        sa.Column(
            "workload_lane",
            sa.String(16),
            nullable=False,
            server_default="ROUTING",
        ),
    )
    op.create_index(
        "ix_assistant_runs_execution_path",
        "assistant_runs",
        ["execution_path"],
    )
    op.create_index(
        "ix_assistant_runs_workload_lane",
        "assistant_runs",
        ["workload_lane"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_runs_workload_lane",
        table_name="assistant_runs",
    )
    op.drop_index(
        "ix_assistant_runs_execution_path",
        table_name="assistant_runs",
    )
    op.drop_column("assistant_runs", "workload_lane")
    op.drop_column("assistant_runs", "execution_path")
