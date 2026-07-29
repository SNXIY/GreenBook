"""Create the initial Python moderation platform tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    op.create_table(
        "moderation_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "risk_type",
            enum("NORMAL", "ADVERTISING", "ABUSE", "PRIVACY", name="moderation_policy_risk_type"),
            nullable=False,
        ),
        sa.Column(
            "default_action",
            enum(
                "PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_policy_default_action"
            ),
            nullable=False,
        ),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_policy"),
        sa.UniqueConstraint(
            "platform",
            "code",
            name="uq_moderation_policy_platform_code",
        ),
    )
    op.create_index("ix_moderation_policy_platform", "moderation_policy", ["platform"])
    op.create_index("ix_moderation_policy_enabled", "moderation_policy", ["enabled"])
    op.create_index("ix_moderation_policy_risk_type", "moderation_policy", ["risk_type"])
    op.create_index(
        "ix_moderation_policy_lookup",
        "moderation_policy",
        ["platform", "risk_type", "enabled", "priority"],
    )

    op.create_table(
        "moderation_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("content_id", sa.String(256), nullable=True),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("creator_id", sa.String(128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            enum(
                "PENDING",
                "RUNNING",
                "WAITING_REVIEW",
                "COMPLETED",
                "FAILED",
                name="moderation_task_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "risk_type",
            enum("NORMAL", "ADVERTISING", "ABUSE", "PRIVACY", name="moderation_risk_type"),
            nullable=True,
        ),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "agent_action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_agent_action"),
            nullable=True,
        ),
        sa.Column("agent_reason", sa.Text(), nullable=True),
        sa.Column("agent_decision", sa.JSON(), nullable=True),
        sa.Column(
            "human_action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_human_action"),
            nullable=True,
        ),
        sa.Column("human_reviewer_id", sa.String(128), nullable=True),
        sa.Column("human_comment", sa.Text(), nullable=True),
        sa.Column("human_decision", sa.JSON(), nullable=True),
        sa.Column(
            "final_action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_final_action"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_task"),
    )
    op.create_index(
        "ix_moderation_task_thread_id",
        "moderation_task",
        ["thread_id"],
        unique=True,
    )
    op.create_index("ix_moderation_task_content_hash", "moderation_task", ["content_hash"])
    op.create_index("ix_moderation_task_platform", "moderation_task", ["platform"])
    op.create_index("ix_moderation_task_status", "moderation_task", ["status"])
    op.create_index("ix_moderation_task_risk_type", "moderation_task", ["risk_type"])
    op.create_index("ix_moderation_task_final_action", "moderation_task", ["final_action"])
    op.create_index(
        "ix_moderation_task_status_created_at",
        "moderation_task",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_moderation_task_platform_content_id",
        "moderation_task",
        ["platform", "content_id"],
    )

    op.create_table(
        "moderation_action_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event",
            enum(
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
                name="moderation_action_log_event",
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            enum("AGENT", "HUMAN", "SYSTEM", name="moderation_decision_source"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column(
            "action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_log_action"),
            nullable=True,
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["moderation_task.id"],
            name="fk_moderation_action_log_task_id_moderation_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_action_log"),
    )
    op.create_index(
        "ix_moderation_action_log_task_id",
        "moderation_action_log",
        ["task_id"],
    )
    op.create_index(
        "ix_moderation_action_log_task_created",
        "moderation_action_log",
        ["task_id", "created_at"],
    )

    op.create_table(
        "moderation_review_case",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("original_task_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column(
            "agent_risk_type",
            enum("NORMAL", "ADVERTISING", "ABUSE", "PRIVACY", name="moderation_case_risk_type"),
            nullable=False,
        ),
        sa.Column(
            "agent_action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_case_agent_action"),
            nullable=False,
        ),
        sa.Column(
            "final_action",
            enum("PASS", "REJECT", "LIMIT", "HUMAN_REVIEW", name="moderation_case_final_action"),
            nullable=False,
        ),
        sa.Column("reviewer_id", sa.String(128), nullable=False),
        sa.Column("reviewer_reason", sa.Text(), nullable=True),
        sa.Column("matched_policy_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["original_task_id"],
            ["moderation_task.id"],
            name="fk_moderation_review_case_original_task_id_moderation_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_review_case"),
    )
    op.create_index(
        "ix_moderation_review_case_original_task_id",
        "moderation_review_case",
        ["original_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_moderation_review_case_content_hash",
        "moderation_review_case",
        ["content_hash"],
    )
    op.create_index(
        "ix_moderation_review_case_platform",
        "moderation_review_case",
        ["platform"],
    )
    op.create_index(
        "ix_moderation_review_case_lookup",
        "moderation_review_case",
        ["platform", "agent_risk_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("moderation_review_case")
    op.drop_table("moderation_action_log")
    op.drop_table("moderation_task")
    op.drop_table("moderation_policy")
