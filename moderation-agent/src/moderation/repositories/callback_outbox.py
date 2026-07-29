from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.base import utc_now
from moderation.models import ModerationCallbackOutbox, ModerationTask
from moderation.repositories.task import _supports_skip_locked


class ModerationCallbackOutboxRepository:
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        task: ModerationTask,
        max_attempts: int,
    ) -> ModerationCallbackOutbox:
        delivery = await session.scalar(
            select(ModerationCallbackOutbox)
            .where(ModerationCallbackOutbox.task_id == task.id)
            .with_for_update()
        )
        now = utc_now()
        if delivery is None:
            delivery = ModerationCallbackOutbox(
                task_id=task.id,
                task_version=task.version,
                status="PENDING",
                max_attempts=max_attempts,
                available_at=now,
            )
            session.add(delivery)
        elif delivery.task_version < task.version:
            delivery.task_version = task.version
            delivery.status = "PENDING"
            delivery.attempts = 0
            delivery.max_attempts = max_attempts
            delivery.available_at = now
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.last_http_status = None
            delivery.last_error = None
            delivery.delivered_at = None
            delivery.updated_at = now
        await session.flush()
        return delivery

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> ModerationCallbackOutbox | None:
        now = utc_now()
        statement = (
            select(ModerationCallbackOutbox)
            .where(
                ModerationCallbackOutbox.attempts
                < ModerationCallbackOutbox.max_attempts,
                or_(
                    and_(
                        ModerationCallbackOutbox.status.in_(
                            ["PENDING", "RETRYING"]
                        ),
                        ModerationCallbackOutbox.available_at <= now,
                    ),
                    and_(
                        ModerationCallbackOutbox.status == "DELIVERING",
                        ModerationCallbackOutbox.lease_expires_at.is_not(None),
                        ModerationCallbackOutbox.lease_expires_at < now,
                    ),
                ),
            )
            .order_by(ModerationCallbackOutbox.available_at.asc())
            .limit(1)
        )
        if _supports_skip_locked(session):
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        delivery = await session.scalar(statement)
        if delivery is None:
            return None
        delivery.status = "DELIVERING"
        delivery.attempts += 1
        delivery.lease_owner = worker_id[:128]
        delivery.lease_expires_at = now + timedelta(seconds=lease_seconds)
        delivery.last_error = None
        delivery.updated_at = now
        await session.flush()
        return delivery

    async def mark_delivered(
        self,
        session: AsyncSession,
        *,
        delivery_id: UUID,
        worker_id: str,
        expected_attempt: int,
        task_version: int,
    ) -> bool:
        delivery = await session.get(
            ModerationCallbackOutbox,
            delivery_id,
            with_for_update=True,
        )
        if not self._owns(
            delivery,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            task_version=task_version,
        ):
            return False
        now = utc_now()
        delivery.status = "DELIVERED"
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        delivery.last_http_status = 204
        delivery.delivered_at = now
        delivery.updated_at = now
        await session.flush()
        return True

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        delivery_id: UUID,
        worker_id: str,
        expected_attempt: int,
        task_version: int,
        error: str,
        http_status: int | None,
        retry_base_seconds: float,
        retry_max_seconds: float,
    ) -> bool:
        delivery = await session.get(
            ModerationCallbackOutbox,
            delivery_id,
            with_for_update=True,
        )
        if not self._owns(
            delivery,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            task_version=task_version,
        ):
            return False
        now = utc_now()
        delivery.last_error = error[:2_000]
        delivery.last_http_status = http_status
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        if delivery.attempts >= delivery.max_attempts:
            delivery.status = "DEAD"
            delivery.available_at = now
        else:
            delivery.status = "RETRYING"
            delay = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** max(0, delivery.attempts - 1)),
            )
            delivery.available_at = now + timedelta(seconds=delay)
        delivery.updated_at = now
        await session.flush()
        return True

    async def list(
        self,
        session: AsyncSession,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ModerationCallbackOutbox]:
        statement = select(ModerationCallbackOutbox)
        if status:
            statement = statement.where(ModerationCallbackOutbox.status == status)
        statement = (
            statement.order_by(ModerationCallbackOutbox.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.scalars(statement)).all())

    @staticmethod
    def _owns(
        delivery: ModerationCallbackOutbox | None,
        *,
        worker_id: str,
        expected_attempt: int,
        task_version: int,
    ) -> bool:
        return bool(
            delivery is not None
            and delivery.status == "DELIVERING"
            and delivery.lease_owner == worker_id
            and delivery.attempts == expected_attempt
            and delivery.task_version == task_version
        )
