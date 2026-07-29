from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moderation.models import ModerationTask
from moderation.schemas import ModerationAction, ModerationTaskStatus, RiskType


class ModerationStatisticsRepository:
    async def total(self, session: AsyncSession) -> int:
        return int(await session.scalar(select(func.count(ModerationTask.id))) or 0)

    async def count_by_status(self, session: AsyncSession) -> dict[ModerationTaskStatus, int]:
        rows = await session.execute(
            select(ModerationTask.status, func.count(ModerationTask.id)).group_by(
                ModerationTask.status
            )
        )
        return {status: int(count) for status, count in rows}

    async def count_by_risk(self, session: AsyncSession) -> dict[RiskType, int]:
        rows = await session.execute(
            select(ModerationTask.risk_type, func.count(ModerationTask.id))
            .where(ModerationTask.risk_type.is_not(None))
            .group_by(ModerationTask.risk_type)
        )
        return {risk_type: int(count) for risk_type, count in rows if risk_type is not None}

    async def count_by_action(self, session: AsyncSession) -> dict[ModerationAction, int]:
        rows = await session.execute(
            select(ModerationTask.final_action, func.count(ModerationTask.id))
            .where(ModerationTask.final_action.is_not(None))
            .group_by(ModerationTask.final_action)
        )
        return {action: int(count) for action, count in rows if action is not None}

    async def disagreement_count(self, session: AsyncSession) -> int:
        statement = select(func.count(ModerationTask.id)).where(
            ModerationTask.human_action.is_not(None),
            ModerationTask.agent_action.is_not(None),
            ModerationTask.agent_action != ModerationAction.HUMAN_REVIEW,
            ModerationTask.human_action != ModerationTask.agent_action,
        )
        return int(await session.scalar(statement) or 0)
