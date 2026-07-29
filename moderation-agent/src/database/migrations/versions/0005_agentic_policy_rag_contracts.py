"""Add Agentic Policy RAG facts and task audit storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table("moderation_policy") as batch:
        batch.add_column(
            sa.Column(
                "applicability_conditions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "exclusion_conditions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "violation_examples",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "safe_examples",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "severity",
                enum(
                    "LOW",
                    "MEDIUM",
                    "HIGH",
                    "CRITICAL",
                    name="moderation_policy_severity",
                ),
                nullable=False,
                server_default=sa.text("'MEDIUM'"),
            )
        )
        batch.add_column(
            sa.Column(
                "suggested_actions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "tags",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE moderation_policy SET effective_at = created_at WHERE effective_at IS NULL")
    )
    connection.execute(
        sa.text(
            "UPDATE moderation_policy SET severity = CASE "
            "WHEN risk_type = 'PRIVACY' THEN 'CRITICAL' "
            "WHEN default_action = 'PASS' THEN 'LOW' "
            "WHEN default_action = 'REJECT' THEN 'HIGH' "
            "ELSE 'MEDIUM' END"
        )
    )

    policy_table = sa.table(
        "moderation_policy",
        sa.column("id", sa.Uuid()),
        sa.column("default_action", sa.String()),
        sa.column("suggested_actions", sa.JSON()),
    )
    policies = connection.execute(sa.select(policy_table.c.id, policy_table.c.default_action)).all()
    for policy_id, default_action in policies:
        connection.execute(
            sa.update(policy_table)
            .where(policy_table.c.id == policy_id)
            .values(suggested_actions=[default_action])
        )

    with op.batch_alter_table("moderation_policy") as batch:
        batch.alter_column("effective_at", nullable=False)
        batch.create_index("ix_moderation_policy_severity", ["severity"])
        batch.create_index("ix_moderation_policy_effective_at", ["effective_at"])
        batch.create_index("ix_moderation_policy_expires_at", ["expires_at"])

    with op.batch_alter_table("moderation_task") as batch:
        batch.add_column(sa.Column("policy_rag", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("moderation_task") as batch:
        batch.drop_column("policy_rag")

    with op.batch_alter_table("moderation_policy") as batch:
        batch.drop_index("ix_moderation_policy_expires_at")
        batch.drop_index("ix_moderation_policy_effective_at")
        batch.drop_index("ix_moderation_policy_severity")
        batch.drop_column("expires_at")
        batch.drop_column("effective_at")
        batch.drop_column("tags")
        batch.drop_column("suggested_actions")
        batch.drop_column("severity")
        batch.drop_column("safe_examples")
        batch.drop_column("violation_examples")
        batch.drop_column("exclusion_conditions")
        batch.drop_column("applicability_conditions")
