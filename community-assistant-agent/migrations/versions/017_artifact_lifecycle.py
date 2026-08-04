"""Persist immutable artifact lifecycle metadata and relations."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "017_artifact_lifecycle"
down_revision: Union[str, Sequence[str], None] = "016_intent_delta_target_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def has_column(table: str, column: str) -> bool:
        return column in {item["name"] for item in inspector.get_columns(table)}

    def has_index(table: str, name: str) -> bool:
        return name in {item["name"] for item in inspector.get_indexes(table)}

    if not has_column("assistant_artifacts", "parent_artifact_id"):
        op.add_column(
            "assistant_artifacts",
            sa.Column("parent_artifact_id", sa.String(length=36), nullable=True),
        )
    if not has_column("assistant_artifacts", "change_type"):
        op.add_column(
            "assistant_artifacts",
            sa.Column("change_type", sa.String(length=32), nullable=True),
        )
    if not has_index("assistant_artifacts", "ix_assistant_artifacts_parent_artifact_id"):
        op.create_index(
            "ix_assistant_artifacts_parent_artifact_id",
            "assistant_artifacts",
            ["parent_artifact_id"],
        )
    if not has_index("assistant_artifacts", "ix_assistant_artifacts_change_type"):
        op.create_index(
            "ix_assistant_artifacts_change_type",
            "assistant_artifacts",
            ["change_type"],
        )
    if not has_column("assistant_target_bindings", "content_artifact_id"):
        op.add_column(
            "assistant_target_bindings",
            sa.Column("content_artifact_id", sa.String(length=128), nullable=True),
        )
    if not has_column("assistant_target_bindings", "content_artifact_version"):
        op.add_column(
            "assistant_target_bindings",
            sa.Column("content_artifact_version", sa.Integer(), nullable=True),
        )
    if not has_index(
        "assistant_target_bindings",
        "ix_assistant_target_bindings_content_artifact_id",
    ):
        op.create_index(
            "ix_assistant_target_bindings_content_artifact_id",
            "assistant_target_bindings",
            ["content_artifact_id"],
        )
    if "assistant_artifact_relations" not in inspector.get_table_names():
        op.create_table(
            "assistant_artifact_relations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "source_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("assistant_artifacts.id"),
                nullable=False,
            ),
            sa.Column(
                "target_artifact_id",
                sa.String(length=36),
                sa.ForeignKey("assistant_artifacts.id"),
                nullable=False,
            ),
            sa.Column("relation_type", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "source_artifact_id",
                "target_artifact_id",
                "relation_type",
            ),
        )
        inspector = sa.inspect(bind)
    if not has_index(
        "assistant_artifact_relations",
        "ix_assistant_artifact_relations_source_artifact_id",
    ):
        op.create_index(
            "ix_assistant_artifact_relations_source_artifact_id",
            "assistant_artifact_relations",
            ["source_artifact_id"],
        )
    if not has_index(
        "assistant_artifact_relations",
        "ix_assistant_artifact_relations_target_artifact_id",
    ):
        op.create_index(
            "ix_assistant_artifact_relations_target_artifact_id",
            "assistant_artifact_relations",
            ["target_artifact_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_artifact_relations_target_artifact_id",
        table_name="assistant_artifact_relations",
    )
    op.drop_index(
        "ix_assistant_artifact_relations_source_artifact_id",
        table_name="assistant_artifact_relations",
    )
    op.drop_table("assistant_artifact_relations")
    op.drop_index(
        "ix_assistant_target_bindings_content_artifact_id",
        table_name="assistant_target_bindings",
    )
    op.drop_column("assistant_target_bindings", "content_artifact_version")
    op.drop_column("assistant_target_bindings", "content_artifact_id")
    op.drop_index(
        "ix_assistant_artifacts_change_type",
        table_name="assistant_artifacts",
    )
    op.drop_index(
        "ix_assistant_artifacts_parent_artifact_id",
        table_name="assistant_artifacts",
    )
    op.drop_column("assistant_artifacts", "change_type")
    op.drop_column("assistant_artifacts", "parent_artifact_id")
