"""Configuration-driven construction of the Runtime persistence aggregate."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from .checkpoint import ExecutionCheckpoint
from .event_store import ExecutionEventStore
from .execution_queue import ExecutionQueue, ExecutionQueueProtocol, PostgresExecutionQueue
from .lease import ExecutionLeaseManager, PostgresExecutionLeaseManager
from .operation_tracking import ExternalOperationStore, ExternalOperationStoreProtocol
from .persistent_stores import (
    PostgresCheckpointStore,
    PostgresExecutionEventStore,
    PostgresExternalOperationStore,
)
from .postgres_repository import PostgresExecutionRepository
from .repository import ExecutionRepository
from .retry_task_store import (
    PostgresRetryTaskStore,
    RetryTaskStore,
    RetryTaskStoreProtocol,
)


class MemoryCheckpointStore:
    """Small explicit checkpoint store used by the memory Runtime profile."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, ExecutionCheckpoint] = {}

    def save(self, checkpoint: ExecutionCheckpoint) -> ExecutionCheckpoint:
        self._checkpoints[checkpoint.execution_id] = checkpoint.model_copy(deep=True)
        return checkpoint.model_copy(deep=True)

    def latest(self, execution_id: str) -> ExecutionCheckpoint | None:
        checkpoint = self._checkpoints.get(execution_id)
        return checkpoint.model_copy(deep=True) if checkpoint is not None else None

    def clear(self, execution_id: str) -> None:
        self._checkpoints.pop(execution_id, None)


@dataclass
class RuntimePersistence:
    """All persistence dependencies shared by one Runtime process."""

    storage: str
    execution_repository: Any
    execution_event_store: Any
    checkpoint_store: Any
    external_operation_store: ExternalOperationStoreProtocol
    retry_task_store: RetryTaskStoreProtocol
    execution_queue: ExecutionQueueProtocol
    lease_manager: Any
    bind: Any | None = None
    owns_bind: bool = False

    def close(self) -> None:
        """Dispose a database bind owned by this provider, if any."""

        if self.owns_bind and self.bind is not None:
            dispose = getattr(self.bind, "dispose", None)
            if callable(dispose):
                dispose()


class RuntimePersistenceFactory:
    """Build a complete memory or PostgreSQL Runtime persistence profile."""

    MEMORY = "memory"
    POSTGRES = "postgres"

    @classmethod
    def from_env(
        cls,
        *,
        storage: str | None = None,
        database_url: str | None = None,
        bind: Any | None = None,
        create_tables: bool = True,
    ) -> RuntimePersistence:
        configured_storage = storage or os.getenv("ASSISTANT_RUNTIME_STORAGE")
        if configured_storage:
            selected = configured_storage.strip().lower()
        else:
            # The repository's deployment configuration historically exposed
            # ``ASSISTANT_DATABASE_URL``.  Treat a configured database as the
            # durable default, while keeping an explicitly selected memory
            # profile available for tests and local development.
            selected = cls.POSTGRES if cls._database_url() else cls.MEMORY
        if selected in {"postgresql", "postgresql+psycopg", "pg"}:
            selected = cls.POSTGRES
        if selected == cls.MEMORY:
            return cls._memory()
        if selected != cls.POSTGRES:
            raise ValueError(
                "ASSISTANT_RUNTIME_STORAGE must be 'memory' or 'postgres'"
            )

        runtime_url = database_url or cls._database_url()
        if bind is None and not runtime_url:
            raise RuntimeError(
                "ASSISTANT_RUNTIME_STORAGE=postgres requires "
                "ASSISTANT_RUNTIME_DATABASE_URL or ASSISTANT_DB_URL"
            )
        owns_bind = bind is None
        engine = bind or sa.create_engine(
            cls._sync_database_url(runtime_url),
            pool_pre_ping=True,
        )
        return cls._postgres(engine, create_tables=create_tables, owns_bind=owns_bind)

    @classmethod
    def _memory(cls) -> RuntimePersistence:
        return RuntimePersistence(
            storage=cls.MEMORY,
            execution_repository=ExecutionRepository(),
            execution_event_store=ExecutionEventStore(),
            checkpoint_store=MemoryCheckpointStore(),
            external_operation_store=ExternalOperationStore(),
            retry_task_store=RetryTaskStore(),
            execution_queue=ExecutionQueue(),
            lease_manager=ExecutionLeaseManager(),
        )

    @classmethod
    def _postgres(
        cls,
        bind: Any,
        *,
        create_tables: bool,
        owns_bind: bool,
    ) -> RuntimePersistence:
        return RuntimePersistence(
            storage=cls.POSTGRES,
            execution_repository=PostgresExecutionRepository(
                bind,
                create_tables=create_tables,
            ),
            execution_event_store=PostgresExecutionEventStore(
                bind,
                create_tables=create_tables,
            ),
            checkpoint_store=PostgresCheckpointStore(
                bind,
                create_tables=create_tables,
            ),
            external_operation_store=PostgresExternalOperationStore(
                bind,
                create_tables=create_tables,
            ),
            retry_task_store=PostgresRetryTaskStore(
                bind,
                create_tables=create_tables,
            ),
            execution_queue=PostgresExecutionQueue(
                bind,
                create_tables=create_tables,
            ),
            lease_manager=PostgresExecutionLeaseManager(
                bind,
                create_tables=create_tables,
            ),
            bind=bind,
            owns_bind=owns_bind,
        )

    @staticmethod
    def _database_url() -> str:
        for name in (
            "ASSISTANT_RUNTIME_DATABASE_URL",
            "ASSISTANT_DATABASE_URL",
            "ASSISTANT_DB_URL",
            "GREENBOOK_DB_URL",
        ):
            value = os.getenv(name)
            if value:
                return value
        return ""

    @staticmethod
    def _sync_database_url(url: str) -> str:
        """Use a synchronous driver for the existing SQLAlchemy adapters."""

        if url.startswith("postgresql+asyncpg://"):
            return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url.removeprefix("postgres://")
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url


__all__ = [
    "MemoryCheckpointStore",
    "RuntimePersistence",
    "RuntimePersistenceFactory",
]
