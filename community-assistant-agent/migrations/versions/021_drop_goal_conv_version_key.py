"""Drop the conversation-wide version constraint on ConversationGoal.

The constraint ``assistant_conversation_goals_conversation_id_version_key``
forces every Goal in the same conversation to share a monotonically
increasing version sequence.  When multiple Goals exist, an update to
Goal A (version 8 → 9) collides with Goal B that was independently
created with version 9.  Per-goal optimistic locking uses
``WHERE version = expected_version`` which does not need cross-Goal
uniqueness.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "021_drop_goal_conv_version"
down_revision: Union[str, Sequence[str], None] = "020_execution_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE assistant_conversation_goals "
        "DROP CONSTRAINT IF EXISTS "
        "assistant_conversation_goals_conversation_id_version_key"
    )


def downgrade() -> None:
    pass  # intentionally no-op: recreating this constraint would break multi-Goal conversations
