from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from moderation.models import ModerationPolicy
from moderation.repositories.exceptions import PolicyConflictError
from moderation.schemas import ModerationPolicyCreate, PolicySeverity, RiskType


class ModerationPolicyRepository:
    async def create(
        self, session: AsyncSession, request: ModerationPolicyCreate
    ) -> ModerationPolicy:
        policy = ModerationPolicy(**request.model_dump())
        session.add(policy)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise PolicyConflictError(
                f"Policy code {request.code} already exists for {request.platform}"
            ) from exc
        return policy

    async def get(self, session: AsyncSession, policy_id: UUID) -> ModerationPolicy | None:
        return await session.get(ModerationPolicy, policy_id)

    async def list(
        self,
        session: AsyncSession,
        *,
        platform: str | None = None,
        risk_types: Sequence[RiskType] | None = None,
        enabled_only: bool = False,
    ) -> builtins.list[ModerationPolicy]:
        statement = select(ModerationPolicy)
        if platform is not None:
            statement = statement.where(
                or_(
                    ModerationPolicy.platform == platform,
                    ModerationPolicy.platform == "default",
                )
            )
        if risk_types:
            statement = statement.where(ModerationPolicy.risk_type.in_(risk_types))
        if enabled_only:
            statement = statement.where(ModerationPolicy.enabled.is_(True))
        statement = statement.order_by(ModerationPolicy.priority.asc(), ModerationPolicy.code.asc())
        return list((await session.scalars(statement)).all())

    async def find_by_code(
        self, session: AsyncSession, *, platform: str, code: str
    ) -> ModerationPolicy | None:
        statement = select(ModerationPolicy).where(
            ModerationPolicy.platform == platform,
            ModerationPolicy.code == code,
        )
        return await session.scalar(statement)

    async def list_active(
        self,
        session: AsyncSession,
        *,
        platform: str,
        risk_types: Sequence[RiskType] | None = None,
        severities: Sequence[PolicySeverity] | None = None,
        as_of: datetime | None = None,
    ) -> builtins.list[ModerationPolicy]:
        now = as_of or datetime.now(UTC)
        statement = select(ModerationPolicy).where(
            or_(
                ModerationPolicy.platform == platform,
                ModerationPolicy.platform == "default",
            ),
            ModerationPolicy.enabled.is_(True),
            ModerationPolicy.effective_at <= now,
            or_(ModerationPolicy.expires_at.is_(None), ModerationPolicy.expires_at > now),
        )
        if risk_types:
            statement = statement.where(ModerationPolicy.risk_type.in_(risk_types))
        if severities:
            statement = statement.where(ModerationPolicy.severity.in_(severities))
        statement = statement.order_by(
            ModerationPolicy.priority.asc(),
            ModerationPolicy.version.desc(),
            ModerationPolicy.code.asc(),
        )
        return list((await session.scalars(statement)).all())

    async def get_active_by_ids(
        self,
        session: AsyncSession,
        *,
        policy_ids: Sequence[UUID],
        platform: str,
        risk_types: Sequence[RiskType] | None = None,
        severities: Sequence[PolicySeverity] | None = None,
        as_of: datetime | None = None,
    ) -> builtins.list[ModerationPolicy]:
        if not policy_ids:
            return []
        now = as_of or datetime.now(UTC)
        statement = select(ModerationPolicy).where(
            ModerationPolicy.id.in_(policy_ids),
            or_(
                ModerationPolicy.platform == platform,
                ModerationPolicy.platform == "default",
            ),
            ModerationPolicy.enabled.is_(True),
            ModerationPolicy.effective_at <= now,
            or_(ModerationPolicy.expires_at.is_(None), ModerationPolicy.expires_at > now),
        )
        if risk_types:
            statement = statement.where(ModerationPolicy.risk_type.in_(risk_types))
        if severities:
            statement = statement.where(ModerationPolicy.severity.in_(severities))
        statement = statement.order_by(
            ModerationPolicy.version.desc(),
            ModerationPolicy.priority.asc(),
        )
        return list((await session.scalars(statement)).all())
