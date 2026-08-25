"""SQLAlchemy tables used by the execution persistence adapters."""

from __future__ import annotations

import sqlalchemy as sa

execution_metadata = sa.MetaData()

executions = sa.Table(
    "execution",
    execution_metadata,
    sa.Column("execution_id", sa.String(128), primary_key=True),
    sa.Column("plan_id", sa.String(128), nullable=False),
    sa.Column("task_id", sa.String(128), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("current_step_index", sa.Integer, nullable=False, default=0),
    sa.Column("version", sa.Integer, nullable=False, default=1),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.Column("completed_at", sa.String(64), nullable=False, default=""),
    sa.Column("requires_approval", sa.Boolean, nullable=False, default=False),
    sa.Column("has_side_effects", sa.Boolean, nullable=False, default=False),
)

execution_controls = sa.Table(
    "execution_control",
    execution_metadata,
    sa.Column("execution_id", sa.String(128), primary_key=True),
    sa.Column("state", sa.String(32), nullable=False, default="RUNNING"),
    sa.Column("reason", sa.Text, nullable=False, default=""),
    sa.Column("requested_at", sa.String(64), nullable=False, default=""),
    sa.Column("updated_at", sa.String(64), nullable=False),
)

execution_steps = sa.Table(
    "execution_step",
    execution_metadata,
    sa.Column("step_execution_id", sa.String(128), primary_key=True),
    sa.Column("step_id", sa.String(128), nullable=False),
    sa.Column("execution_id", sa.String(128), nullable=False),
    sa.Column("capability", sa.String(256), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False, default=0),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("retry_count", sa.Integer, nullable=False, default=0),
    sa.Column("max_retries", sa.Integer, nullable=False, default=3),
    sa.Column("error_code", sa.String(128), nullable=False, default=""),
    sa.Column("error_message", sa.Text, nullable=False, default=""),
    sa.Column("checkpoint_data", sa.JSON, nullable=False, default=dict),
    sa.Column("started_at", sa.String(64), nullable=False, default=""),
    sa.Column("completed_at", sa.String(64), nullable=False, default=""),
    sa.Column("version", sa.Integer, nullable=False, default=1),
    sa.Column("input_artifact_types", sa.JSON, nullable=False, default=list),
    sa.Column("output_artifact_type", sa.String(128), nullable=False, default=""),
    sa.Column("depends_on", sa.JSON, nullable=False, default=list),
    sa.Column("input_artifacts", sa.JSON, nullable=False, default=list),
    sa.Column("output_artifact", sa.JSON),
)

execution_events = sa.Table(
    "execution_event",
    execution_metadata,
    sa.Column("event_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("step_id", sa.String(128)),
    sa.Column("payload", sa.JSON, nullable=False, default=dict),
    sa.Column("created_at", sa.String(64), nullable=False),
)

checkpoints = sa.Table(
    "checkpoint",
    execution_metadata,
    sa.Column("checkpoint_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("execution_id", sa.String(128), nullable=False),
    sa.Column("step_id", sa.String(128), nullable=False, default=""),
    sa.Column("snapshot", sa.JSON, nullable=False, default=dict),
    sa.Column("created_at", sa.String(64), nullable=False),
)

execution_leases = sa.Table(
    "execution_lease",
    execution_metadata,
    sa.Column("execution_id", sa.String(128), primary_key=True),
    sa.Column("worker_id", sa.String(128), nullable=False),
    sa.Column("lease_until", sa.String(64), nullable=False),
)

external_operations = sa.Table(
    "external_operation",
    execution_metadata,
    sa.Column("operation_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False),
    sa.Column("step_id", sa.String(128), nullable=False),
    sa.Column("tool_name", sa.String(256), nullable=False, default=""),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("external_operation_id", sa.String(256)),
    sa.Column("receipt_id", sa.String(256)),
    sa.Column("idempotency_key", sa.String(256)),
    sa.Column("runtime_idempotency_key", sa.String(256)),
    sa.Column("external_idempotency_key", sa.String(256)),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.Column("evidence", sa.JSON),
    sa.Column("trace_id", sa.String(128), default=""),
    sa.Column("conversation_id", sa.String(128), default=""),
    sa.Column("semantic_action", sa.String(128), default=""),
    sa.Column("resource_type", sa.String(64), default=""),
    sa.Column("resource_id", sa.String(256), default=""),
    sa.Column("expected_postcondition", sa.JSON, default=dict),
    sa.Column("attempt", sa.Integer, default=0),
    sa.Column("claim_owner", sa.String(256), default=""),
    sa.Column("claim_version", sa.Integer, default=0),
    sa.Column("fencing_token", sa.String(256), default=""),
    sa.Column("lease_expires_at", sa.String(64), default=""),
    sa.Column("side_effect_started", sa.Boolean, default=False),
    sa.Column("reconciliation_needed", sa.Boolean, default=False),
    sa.Column("retry_classification", sa.String(32), default=""),
    sa.Column("reconcile_attempts", sa.Integer, default=0),
    sa.Column("verified_status", sa.String(32), default=""),
    sa.Column("verified_reason", sa.String(512), default=""),
    sa.Column("next_reconcile_at", sa.String(64), default=""),
)

retry_tasks = sa.Table(
    "retry_task",
    execution_metadata,
    sa.Column("task_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False),
    sa.Column("step_id", sa.String(128), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("next_retry_time", sa.String(64), nullable=False),
    sa.Column("backoff", sa.Float, nullable=False, default=0.0),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("retry_budget", sa.Integer, nullable=False, default=1),
    sa.Column("max_attempts", sa.Integer, nullable=False, default=1),
    sa.Column("deadline", sa.String(64)),
    sa.Column("operation_id", sa.String(128)),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("claimed_by", sa.String(128)),
    sa.Column("claim_until", sa.String(64)),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.UniqueConstraint(
        "execution_id",
        "step_id",
        "attempt",
        name="uq_retry_task_execution_step_attempt",
    ),
)

execution_queue_messages = sa.Table(
    "execution_queue_message",
    execution_metadata,
    sa.Column("message_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False, unique=True),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("available_at", sa.String(64), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False, default=0),
    sa.Column("trace_id", sa.String(128), nullable=False, default=""),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("claimed_by", sa.String(128)),
    sa.Column("claim_until", sa.String(64)),
    sa.Column("last_error", sa.Text, nullable=False, default=""),
    sa.Column("payload", sa.JSON, nullable=False, default=dict),
    sa.Column("updated_at", sa.String(64), nullable=False),
)


__all__ = [
    "execution_metadata",
    "executions",
    "execution_steps",
    "execution_events",
    "checkpoints",
    "execution_leases",
    "external_operations",
    "retry_tasks",
    "execution_queue_messages",
    "execution_controls",
]
