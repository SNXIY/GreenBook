from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.creator.domain.models import (
    CreatorHumanDecision,
    CreatorIdempotencyRecord,
    CreatorOutboxMessage,
    CreatorRun,
    CreatorRunEvent,
    CreatorTask,
    RuntimeOutcome,
    RuntimeResumeRequest,
    RuntimeStartRequest,
)


@runtime_checkable
class CreatorRuntimePort(Protocol):
    name: str

    async def start(self, request: RuntimeStartRequest) -> RuntimeOutcome:
        """Start or recover a run using its stable internal thread ID."""

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeOutcome:
        """Resume one exact LangGraph interrupt on the stable thread."""


class CreatorTaskMemoryPort(Protocol):
    async def remember_task(self, task: CreatorTask, run: CreatorRun) -> None:
        """Project the latest durable task/run state into short memory."""


class CreatorTaskRepository(Protocol):
    async def get(
        self, task_id: str, *, for_update: bool = False
    ) -> CreatorTask | None: ...

    async def add(self, task: CreatorTask) -> None: ...

    async def save(self, task: CreatorTask, *, expected_version: int) -> None: ...


class CreatorRunRepository(Protocol):
    async def get(
        self, run_id: str, *, for_update: bool = False
    ) -> CreatorRun | None: ...

    async def add(self, run: CreatorRun) -> None: ...

    async def save(self, run: CreatorRun, *, expected_version: int) -> None: ...

    async def max_attempt(self, task_id: str) -> int: ...


class CreatorEventRepository(Protocol):
    async def next_sequence(self, run_id: str) -> int: ...

    async def add(self, event: CreatorRunEvent) -> None: ...


class CreatorOutboxRepository(Protocol):
    async def get(
        self,
        message_id: str,
        *,
        for_update: bool = False,
    ) -> CreatorOutboxMessage | None: ...

    async def add(self, message: CreatorOutboxMessage) -> None: ...

    async def claim_ready(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[CreatorOutboxMessage, ...]: ...

    async def renew_lease(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool: ...

    async def mark_completed(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
    ) -> bool: ...

    async def mark_retry(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        available_at: datetime,
        last_error: str,
    ) -> bool: ...

    async def mark_dead(
        self,
        message_id: str,
        *,
        worker_id: str,
        now: datetime,
        last_error: str,
    ) -> bool: ...


class CreatorIdempotencyRepository(Protocol):
    async def get(
        self, scope: str, key_hash: str
    ) -> CreatorIdempotencyRecord | None: ...

    async def add(self, record: CreatorIdempotencyRecord) -> None: ...


class CreatorDecisionRepository(Protocol):
    async def get(
        self, decision_id: str, *, for_update: bool = False
    ) -> CreatorHumanDecision | None: ...

    async def add(self, decision: CreatorHumanDecision) -> None: ...

    async def save(
        self, decision: CreatorHumanDecision, *, expected_version: int
    ) -> None: ...


class CreatorUnitOfWork(Protocol):
    @property
    def tasks(self) -> CreatorTaskRepository: ...

    @property
    def runs(self) -> CreatorRunRepository: ...

    @property
    def events(self) -> CreatorEventRepository: ...

    @property
    def outbox(self) -> CreatorOutboxRepository: ...

    @property
    def idempotency(self) -> CreatorIdempotencyRepository: ...

    @property
    def decisions(self) -> CreatorDecisionRepository: ...

    async def __aenter__(self) -> CreatorUnitOfWork: ...

    async def __aexit__(self, exc_type, exc, traceback) -> None: ...

    async def flush(self) -> None: ...

    async def commit(self) -> None: ...


class CreatorUnitOfWorkFactory(Protocol):
    def __call__(self) -> CreatorUnitOfWork: ...
