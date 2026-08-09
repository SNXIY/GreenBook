"""add multi-agent orchestration ledgers and DAG task metadata

Revision ID: 007_orchestration_platform
Revises: 006_concurrency_latency
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "007_orchestration_platform"
down_revision = "006_concurrency_latency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assistant_runs", sa.Column("intent_detail", sa.JSON(), nullable=True))
    op.add_column(
        "assistant_runs",
        sa.Column("task_ledger", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "assistant_runs",
        sa.Column(
            "progress_ledger", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.add_column(
        "assistant_runs",
        sa.Column("interrupted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assistant_run_steps", sa.Column("task_key", sa.String(80), nullable=True)
    )
    op.add_column(
        "assistant_run_steps", sa.Column("agent_name", sa.String(80), nullable=True)
    )
    op.add_column(
        "assistant_run_steps",
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "assistant_run_steps",
        sa.Column("depends_on", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "assistant_run_steps", sa.Column("condition", sa.JSON(), nullable=True)
    )
    op.add_column(
        "assistant_run_steps",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "assistant_run_steps",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_assistant_run_steps_task_key",
        "assistant_run_steps",
        ["task_key"],
    )
    op.create_index(
        "ix_assistant_run_steps_agent_name",
        "assistant_run_steps",
        ["agent_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_run_steps_agent_name", table_name="assistant_run_steps")
    op.drop_index("ix_assistant_run_steps_task_key", table_name="assistant_run_steps")
    for column in (
        "max_attempts",
        "attempts",
        "condition",
        "depends_on",
        "capabilities",
        "agent_name",
        "task_key",
    ):
        op.drop_column("assistant_run_steps", column)
    for column in ("interrupted_at", "progress_ledger", "task_ledger", "intent_detail"):
        op.drop_column("assistant_runs", column)
