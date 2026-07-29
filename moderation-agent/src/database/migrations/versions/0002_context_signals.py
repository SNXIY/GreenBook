"""Add moderation context signals and review idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column(
                "content_type",
                enum("TEXT", "POST", "COMMENT", name="moderation_content_type"),
                nullable=False,
                server_default=sa.text("'TEXT'"),
            )
        )
        batch.add_column(
            sa.Column(
                "human_risk_type",
                enum(
                    "NORMAL",
                    "ADVERTISING",
                    "ABUSE",
                    "PRIVACY",
                    name="moderation_human_risk_type",
                ),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "final_risk_type",
                enum(
                    "NORMAL",
                    "ADVERTISING",
                    "ABUSE",
                    "PRIVACY",
                    name="moderation_final_risk_type",
                ),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("review_idempotency_key", sa.String(128), nullable=True))
        batch.create_unique_constraint(
            "uq_moderation_task_idempotency_key",
            ["idempotency_key"],
        )
        batch.create_unique_constraint(
            "uq_moderation_task_review_idempotency_key",
            ["review_idempotency_key"],
        )
        batch.create_index("ix_moderation_task_content_type", ["content_type"])
        batch.create_index("ix_moderation_task_final_risk_type", ["final_risk_type"])
    op.execute(
        sa.text(
            "UPDATE moderation_task SET final_risk_type = risk_type "
            "WHERE final_risk_type IS NULL"
        )
    )
    with op.batch_alter_table("moderation_task") as batch:
        batch.alter_column("content_type", server_default=None)

    with op.batch_alter_table("moderation_review_case") as batch:
        batch.add_column(
            sa.Column(
                "final_risk_type",
                enum(
                    "NORMAL",
                    "ADVERTISING",
                    "ABUSE",
                    "PRIVACY",
                    name="moderation_case_final_risk_type",
                ),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            "UPDATE moderation_review_case "
            "SET final_risk_type = agent_risk_type "
            "WHERE final_risk_type IS NULL"
        )
    )
    with op.batch_alter_table("moderation_review_case") as batch:
        batch.alter_column("final_risk_type", nullable=False)

    op.create_table(
        "moderation_signal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "signal_type",
            enum(
                "TEXT_PATTERN",
                "REPORT_COUNT",
                "AUTHOR_VIOLATION_HISTORY",
                "CONTEXT_INCOMPLETE",
                name="moderation_signal_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            enum(
                "CONTENT",
                "COMMUNITY",
                "REPORT",
                name="moderation_signal_source",
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["moderation_task.id"],
            name="fk_moderation_signal_task_id_moderation_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_signal"),
    )
    op.create_index("ix_moderation_signal_task_id", "moderation_signal", ["task_id"])
    op.create_index(
        "ix_moderation_signal_signal_type",
        "moderation_signal",
        ["signal_type"],
    )
    op.create_index("ix_moderation_signal_source", "moderation_signal", ["source"])
    op.create_index(
        "ix_moderation_signal_task_created",
        "moderation_signal",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("moderation_signal")
    with op.batch_alter_table("moderation_review_case") as batch:
        batch.drop_column("final_risk_type")
    with op.batch_alter_table("moderation_task") as batch:
        batch.drop_index("ix_moderation_task_final_risk_type")
        batch.drop_index("ix_moderation_task_content_type")
        batch.drop_constraint(
            "uq_moderation_task_review_idempotency_key",
            type_="unique",
        )
        batch.drop_constraint(
            "uq_moderation_task_idempotency_key",
            type_="unique",
        )
        batch.drop_column("review_idempotency_key")
        batch.drop_column("final_risk_type")
        batch.drop_column("human_risk_type")
        batch.drop_column("content_type")
        batch.drop_column("idempotency_key")
