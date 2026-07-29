from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moderation.models import ModerationSignal
from moderation.schemas import ModerationSignalEvidence
from moderation.security import redact_data


class ModerationSignalRepository:
    async def add_many(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        signals: list[ModerationSignalEvidence],
    ) -> list[ModerationSignal]:
        records = [
            ModerationSignal(
                task_id=task_id,
                signal_type=signal.signal_type,
                source=signal.source,
                score=signal.score,
                details=redact_data(signal.details),
            )
            for signal in signals
        ]
        session.add_all(records)
        await session.flush()
        return records

    async def list_for_task(
        self,
        session: AsyncSession,
        task_id: UUID,
    ) -> list[ModerationSignal]:
        statement = (
            select(ModerationSignal)
            .where(ModerationSignal.task_id == task_id)
            .order_by(ModerationSignal.created_at.asc())
        )
        return list((await session.scalars(statement)).all())
