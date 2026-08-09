"""Stub: tool invocation facts — schema already applied via create_all."""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "024_tool_invocation_facts"
down_revision: Union[str, Sequence[str], None] = "023_goal_contract"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
