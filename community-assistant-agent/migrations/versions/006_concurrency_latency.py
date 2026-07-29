"""Add run concurrency timing fields.

Revision ID: 006_concurrency_latency
Revises: 005_runtime_freshness
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "006_concurrency_latency"
down_revision: Union[str, Sequence[str], None] = "005_runtime_freshness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
        "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS "
            "dependency_wait_started_at TIMESTAMPTZ"
        ),
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS "
            "model_duration_ms INTEGER NOT NULL DEFAULT 0"
        ),
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS "
            "tool_duration_ms INTEGER NOT NULL DEFAULT 0"
        ),
        (
            "ALTER TABLE assistant_runs ADD COLUMN IF NOT EXISTS "
            "dependency_wait_ms INTEGER NOT NULL DEFAULT 0"
        ),
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for column in [
        "dependency_wait_ms",
        "tool_duration_ms",
        "model_duration_ms",
        "dependency_wait_started_at",
        "completed_at",
        "started_at",
    ]:
        op.execute(f"ALTER TABLE assistant_runs DROP COLUMN IF EXISTS {column}")
