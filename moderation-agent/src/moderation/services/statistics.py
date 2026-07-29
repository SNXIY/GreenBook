from database import DatabaseManager
from moderation.repositories import ModerationStatisticsRepository
from moderation.schemas import ModerationStatistics, ModerationTaskStatus


class ModerationStatisticsService:
    def __init__(self, database: DatabaseManager) -> None:
        self.database = database
        self.repository = ModerationStatisticsRepository()

    async def get(self) -> ModerationStatistics:
        async with self.database.session() as session:
            by_status = await self.repository.count_by_status(session)
            return ModerationStatistics(
                total_tasks=await self.repository.total(session),
                pending_review=by_status.get(ModerationTaskStatus.WAITING_REVIEW, 0),
                agent_human_disagreements=await self.repository.disagreement_count(session),
                by_status=by_status,
                by_risk_type=await self.repository.count_by_risk(session),
                by_action=await self.repository.count_by_action(session),
            )
