"""SQLAlchemy metadata table for durable Artifact records."""

from __future__ import annotations

import sqlalchemy as sa

artifact_metadata = sa.MetaData()

artifact_records = sa.Table(
    "artifact_record",
    artifact_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("artifact_id", sa.String(128), nullable=False, unique=True),
    sa.Column("artifact_type", sa.String(128), nullable=False),
    sa.Column("resource_type", sa.String(64)),
    sa.Column("resource_id", sa.String(128)),
    sa.Column("title", sa.String(500)),
    sa.Column("summary", sa.String(1000), nullable=False, default=""),
    sa.Column("result_status", sa.String(64)),
    sa.Column("run_at", sa.String(64)),
    sa.Column("timezone", sa.String(64)),
    sa.Column("step_id", sa.String(128), nullable=False, default=""),
    sa.Column("owner_task_id", sa.String(128), nullable=False, default=""),
    sa.Column("owner_execution_id", sa.String(128), nullable=False, default=""),
    sa.Column("owner_agent", sa.String(128), nullable=False, default=""),
    sa.Column("lifecycle", sa.String(32), nullable=False),
    sa.Column("schema_version", sa.String(128), nullable=False, default=""),
    sa.Column("metadata_json", sa.JSON, nullable=False, default=dict),
    sa.Column("storage_type", sa.String(32), nullable=False, default="INLINE"),
    sa.Column("storage_reference", sa.String(512)),
    sa.Column("content_hash", sa.String(256)),
    sa.Column("version", sa.Integer, nullable=False, default=1),
    sa.Column("size", sa.Integer),
    sa.Column("consumed_by_task_ids", sa.JSON, nullable=False, default=list),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
)

artifact_events = sa.Table(
    "artifact_event",
    artifact_metadata,
    sa.Column("event_id", sa.String(128), primary_key=True),
    sa.Column("artifact_id", sa.String(128), nullable=False),
    sa.Column("artifact_type", sa.String(128), nullable=False),
    sa.Column("execution_id", sa.String(128), nullable=False, default=""),
    sa.Column("task_id", sa.String(128), nullable=False, default=""),
    sa.Column("agent_name", sa.String(128), nullable=False, default=""),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("lifecycle", sa.String(32), nullable=False),
    sa.Column("timestamp", sa.String(64), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False, default=dict),
)


__all__ = ["artifact_metadata", "artifact_records", "artifact_events"]
