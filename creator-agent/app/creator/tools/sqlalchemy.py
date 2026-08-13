from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.infrastructure.sqlalchemy import CreatorBase
from app.creator.tools.errors import CreatorToolAuditError
from app.creator.tools.models import (
    CreatorToolCallAudit,
    CreatorToolCallStatus,
    CreatorToolRisk,
)


class CreatorToolCallRow(CreatorBase):
    __tablename__ = "creator_tool_calls"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("creator_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    caller: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    risk: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SqlAlchemyCreatorToolAuditStore:
    backend_name = "postgresql"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sessions = sessions

    async def start(self, audit: CreatorToolCallAudit) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                CreatorToolCallRow(
                    call_id=audit.call_id,
                    trace_id=audit.trace_id,
                    task_id=audit.task_id,
                    run_id=audit.run_id,
                    tenant_id=audit.tenant_id,
                    creator_id=audit.creator_id,
                    actor_id=audit.actor_id,
                    caller=audit.caller,
                    tool_name=audit.tool_name,
                    risk=audit.risk.value,
                    arguments_sha256=audit.arguments_sha256,
                    status=audit.status.value,
                    started_at=audit.started_at,
                )
            )

    async def finish(
        self,
        *,
        call_id: str,
        status: CreatorToolCallStatus,
        finished_at,
        latency_ms: int,
        result_sha256: str | None,
        result_size_bytes: int | None,
        error_code: str | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(CreatorToolCallRow)
                .where(
                    CreatorToolCallRow.call_id == call_id,
                    CreatorToolCallRow.status
                    == CreatorToolCallStatus.RUNNING.value,
                )
                .values(
                    status=status.value,
                    finished_at=finished_at,
                    latency_ms=latency_ms,
                    result_sha256=result_sha256,
                    result_size_bytes=result_size_bytes,
                    error_code=error_code,
                )
            )
            if result.rowcount != 1:
                raise CreatorToolAuditError(
                    f"Tool audit {call_id} could not be finalized",
                    call_id=call_id,
                )

    async def get(self, call_id: str) -> CreatorToolCallAudit | None:
        async with self._sessions() as session:
            row = await session.get(CreatorToolCallRow, call_id)
        return _from_row(row) if row is not None else None


def _from_row(row: CreatorToolCallRow) -> CreatorToolCallAudit:
    return CreatorToolCallAudit(
        call_id=row.call_id,
        trace_id=row.trace_id,
        task_id=row.task_id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        actor_id=row.actor_id,
        caller=row.caller,
        tool_name=row.tool_name,
        risk=CreatorToolRisk(row.risk),
        arguments_sha256=row.arguments_sha256,
        status=CreatorToolCallStatus(row.status),
        started_at=_as_utc(row.started_at),
        finished_at=_as_utc(row.finished_at) if row.finished_at else None,
        latency_ms=row.latency_ms,
        result_sha256=row.result_sha256,
        result_size_bytes=row.result_size_bytes,
        error_code=row.error_code,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
