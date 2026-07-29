"""Add runtime identity, draft freshness, and scheduled-attempt audit.

Revision ID: 005_runtime_freshness
Revises: 004_capability_revocation
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "005_runtime_freshness"
down_revision: Union[str, Sequence[str], None] = "004_capability_revocation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS runtime_identity "
            "JSONB NOT NULL DEFAULT '{}'::jsonb"
        ),
        (
            "ALTER TABLE assistant_scheduled_actions ADD COLUMN IF NOT EXISTS "
            "expected_content_sha256 VARCHAR(64)"
        ),
        (
            "UPDATE assistant_scheduled_actions SET expected_content_sha256 = "
            "repeat('0', 64) WHERE expected_content_sha256 IS NULL"
        ),
        (
            "ALTER TABLE assistant_scheduled_actions ALTER COLUMN "
            "expected_content_sha256 SET NOT NULL"
        ),
        """
        CREATE TABLE IF NOT EXISTS assistant_scheduled_action_attempts (
            id VARCHAR(36) PRIMARY KEY,
            action_id VARCHAR(36) NOT NULL
                REFERENCES assistant_scheduled_actions(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'RUNNING',
            worker_id VARCHAR(80) NOT NULL,
            result JSONB,
            error TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_assistant_scheduled_attempt UNIQUE (action_id, attempt)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS ix_assistant_scheduled_attempt_action "
            "ON assistant_scheduled_action_attempts (action_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_assistant_scheduled_attempt_status "
            "ON assistant_scheduled_action_attempts (status)"
        ),
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assistant_scheduled_action_attempts")
    op.execute(
        "ALTER TABLE assistant_scheduled_actions "
        "DROP COLUMN IF EXISTS expected_content_sha256"
    )
    op.execute(
        "ALTER TABLE assistant_runs DROP COLUMN IF EXISTS runtime_identity"
    )
