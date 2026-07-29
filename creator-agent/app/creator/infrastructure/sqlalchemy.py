from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, overload

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.creator.application.ports import CreatorUnitOfWork
from app.creator.domain.errors import (
    CreatorArtifactConflictError,
    CreatorPersistenceConflictError,
)
from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorDecisionStatus,
    CreatorGoal,
    CreatorHumanDecision,
    CreatorIdempotencyRecord,
    CreatorOutboxMessage,
    CreatorRun,
    CreatorRunEvent,
    CreatorRunStatus,
    CreatorTask,
    CreatorTaskKind,
    CreatorTaskStatus,
    OutboxStatus,
)
from app.creator.runtime.models import ArtifactKind, CreatorArtifact


class CreatorBase(DeclarativeBase):
    """Separate metadata for the Creator control plane."""


class CreatorTaskRow(CreatorBase):
    __tablename__ = "creator_tasks"
    __table_args__ = (
        Index("ix_creator_tasks_scope_status", "tenant_id", "creator_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    goal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active_run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    final_artifact_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_decision_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorRunRow(CreatorBase):
    __tablename__ = "creator_runs"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_creator_runs_thread_id"),
        UniqueConstraint("task_id", "attempt", name="uq_creator_runs_task_attempt"),
        Index("ix_creator_runs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_decision_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorRunEventRow(CreatorBase):
    __tablename__ = "creator_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_creator_run_events_sequence"),
        Index("ix_creator_run_events_task_sequence", "task_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("creator_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorIdempotencyRow(CreatorBase):
    __tablename__ = "creator_idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_creator_idempotency_scope_key"),
        Index("ix_creator_idempotency_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorOutboxRow(CreatorBase):
    __tablename__ = "creator_outbox_events"
    __table_args__ = (
        Index("ix_creator_outbox_delivery", "status", "available_at"),
        Index("ix_creator_outbox_lease", "status", "lease_expires_at"),
        Index("ix_creator_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorArtifactRow(CreatorBase):
    __tablename__ = "creator_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "step_id",
            "kind",
            "revision",
            name="uq_creator_artifacts_step_revision",
        ),
        Index("ix_creator_artifacts_task_kind", "task_id", "kind"),
        Index("ix_creator_artifacts_run_created", "run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("creator_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    producer: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parent_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CreatorHumanDecisionRow(CreatorBase):
    __tablename__ = "creator_human_decisions"
    __table_args__ = (
        UniqueConstraint("interrupt_id", name="uq_creator_human_decisions_interrupt"),
        Index("ix_creator_human_decisions_run_status", "run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creator_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("creator_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    interrupt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_actions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    allowed_option_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_option_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SqlAlchemyCreatorTaskRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(
        self, task_id: str, *, for_update: bool = False
    ) -> CreatorTask | None:
        statement = select(CreatorTaskRow).where(CreatorTaskRow.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return _task_from_row(row) if row is not None else None

    async def add(self, task: CreatorTask) -> None:
        self._session.add(_task_to_row(task))

    async def save(self, task: CreatorTask, *, expected_version: int) -> None:
        result = await self._session.execute(
            update(CreatorTaskRow)
            .where(
                CreatorTaskRow.id == task.id,
                CreatorTaskRow.version == expected_version,
            )
            .values(**_task_values(task))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CreatorPersistenceConflictError(
                f"Task {task.id} optimistic update failed",
                details={"task_id": task.id, "expected_version": expected_version},
            )


class SqlAlchemyCreatorRunRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, run_id: str, *, for_update: bool = False) -> CreatorRun | None:
        statement = select(CreatorRunRow).where(CreatorRunRow.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return _run_from_row(row) if row is not None else None

    async def add(self, run: CreatorRun) -> None:
        self._session.add(_run_to_row(run))

    async def save(self, run: CreatorRun, *, expected_version: int) -> None:
        result = await self._session.execute(
            update(CreatorRunRow)
            .where(
                CreatorRunRow.id == run.id,
                CreatorRunRow.version == expected_version,
            )
            .values(**_run_values(run))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CreatorPersistenceConflictError(
                f"Run {run.id} optimistic update failed",
                details={"run_id": run.id, "expected_version": expected_version},
            )

    async def max_attempt(self, task_id: str) -> int:
        value = await self._session.scalar(
            select(func.max(CreatorRunRow.attempt)).where(
                CreatorRunRow.task_id == task_id
            )
        )
        return int(value or 0)


class SqlAlchemyCreatorEventRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def next_sequence(self, run_id: str) -> int:
        await self._session.flush()
        value = await self._session.scalar(
            select(func.max(CreatorRunEventRow.sequence)).where(
                CreatorRunEventRow.run_id == run_id
            )
        )
        return int(value or 0) + 1

    async def add(self, event: CreatorRunEvent) -> None:
        self._session.add(
            CreatorRunEventRow(
                id=event.id,
                task_id=event.task_id,
                run_id=event.run_id,
                sequence=event.sequence,
                type=event.type,
                payload_json=event.payload,
                trace_id=event.trace_id,
                created_at=event.created_at,
            )
        )


class SqlAlchemyCreatorOutboxRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> CreatorOutboxMessage | None:
        statement = select(CreatorOutboxRow).where(CreatorOutboxRow.id == message_id)
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return _outbox_from_row(row) if row is not None else None

    async def add(self, message: CreatorOutboxMessage) -> None:
        self._session.add(
            CreatorOutboxRow(
                id=message.id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                topic=message.topic,
                payload_json=message.payload,
                status=message.status.value,
                attempts=message.attempts,
                available_at=message.available_at,
                lease_owner=message.lease_owner,
                lease_expires_at=message.lease_expires_at,
                last_error=message.last_error,
                created_at=message.created_at,
                updated_at=message.updated_at,
            )
        )

    async def claim_ready(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[CreatorOutboxMessage, ...]:
        claimable = _claimable_outbox(now)
        rows = (
            await self._session.scalars(
                select(CreatorOutboxRow)
                .where(claimable)
                .order_by(
                    CreatorOutboxRow.available_at,
                    CreatorOutboxRow.created_at,
                    CreatorOutboxRow.id,
                )
                .limit(max(1, limit))
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed: list[CreatorOutboxMessage] = []
        for row in rows:
            result = await self._session.execute(
                update(CreatorOutboxRow)
                .where(
                    CreatorOutboxRow.id == row.id,
                    _claimable_outbox(now),
                )
                .values(
                    status=OutboxStatus.PROCESSING.value,
                    attempts=row.attempts + 1,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                continue
            claimed.append(
                _outbox_from_row(row).model_copy(
                    update={
                        "status": OutboxStatus.PROCESSING,
                        "attempts": row.attempts + 1,
                        "lease_owner": worker_id,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    }
                )
            )
        return tuple(claimed)

    async def renew_lease(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = await self._session.execute(
            update(CreatorOutboxRow)
            .where(
                CreatorOutboxRow.id == message_id,
                CreatorOutboxRow.status == OutboxStatus.PROCESSING.value,
                CreatorOutboxRow.lease_owner == worker_id,
                CreatorOutboxRow.lease_expires_at > now,
            )
            .values(
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def mark_completed(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
    ) -> bool:
        return await self._finish(
            message_id,
            worker_id=worker_id,
            now=now,
            status=OutboxStatus.COMPLETED,
            last_error=None,
        )

    async def mark_retry(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        available_at: datetime,
        last_error: str,
    ) -> bool:
        result = await self._session.execute(
            update(CreatorOutboxRow)
            .where(
                CreatorOutboxRow.id == message_id,
                CreatorOutboxRow.status == OutboxStatus.PROCESSING.value,
                CreatorOutboxRow.lease_owner == worker_id,
                CreatorOutboxRow.lease_expires_at > now,
            )
            .values(
                status=OutboxStatus.PENDING.value,
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
                last_error=last_error,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def mark_dead(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        last_error: str,
    ) -> bool:
        return await self._finish(
            message_id,
            worker_id=worker_id,
            now=now,
            status=OutboxStatus.DEAD,
            last_error=last_error,
        )

    async def _finish(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        status: OutboxStatus,
        last_error: str | None,
    ) -> bool:
        result = await self._session.execute(
            update(CreatorOutboxRow)
            .where(
                CreatorOutboxRow.id == message_id,
                CreatorOutboxRow.status == OutboxStatus.PROCESSING.value,
                CreatorOutboxRow.lease_owner == worker_id,
                CreatorOutboxRow.lease_expires_at > now,
            )
            .values(
                status=status.value,
                lease_owner=None,
                lease_expires_at=None,
                last_error=last_error,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


class SqlAlchemyCreatorIdempotencyRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, scope: str, key_hash: str) -> CreatorIdempotencyRecord | None:
        row = await self._session.scalar(
            select(CreatorIdempotencyRow).where(
                CreatorIdempotencyRow.scope == scope,
                CreatorIdempotencyRow.key_hash == key_hash,
            )
        )
        if row is None:
            return None
        return CreatorIdempotencyRecord(
            id=row.id,
            scope=row.scope,
            key_hash=row.key_hash,
            request_hash=row.request_hash,
            response=row.response_json,
            task_id=row.task_id,
            created_at=_as_utc(row.created_at),
            expires_at=_as_utc(row.expires_at),
        )

    async def add(self, record: CreatorIdempotencyRecord) -> None:
        self._session.add(
            CreatorIdempotencyRow(
                id=record.id,
                scope=record.scope,
                key_hash=record.key_hash,
                request_hash=record.request_hash,
                response_json=record.response,
                task_id=record.task_id,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
        )


class SqlAlchemyCreatorDecisionRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(
        self, decision_id: str, *, for_update: bool = False
    ) -> CreatorHumanDecision | None:
        statement = select(CreatorHumanDecisionRow).where(
            CreatorHumanDecisionRow.id == decision_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return _decision_from_row(row) if row is not None else None

    async def add(self, decision: CreatorHumanDecision) -> None:
        self._session.add(_decision_to_row(decision))

    async def save(
        self,
        decision: CreatorHumanDecision,
        *,
        expected_version: int,
    ) -> None:
        result = await self._session.execute(
            update(CreatorHumanDecisionRow)
            .where(
                CreatorHumanDecisionRow.id == decision.id,
                CreatorHumanDecisionRow.version == expected_version,
            )
            .values(**_decision_values(decision))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CreatorPersistenceConflictError(
                f"Decision {decision.id} optimistic update failed",
                details={
                    "decision_id": decision.id,
                    "expected_version": expected_version,
                },
            )


class SqlAlchemyCreatorUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> "SqlAlchemyCreatorUnitOfWork":
        self._session = self._session_factory()
        self.tasks = SqlAlchemyCreatorTaskRepository(self._session)
        self.runs = SqlAlchemyCreatorRunRepository(self._session)
        self.events = SqlAlchemyCreatorEventRepository(self._session)
        self.outbox = SqlAlchemyCreatorOutboxRepository(self._session)
        self.idempotency = SqlAlchemyCreatorIdempotencyRepository(self._session)
        self.decisions = SqlAlchemyCreatorDecisionRepository(self._session)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._session is None:
            return
        if exc_type is not None or not self._committed:
            await self._session.rollback()
        await self._session.close()
        self._session = None

    async def flush(self) -> None:
        if self._session is None:
            raise RuntimeError("Unit of Work is not active")
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CreatorPersistenceConflictError(
                "Creator control-plane transaction conflicted",
                details={"constraint": _constraint_name(exc)},
            ) from exc

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("Unit of Work is not active")
        try:
            await self._session.commit()
            self._committed = True
        except IntegrityError as exc:
            await self._session.rollback()
            raise CreatorPersistenceConflictError(
                "Creator control-plane transaction conflicted",
                details={"constraint": _constraint_name(exc)},
            ) from exc


class SqlAlchemyCreatorUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    def __call__(self) -> CreatorUnitOfWork:
        return SqlAlchemyCreatorUnitOfWork(self._session_factory)


class SqlAlchemyCreatorArtifactStore:
    """Immutable data-plane store, intentionally separate from the Harness UoW."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def put(self, artifact: CreatorArtifact) -> None:
        async with self._session_factory() as session:
            existing = await session.get(CreatorArtifactRow, artifact.id)
            if existing is not None:
                _assert_artifact_row(existing, artifact)
                return
            session.add(_artifact_to_row(artifact))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                existing = await session.get(CreatorArtifactRow, artifact.id)
                if existing is not None:
                    _assert_artifact_row(existing, artifact)
                    return
                raise CreatorPersistenceConflictError(
                    "Creator Artifact write conflicted",
                    details={"constraint": _constraint_name(exc)},
                ) from exc

    async def get(self, artifact_id: str) -> CreatorArtifact | None:
        async with self._session_factory() as session:
            row = await session.get(CreatorArtifactRow, artifact_id)
            return _artifact_from_row(row) if row is not None else None

    async def get_many(
        self, artifact_ids: tuple[str, ...]
    ) -> tuple[CreatorArtifact, ...]:
        if not artifact_ids:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(CreatorArtifactRow).where(
                        CreatorArtifactRow.id.in_(artifact_ids)
                    )
                )
            ).all()
        by_id = {row.id: row for row in rows}
        return tuple(
            _artifact_from_row(by_id[artifact_id])
            for artifact_id in artifact_ids
            if artifact_id in by_id
        )

    async def list_for_run(self, run_id: str) -> tuple[CreatorArtifact, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(CreatorArtifactRow)
                    .where(CreatorArtifactRow.run_id == run_id)
                    .order_by(CreatorArtifactRow.created_at, CreatorArtifactRow.id)
                )
            ).all()
        return tuple(_artifact_from_row(row) for row in rows)


async def create_creator_schema(engine: AsyncEngine) -> None:
    """Create the Creator schema for local development and tests.

    Production deployments will use versioned migrations instead of this
    helper.
    """

    async with engine.begin() as connection:
        await connection.run_sync(CreatorBase.metadata.create_all)


def _claimable_outbox(now: datetime) -> Any:
    return or_(
        and_(
            CreatorOutboxRow.status == OutboxStatus.PENDING.value,
            CreatorOutboxRow.available_at <= now,
        ),
        and_(
            CreatorOutboxRow.status == OutboxStatus.PROCESSING.value,
            CreatorOutboxRow.lease_expires_at.is_not(None),
            CreatorOutboxRow.lease_expires_at <= now,
        ),
    )


def _outbox_from_row(row: CreatorOutboxRow) -> CreatorOutboxMessage:
    return CreatorOutboxMessage(
        id=row.id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        topic=row.topic,
        payload=dict(row.payload_json),
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        available_at=_as_utc(row.available_at),
        lease_owner=row.lease_owner,
        lease_expires_at=_as_utc(row.lease_expires_at),
        last_error=row.last_error,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _task_to_row(task: CreatorTask) -> CreatorTaskRow:
    return CreatorTaskRow(id=task.id, **_task_values(task))


def _task_values(task: CreatorTask) -> dict[str, Any]:
    return {
        "tenant_id": task.tenant_id,
        "creator_id": task.creator_id,
        "session_id": task.session_id,
        "kind": task.kind.value,
        "goal_json": task.goal.model_dump(mode="json"),
        "status": task.status.value,
        "version": task.version,
        "active_run_id": task.active_run_id,
        "final_artifact_id": task.final_artifact_id,
        "pending_decision_id": task.pending_decision_id,
        "trace_id": task.trace_id,
        "cancel_requested": task.cancel_requested,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_from_row(row: CreatorTaskRow) -> CreatorTask:
    return CreatorTask(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        session_id=row.session_id,
        kind=CreatorTaskKind(row.kind),
        goal=CreatorGoal.model_validate(row.goal_json),
        status=CreatorTaskStatus(row.status),
        version=row.version,
        active_run_id=row.active_run_id,
        final_artifact_id=row.final_artifact_id,
        pending_decision_id=row.pending_decision_id,
        trace_id=row.trace_id,
        cancel_requested=row.cancel_requested,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _run_to_row(run: CreatorRun) -> CreatorRunRow:
    return CreatorRunRow(id=run.id, **_run_values(run))


def _run_values(run: CreatorRun) -> dict[str, Any]:
    return {
        "task_id": run.task_id,
        "thread_id": run.thread_id,
        "attempt": run.attempt,
        "execution_attempts": run.execution_attempts,
        "status": run.status.value,
        "version": run.version,
        "lease_owner": run.lease_owner,
        "lease_expires_at": run.lease_expires_at,
        "checkpoint_id": run.checkpoint_id,
        "pending_decision_id": run.pending_decision_id,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "retryable": run.retryable,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _run_from_row(row: CreatorRunRow) -> CreatorRun:
    return CreatorRun(
        id=row.id,
        task_id=row.task_id,
        thread_id=row.thread_id,
        attempt=row.attempt,
        execution_attempts=row.execution_attempts,
        status=CreatorRunStatus(row.status),
        version=row.version,
        lease_owner=row.lease_owner,
        lease_expires_at=_as_utc(row.lease_expires_at),
        checkpoint_id=row.checkpoint_id,
        pending_decision_id=row.pending_decision_id,
        error_code=row.error_code,
        error_message=row.error_message,
        retryable=row.retryable,
        started_at=_as_utc(row.started_at),
        ended_at=_as_utc(row.ended_at),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _artifact_to_row(artifact: CreatorArtifact) -> CreatorArtifactRow:
    return CreatorArtifactRow(
        id=artifact.id,
        tenant_id=artifact.tenant_id,
        creator_id=artifact.creator_id,
        task_id=artifact.task_id,
        run_id=artifact.run_id,
        step_id=artifact.step_id,
        kind=artifact.kind.value,
        producer=artifact.producer,
        revision=artifact.revision,
        content_json=artifact.content,
        parent_ids_json=list(artifact.parent_ids),
        metadata_json=artifact.metadata,
        confidence=artifact.confidence,
        content_sha256=artifact.content_sha256,
        created_at=artifact.created_at,
    )


def _decision_to_row(
    decision: CreatorHumanDecision,
) -> CreatorHumanDecisionRow:
    return CreatorHumanDecisionRow(
        id=decision.id,
        **_decision_values(decision),
    )


def _decision_values(decision: CreatorHumanDecision) -> dict[str, Any]:
    return {
        "task_id": decision.task_id,
        "run_id": decision.run_id,
        "checkpoint_id": decision.checkpoint_id,
        "interrupt_id": decision.interrupt_id,
        "kind": decision.kind.value,
        "prompt": decision.prompt,
        "source_artifact_id": decision.source_artifact_id,
        "allowed_actions_json": [action.value for action in decision.allowed_actions],
        "allowed_option_ids_json": list(decision.allowed_option_ids),
        "status": decision.status.value,
        "version": decision.version,
        "submission_hash": decision.submission_hash,
        "idempotency_key_hash": decision.idempotency_key_hash,
        "action": decision.action.value if decision.action else None,
        "actor_id": decision.actor_id,
        "selected_option_id": decision.selected_option_id,
        "feedback": decision.feedback,
        "edited_payload_json": (
            dict(decision.edited_payload) if decision.edited_payload else None
        ),
        "created_at": decision.created_at,
        "submitted_at": decision.submitted_at,
        "applied_at": decision.applied_at,
    }


def _decision_from_row(
    row: CreatorHumanDecisionRow,
) -> CreatorHumanDecision:
    return CreatorHumanDecision(
        id=row.id,
        task_id=row.task_id,
        run_id=row.run_id,
        checkpoint_id=row.checkpoint_id,
        interrupt_id=row.interrupt_id,
        kind=CreatorDecisionKind(row.kind),
        prompt=row.prompt,
        source_artifact_id=row.source_artifact_id,
        allowed_actions=tuple(
            CreatorDecisionAction(action) for action in row.allowed_actions_json
        ),
        allowed_option_ids=tuple(row.allowed_option_ids_json),
        status=CreatorDecisionStatus(row.status),
        version=row.version,
        submission_hash=row.submission_hash,
        idempotency_key_hash=row.idempotency_key_hash,
        action=CreatorDecisionAction(row.action) if row.action else None,
        actor_id=row.actor_id,
        selected_option_id=row.selected_option_id,
        feedback=row.feedback,
        edited_payload=(
            dict(row.edited_payload_json) if row.edited_payload_json else None
        ),
        created_at=_as_utc(row.created_at),
        submitted_at=_as_utc(row.submitted_at),
        applied_at=_as_utc(row.applied_at),
    )


def _artifact_from_row(row: CreatorArtifactRow) -> CreatorArtifact:
    return CreatorArtifact(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        run_id=row.run_id,
        step_id=row.step_id,
        kind=ArtifactKind(row.kind),
        producer=row.producer,
        revision=row.revision,
        content=row.content_json,
        parent_ids=tuple(row.parent_ids_json),
        metadata=row.metadata_json,
        confidence=row.confidence,
        content_sha256=row.content_sha256,
        created_at=_as_utc(row.created_at),
    )


def _assert_artifact_row(
    existing: CreatorArtifactRow, incoming: CreatorArtifact
) -> None:
    stable_existing = (
        existing.tenant_id,
        existing.creator_id,
        existing.task_id,
        existing.run_id,
        existing.step_id,
        existing.kind,
        existing.producer,
        existing.revision,
        existing.content_json,
        tuple(existing.parent_ids_json),
        existing.metadata_json,
        existing.confidence,
        existing.content_sha256,
    )
    stable_incoming = (
        incoming.tenant_id,
        incoming.creator_id,
        incoming.task_id,
        incoming.run_id,
        incoming.step_id,
        incoming.kind.value,
        incoming.producer,
        incoming.revision,
        incoming.content,
        incoming.parent_ids,
        incoming.metadata,
        incoming.confidence,
        incoming.content_sha256,
    )
    if stable_existing != stable_incoming:
        raise CreatorArtifactConflictError(
            f"Artifact {incoming.id} cannot be overwritten",
            details={"artifact_id": incoming.id},
        )


@overload
def _as_utc(value: datetime) -> datetime: ...


@overload
def _as_utc(value: None) -> None: ...


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _constraint_name(exc: IntegrityError) -> str:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    return str(name or "unknown")
