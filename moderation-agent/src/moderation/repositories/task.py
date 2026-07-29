from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base import utc_now
from moderation.models import ModerationTask
from moderation.repositories.exceptions import TaskNotFoundError, TaskStateConflictError
from moderation.schemas import (
    AgentDecision,
    HumanDecision,
    ModerationAction,
    ModerationTaskCreate,
    ModerationTaskStatus,
    RiskType,
    adversarial_review_audit_from_state,
    agentic_policy_rag_audit_from_state,
    evidence_collection_audit_from_state,
    evidence_reviewer_audit_from_state,
)
from moderation.security import redact_data

if TYPE_CHECKING:
    from agents.moderation.state import ModerationState


def _supports_skip_locked(session: AsyncSession) -> bool:
    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    return dialect_name == "postgresql"


class ModerationTaskRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        thread_id: str,
        request: ModerationTaskCreate,
    ) -> ModerationTask:
        task = ModerationTask(
            id=task_id,
            thread_id=thread_id,
            trace_id=request.trace_id or str(task_id),
            idempotency_key=request.idempotency_key,
            content=request.content,
            content_type=request.content_type,
            content_id=request.content_id,
            platform=request.platform,
            creator_id=request.creator_id,
            task_metadata=request.metadata,
            status=ModerationTaskStatus.PENDING,
        )
        session.add(task)
        await session.flush()
        return task

    async def find_by_idempotency_key(
        self,
        session: AsyncSession,
        key: str,
    ) -> ModerationTask | None:
        return await session.scalar(
            select(ModerationTask).where(ModerationTask.idempotency_key == key)
        )

    async def get(
        self,
        session: AsyncSession,
        task_id: UUID,
        *,
        for_update: bool = False,
    ) -> ModerationTask:
        statement = select(ModerationTask).where(ModerationTask.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        task = await session.scalar(statement)
        if task is None:
            raise TaskNotFoundError(f"Moderation task {task_id} was not found")
        return task

    async def list_tasks(
        self,
        session: AsyncSession,
        *,
        status: ModerationTaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModerationTask]:
        statement: Select[tuple[ModerationTask]] = select(ModerationTask)
        if status is not None:
            statement = statement.where(ModerationTask.status == status)
        statement = statement.order_by(ModerationTask.created_at.asc()).limit(limit).offset(offset)
        return list((await session.scalars(statement)).all())

    async def outcomes_by_ids(
        self,
        session: AsyncSession,
        task_ids: list[UUID],
    ) -> dict[UUID, tuple[RiskType | None, ModerationAction | None]]:
        if not task_ids:
            return {}
        rows = await session.execute(
            select(
                ModerationTask.id,
                ModerationTask.final_risk_type,
                ModerationTask.final_action,
            ).where(ModerationTask.id.in_(task_ids))
        )
        return {task_id: (risk_type, final_action) for task_id, risk_type, final_action in rows}

    async def apply_agent_state(
        self,
        session: AsyncSession,
        *,
        task: ModerationTask,
        state: Mapping[str, Any] | ModerationState,
        waiting_for_review: bool,
    ) -> ModerationTask:
        decision = AgentDecision.model_validate(state["agent_decision"])
        evidence_collection = evidence_collection_audit_from_state(state)
        if evidence_collection is not None:
            decision = decision.model_copy(update={"evidence_collection": evidence_collection})
        task.normalized_content = state.get("normalized_content")
        task.content_hash = state.get("content_hash")
        task.risk_type = decision.risk_type
        task.risk_score = decision.risk_score
        task.confidence = decision.confidence
        task.agent_action = decision.recommended_action
        task.agent_reason = decision.reason
        task.agent_decision = redact_data(decision.model_dump(mode="json"))
        adversarial_review = adversarial_review_audit_from_state(
            state,
            entered_human_review=waiting_for_review,
        )
        task.adversarial_review = (
            redact_data(adversarial_review.model_dump(mode="json")) if adversarial_review else None
        )
        policy_rag = agentic_policy_rag_audit_from_state(
            state,
            entered_human_review=waiting_for_review,
        )
        task.policy_rag = redact_data(policy_rag.model_dump(mode="json")) if policy_rag else None
        evidence_review = evidence_reviewer_audit_from_state(
            state,
            entered_human_review=waiting_for_review,
        )
        task.evidence_review = (
            redact_data(evidence_review.model_copy(update={"history": []}).model_dump(mode="json"))
            if evidence_review
            else None
        )
        task.final_risk_type = decision.risk_type
        task.version += 1
        if waiting_for_review:
            task.status = ModerationTaskStatus.WAITING_REVIEW
            task.final_action = None
        else:
            task.status = ModerationTaskStatus.COMPLETED
            task.final_action = ModerationAction(state["final_action"])
            task.completed_at = utc_now()
        task.locked_at = None
        task.locked_by = None
        await session.flush()
        return task

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> ModerationTask | None:
        """Atomically claim the oldest PENDING task for a worker."""
        del lease_seconds  # lease duration is enforced by reclaim_stale using locked_at
        statement = (
            select(ModerationTask)
            .where(ModerationTask.status == ModerationTaskStatus.PENDING)
            .order_by(ModerationTask.created_at.asc())
            .limit(1)
        )
        if _supports_skip_locked(session):
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        task = await session.scalar(statement)
        if task is None:
            return None
        now = utc_now()
        task.status = ModerationTaskStatus.RUNNING
        task.locked_at = now
        task.locked_by = worker_id[:128]
        task.attempt_count = int(task.attempt_count or 0) + 1
        task.version += 1
        task.error_message = None
        await session.flush()
        return task

    async def reclaim_stale(
        self,
        session: AsyncSession,
        *,
        lease_seconds: float,
    ) -> int:
        """Return stuck RUNNING tasks to PENDING when their lease expires."""
        cutoff = utc_now() - timedelta(seconds=lease_seconds)
        statement = select(ModerationTask).where(
            ModerationTask.status == ModerationTaskStatus.RUNNING,
            ModerationTask.locked_at.is_not(None),
            ModerationTask.locked_at < cutoff,
        )
        if _supports_skip_locked(session):
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        stale_tasks = list((await session.scalars(statement)).all())
        for task in stale_tasks:
            task.status = ModerationTaskStatus.PENDING
            task.locked_at = None
            task.locked_by = None
            task.version += 1
        if stale_tasks:
            await session.flush()
        return len(stale_tasks)

    async def complete_human_review(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        expected_version: int | None,
        decision: HumanDecision,
        review_idempotency_key: str | None = None,
    ) -> ModerationTask:
        task = await self.get(session, task_id, for_update=True)
        if task.status != ModerationTaskStatus.WAITING_REVIEW:
            raise TaskStateConflictError(f"Task {task_id} is {task.status}, not WAITING_REVIEW")
        if expected_version is not None and task.version != expected_version:
            raise TaskStateConflictError(
                f"Task {task_id} version changed from {expected_version} to {task.version}"
            )
        task.human_action = decision.action
        task.human_risk_type = decision.risk_type
        task.human_reviewer_id = decision.reviewer_id
        task.human_comment = decision.comment
        task.human_decision = decision.model_dump(mode="json")
        task.final_action = decision.action
        task.final_risk_type = decision.risk_type or task.risk_type
        task.review_idempotency_key = review_idempotency_key
        task.status = ModerationTaskStatus.COMPLETED
        task.version += 1
        task.completed_at = utc_now()
        task.locked_at = None
        task.locked_by = None
        await session.flush()
        return task

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        error_message: str,
    ) -> ModerationTask:
        task = await self.get(session, task_id, for_update=True)
        task.status = ModerationTaskStatus.FAILED
        task.error_message = error_message[:2000]
        task.locked_at = None
        task.locked_by = None
        task.version += 1
        await session.flush()
        return task
