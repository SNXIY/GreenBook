"""Stub: goal contract — schema already applied via create_all."""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "023_goal_contract"
down_revision: Union[str, Sequence[str], None] = "022_capability_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
