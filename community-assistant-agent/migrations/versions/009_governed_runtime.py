"""Add immutable artifacts, policy audit and durable tool jobs.

Revision ID: 009_governed_runtime
Revises: 008_four_layer_memory
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "009_governed_runtime"
down_revision: Union[str, Sequence[str], None] = "008_four_layer_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_runs",
        sa.Column(
            "tenant_id",
            sa.String(64),
            nullable=False,
            server_default="zhiguang",
        ),
    )
    op.add_column(
        "assistant_runs",
        sa.Column(
            "principal_role",
            sa.String(32),
            nullable=False,
            server_default="USER",
        ),
    )
    op.create_index("ix_assistant_runs_tenant_id", "assistant_runs", ["tenant_id"])

    op.create_table(
        "assistant_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("assistant_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.String(36),
            sa.ForeignKey("assistant_run_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_key", sa.String(80), nullable=False),
        sa.Column("agent_name", sa.String(80), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column(
            "parent_artifact_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "run_id",
            "task_key",
            "version",
            name="uq_assistant_artifact_task_version",
        ),
        sa.UniqueConstraint(
            "run_id",
            "step_id",
            "content_hash",
            name="uq_assistant_artifact_step_content",
        ),
    )
    for column in (
        "run_id",
        "step_id",
        "task_key",
        "agent_name",
        "artifact_type",
        "content_hash",
        "created_at",
    ):
        op.create_index(
            f"ix_assistant_artifacts_{column}",
            "assistant_artifacts",
            [column],
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assistant_reject_artifact_update()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'assistant artifacts are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_assistant_artifacts_immutable'
            ) THEN
                CREATE TRIGGER trg_assistant_artifacts_immutable
                BEFORE UPDATE ON assistant_artifacts
                FOR EACH ROW
                EXECUTE FUNCTION assistant_reject_artifact_update();
            END IF;
        END
        $$
        """
    )

    op.create_table(
        "assistant_policy_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("principal_role", sa.String(32), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column(
            "resource", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column(
            "context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    for column in (
        "run_id",
        "user_id",
        "tenant_id",
        "action",
        "decision",
        "policy_version",
        "created_at",
    ):
        op.create_index(
            f"ix_assistant_policy_audits_{column}",
            "assistant_policy_audits",
            [column],
        )

    op.create_table(
        "assistant_tool_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("assistant_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_assistant_tool_job_idempotency"
        ),
        sa.UniqueConstraint(
            "run_id",
            "step_ordinal",
            "tool_name",
            name="uq_assistant_tool_job_step",
        ),
    )
    for column in (
        "run_id",
        "tool_name",
        "status",
        "next_attempt_at",
        "lease_expires_at",
        "dead_lettered_at",
        "created_at",
    ):
        op.create_index(
            f"ix_assistant_tool_jobs_{column}",
            "assistant_tool_jobs",
            [column],
        )


def downgrade() -> None:
    op.drop_table("assistant_tool_jobs")
    op.drop_table("assistant_policy_audits")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_assistant_artifacts_immutable "
        "ON assistant_artifacts"
    )
    op.execute("DROP FUNCTION IF EXISTS assistant_reject_artifact_update()")
    op.drop_table("assistant_artifacts")
    op.drop_index("ix_assistant_runs_tenant_id", table_name="assistant_runs")
    op.drop_column("assistant_runs", "principal_role")
    op.drop_column("assistant_runs", "tenant_id")
