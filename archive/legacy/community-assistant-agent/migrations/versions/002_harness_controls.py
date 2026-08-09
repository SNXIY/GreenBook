"""Add budgets, approvals, checkpoints and explicit user memory.

Revision ID: 002_harness_controls
Revises: 001_assistant_baseline
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "002_harness_controls"
down_revision: Union[str, Sequence[str], None] = "001_assistant_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS context_comment_id VARCHAR(64)",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS plan_hash VARCHAR(64)",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb",
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS trace_id VARCHAR(36) "
            "NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text)"
        ),
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS model_calls INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS tool_calls INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS replan_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS max_model_calls INTEGER NOT NULL DEFAULT 6",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS max_tool_calls INTEGER NOT NULL DEFAULT 10",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS max_replans INTEGER NOT NULL DEFAULT 2",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_assistant_runs_trace_id ON assistant_runs (trace_id)",
        """
        CREATE TABLE IF NOT EXISTS assistant_approvals (
            id VARCHAR(36) PRIMARY KEY,
            run_id VARCHAR(36) NOT NULL REFERENCES assistant_runs(id) ON DELETE CASCADE,
            step_ordinal INTEGER NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            action VARCHAR(80) NOT NULL,
            description VARCHAR(240) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
            plan_hash VARCHAR(64) NOT NULL,
            input_hash VARCHAR(64) NOT NULL,
            preview JSONB NOT NULL DEFAULT '{}'::jsonb,
            expected_run_version INTEGER NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_assistant_approval_step UNIQUE (run_id, step_ordinal),
            CONSTRAINT uq_assistant_approval_input UNIQUE (run_id, input_hash)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_assistant_approvals_run_id ON assistant_approvals (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_assistant_approvals_user_id ON assistant_approvals (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_assistant_approvals_status ON assistant_approvals (status)",
        """
        CREATE TABLE IF NOT EXISTS assistant_user_memories (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            key VARCHAR(80) NOT NULL,
            value VARCHAR(1000) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_assistant_user_memory UNIQUE (user_id, key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_assistant_user_memories_user_id ON assistant_user_memories (user_id)",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assistant_user_memories")
    op.execute("DROP TABLE IF EXISTS assistant_approvals")
    for column in [
        "deadline_at",
        "max_replans",
        "max_tool_calls",
        "max_model_calls",
        "replan_count",
        "tool_calls",
        "model_calls",
        "trace_id",
        "checkpoint",
        "plan_hash",
        "context_comment_id",
    ]:
        op.execute(f"ALTER TABLE assistant_runs DROP COLUMN IF EXISTS {column}")
