from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moderation.models import ModerationActionLog
from moderation.schemas import (
    ActionLogEvent,
    DecisionSource,
    ModerationAction,
)
from moderation.security import redact_data


class ModerationActionLogRepository:
    async def add(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        event: ActionLogEvent,
        source: DecisionSource,
        actor_id: str | None = None,
        action: ModerationAction | None = None,
        details: dict[str, Any] | None = None,
    ) -> ModerationActionLog:
        log = ModerationActionLog(
            task_id=task_id,
            event=event,
            source=source,
            actor_id=actor_id,
            action=action,
            details=redact_data(details or {}),
        )
        session.add(log)
        await session.flush()
        return log

    async def list_for_task(
        self,
        session: AsyncSession,
        task_id: UUID,
        *,
        event: ActionLogEvent | None = None,
    ) -> list[ModerationActionLog]:
        statement = select(ModerationActionLog).where(ModerationActionLog.task_id == task_id)
        if event is not None:
            statement = statement.where(ModerationActionLog.event == event)
        statement = statement.order_by(ModerationActionLog.created_at.asc())
        return list((await session.scalars(statement)).all())
