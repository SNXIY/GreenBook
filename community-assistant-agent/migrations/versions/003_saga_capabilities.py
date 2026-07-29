"""Add durable side-effect ledger and delegated scheduled capabilities.

Revision ID: 003_saga_capabilities
Revises: 002_harness_controls
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "003_saga_capabilities"
down_revision: Union[str, Sequence[str], None] = "002_harness_controls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_assistant_runs_retry_after ON assistant_runs (retry_after)",
        "ALTER TABLE assistant_scheduled_actions ADD COLUMN IF NOT EXISTS capability_token TEXT",
        """
        CREATE TABLE IF NOT EXISTS assistant_side_effects (
            id VARCHAR(36) PRIMARY KEY,
            run_id VARCHAR(36) NOT NULL REFERENCES assistant_runs(id) ON DELETE CASCADE,
            step_ordinal INTEGER NOT NULL,
            tool_name VARCHAR(80) NOT NULL,
            operation_key VARCHAR(160) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            resource_id VARCHAR(128),
            status VARCHAR(24) NOT NULL DEFAULT 'PREPARED',
            attempts INTEGER NOT NULL DEFAULT 0,
            remote_operation_id VARCHAR(128),
            result JSONB,
            error TEXT,
            last_reconciled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_assistant_side_effect_step UNIQUE (run_id, step_ordinal),
            CONSTRAINT uq_assistant_side_effect_operation UNIQUE (operation_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_assistant_side_effects_run_id ON assistant_side_effects (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_assistant_side_effects_status ON assistant_side_effects (status)",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assistant_side_effects")
    op.execute(
        "ALTER TABLE assistant_scheduled_actions "
        "DROP COLUMN IF EXISTS capability_token"
    )
    op.execute("DROP INDEX IF EXISTS ix_assistant_runs_retry_after")
    for column in ["retry_after", "max_attempts", "attempts"]:
        op.execute(f"ALTER TABLE assistant_runs DROP COLUMN IF EXISTS {column}")
