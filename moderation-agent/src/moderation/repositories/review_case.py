from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moderation.models import ModerationReviewCase, ModerationTask
from moderation.schemas import HumanDecision, RiskType


class ModerationReviewCaseRepository:
    async def create_from_override(
        self,
        session: AsyncSession,
        *,
        task: ModerationTask,
        human_decision: HumanDecision,
    ) -> ModerationReviewCase:
        if task.risk_type is None or task.agent_action is None or task.content_hash is None:
            raise ValueError("Task is missing agent decision data")
        agent_decision = task.agent_decision or {}
        policy_ids = [
            evidence["policy_id"]
            for evidence in agent_decision.get("matched_policies", [])
            if "policy_id" in evidence
        ]
        review_case = ModerationReviewCase(
            original_task_id=task.id,
            content=task.content,
            normalized_content=task.normalized_content or task.content,
            content_hash=task.content_hash,
            platform=task.platform,
            agent_risk_type=task.risk_type,
            agent_action=task.agent_action,
            final_action=human_decision.action,
            final_risk_type=human_decision.risk_type or task.risk_type,
            reviewer_id=human_decision.reviewer_id,
            reviewer_reason=human_decision.comment,
            matched_policy_ids=policy_ids,
        )
        session.add(review_case)
        await session.flush()
        return review_case

    async def list_candidates(
        self,
        session: AsyncSession,
        *,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 100,
    ) -> list[ModerationReviewCase]:
        statement = (
            select(ModerationReviewCase)
            .where(
                ModerationReviewCase.platform.in_([platform, "default"]),
                ModerationReviewCase.agent_risk_type.in_(risk_types),
            )
            .order_by(ModerationReviewCase.created_at.desc())
            .limit(limit)
        )
        return list((await session.scalars(statement)).all())

    async def get(self, session: AsyncSession, case_id: UUID) -> ModerationReviewCase | None:
        return await session.get(ModerationReviewCase, case_id)

    async def get_by_task(
        self,
        session: AsyncSession,
        task_id: UUID,
    ) -> ModerationReviewCase | None:
        return await session.scalar(
            select(ModerationReviewCase).where(ModerationReviewCase.original_task_id == task_id)
        )
