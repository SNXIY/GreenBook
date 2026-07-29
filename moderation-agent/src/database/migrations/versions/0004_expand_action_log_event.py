"""Expand action-log event storage for the current audit event names."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.interfaces import ReflectedColumn

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_VALUES = (
    "TASK_CREATED",
    "AGENT_DECIDED",
    "REVIEW_REQUESTED",
    "HUMAN_DECIDED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "POLICY_CREATED",
    "CASE_CREATED",
    "SIGNALS_CAPTURED",
    "REPORT_RECEIVED",
    "COMMUNITY_STATUS_UPDATED",
    "CONTEXT_RETRIEVAL_FAILED",
)
EVENT_TYPE = sa.Enum(
    *EVENT_VALUES,
    name="moderation_action_log_event",
    native_enum=False,
)
LEGACY_EVENT_LENGTH = 16
REQUIRED_EVENT_LENGTH = max(map(len, EVENT_VALUES))


def _event_column() -> ReflectedColumn:
    columns = sa.inspect(op.get_bind()).get_columns("moderation_action_log")
    return next(column for column in columns if column["name"] == "event")


def upgrade() -> None:
    column = _event_column()
    existing_type = column["type"]
    existing_length = getattr(existing_type, "length", None)
    if existing_length is None or existing_length >= REQUIRED_EVENT_LENGTH:
        return

    with op.batch_alter_table("moderation_action_log") as batch:
        batch.alter_column(
            "event",
            existing_type=existing_type,
            type_=EVENT_TYPE,
            existing_nullable=bool(column["nullable"]),
        )


def downgrade() -> None:
    connection = op.get_bind()
    has_long_events = connection.execute(
        sa.text("SELECT 1 FROM moderation_action_log WHERE length(event) > :max_length LIMIT 1"),
        {"max_length": LEGACY_EVENT_LENGTH},
    ).first()
    if has_long_events:
        raise RuntimeError(
            "Cannot shrink moderation_action_log.event while newer audit events exist"
        )

    column = _event_column()
    existing_type = column["type"]
    existing_length = getattr(existing_type, "length", None)
    if existing_length is not None and existing_length <= LEGACY_EVENT_LENGTH:
        return

    with op.batch_alter_table("moderation_action_log") as batch:
        batch.alter_column(
            "event",
            existing_type=existing_type,
            type_=sa.String(LEGACY_EVENT_LENGTH),
            existing_nullable=bool(column["nullable"]),
        )
