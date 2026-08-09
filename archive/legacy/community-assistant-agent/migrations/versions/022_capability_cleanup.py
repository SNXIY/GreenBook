"""Stub: capability cleanup — schema already applied via create_all."""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "022_capability_cleanup"
down_revision: Union[str, Sequence[str], None] = "021_drop_goal_conv_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
