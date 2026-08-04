"""Add tool execution receipts and artifact provenance keys."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "020_execution_reliability"
down_revision: Union[str, Sequence[str], None] = "019_goal_resolution_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # This migration is deliberately restart-safe.  A previous deployment may
    # have committed the DDL while failing before Alembic recorded the revision.
    if "assistant_tool_execution_receipts" not in tables:
        op.create_table(
            "assistant_tool_execution_receipts",
            sa.Column("execution_id", sa.String(length=36), primary_key=True),
            sa.Column(
                "run_id",
                sa.String(length=36),
                sa.ForeignKey("assistant_runs.id"),
                nullable=False,
            ),
            sa.Column(
                "step_id",
                sa.String(length=36),
                sa.ForeignKey("assistant_run_steps.id"),
                nullable=False,
            ),
            sa.Column("tool_name", sa.String(length=80), nullable=False),
            sa.Column("idempotency_key", sa.String(length=160), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("result_ref", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("idempotency_key"),
            sa.UniqueConstraint("run_id", "step_id"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {
        item["name"] for item in inspector.get_indexes("assistant_tool_execution_receipts")
    }
    for column in ("run_id", "step_id", "tool_name", "status", "result_ref", "created_at"):
        index_name = f"ix_assistant_tool_execution_receipts_{column}"
        if index_name not in existing_indexes:
            op.create_index(index_name, "assistant_tool_execution_receipts", [column])
    inspector = sa.inspect(bind)

    # Existing Artifact rows remain untouched because the table is protected by
    # an UPDATE-rejecting immutability trigger. New tool artifacts always supply
    # this key at INSERT time; PostgreSQL's UNIQUE semantics permit legacy NULLs.
    artifact_columns = {
        item["name"] for item in inspector.get_columns("assistant_artifacts")
    }
    if "provenance_key" not in artifact_columns:
        op.add_column(
            "assistant_artifacts",
            sa.Column("provenance_key", sa.String(length=160), nullable=True),
        )
    artifact_indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("assistant_artifacts")
    }
    if "ix_assistant_artifacts_provenance_key" not in artifact_indexes:
        op.create_index(
            "ix_assistant_artifacts_provenance_key",
            "assistant_artifacts",
            ["provenance_key"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_artifacts_provenance_key",
        table_name="assistant_artifacts",
    )
    op.drop_column("assistant_artifacts", "provenance_key")
    for column in reversed(
        ("run_id", "step_id", "tool_name", "status", "result_ref", "created_at")
    ):
        op.drop_index(
            f"ix_assistant_tool_execution_receipts_{column}",
            table_name="assistant_tool_execution_receipts",
        )
    op.drop_table("assistant_tool_execution_receipts")
