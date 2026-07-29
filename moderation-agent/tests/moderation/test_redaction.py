from uuid import uuid4

import pytest

from database import DatabaseManager
from moderation.repositories import ModerationActionLogRepository, ModerationTaskRepository
from moderation.schemas import (
    ActionLogEvent,
    DecisionSource,
    ModerationTaskCreate,
)
from moderation.security import redact_data, redact_text


def test_sensitive_values_are_redacted_recursively() -> None:
    assert redact_text("Call 13812345678 now") == "Call 138****5678 now"
    assert redact_text("ID 11010519491231002X") == "ID 110***********002X"
    assert redact_text("Email alice@example.com") == "Email a***@example.com"
    assert redact_data(
        {
            "content": "Phone 13812345678",
            "items": ["11010519491231002X", "alice@example.com"],
        }
    ) == {
        "content": "Phone 138****5678",
        "items": ["110***********002X", "a***@example.com"],
    }


@pytest.mark.asyncio
async def test_action_log_redacts_sensitive_details(tmp_path) -> None:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'redaction.db'}")
    task_id = uuid4()
    try:
        async with database.session() as session:
            await ModerationTaskRepository().create(
                session,
                task_id=task_id,
                thread_id=str(uuid4()),
                request=ModerationTaskCreate(content="Call 13812345678"),
            )
            await ModerationActionLogRepository().add(
                session,
                task_id=task_id,
                event=ActionLogEvent.TASK_CREATED,
                source=DecisionSource.SYSTEM,
                details={"raw": "Call 13812345678"},
            )
            await session.commit()

        async with database.session() as session:
            logs = await ModerationActionLogRepository().list_for_task(session, task_id)
        assert logs[0].details == {"raw": "Call 138****5678"}
    finally:
        await database.close()
