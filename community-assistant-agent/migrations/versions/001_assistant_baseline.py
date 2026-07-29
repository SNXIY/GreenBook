"""Community Assistant durable harness baseline.

Revision ID: 001_assistant_baseline
Revises:
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op

from app.database import Base


revision: str = "001_assistant_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This is the immutable baseline. Future changes must use explicit Alembic
    # operations in a new revision rather than editing this revision.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)

