"""Phase 11-E Runtime persistence factory tests."""

from __future__ import annotations

import sqlalchemy as sa
import pytest

from greenbook_assistant_core.execution.operation_tracking import (
    ExternalOperationRecord,
    OperationStatus,
)
from greenbook_assistant_core.execution.execution_queue import (
    ExecutionQueue,
    PostgresExecutionQueue,
)
from greenbook_assistant_core.execution.persistence_provider import (
    MemoryCheckpointStore,
    RuntimePersistenceFactory,
)
from greenbook_assistant_core.execution.persistent_stores import (
    PostgresExternalOperationStore,
)
from greenbook_assistant_core.execution.retry_task import RetryTask
from greenbook_assistant_core.execution.retry_task_store import PostgresRetryTaskStore


def test_memory_profile_builds_all_runtime_dependencies() -> None:
    persistence = RuntimePersistenceFactory.from_env(storage="memory")

    assert persistence.storage == "memory"
    assert isinstance(persistence.checkpoint_store, MemoryCheckpointStore)
    assert persistence.execution_repository.__class__.__name__ == "ExecutionRepository"
    assert persistence.external_operation_store.__class__.__name__ == "ExternalOperationStore"
    assert persistence.retry_task_store.__class__.__name__ == "RetryTaskStore"
    assert isinstance(persistence.execution_queue, ExecutionQueue)
    persistence.close()


def test_postgres_profile_uses_one_bind_for_all_runtime_stores() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    persistence = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
    )
    record = ExternalOperationRecord(
        operation_id="provider-operation",
        execution_id="provider-execution",
        step_id="step-1",
        status=OperationStatus.UNKNOWN,
    )
    task = RetryTask(
        execution_id="provider-execution",
        step_id="step-1",
        attempt=1,
        next_retry_time="2026-08-10T12:00:00+00:00",
        reason="provider retry",
    )
    persistence.external_operation_store.create(record)
    persistence.retry_task_store.create(task)

    restarted = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
    )
    assert isinstance(restarted.external_operation_store, PostgresExternalOperationStore)
    assert isinstance(restarted.retry_task_store, PostgresRetryTaskStore)
    assert isinstance(restarted.execution_queue, PostgresExecutionQueue)
    assert restarted.external_operation_store.get(record.operation_id) is not None
    assert restarted.retry_task_store.get(task.task_id) is not None
    persistence.close()
    restarted.close()


def test_postgres_profile_requires_explicit_database_configuration(monkeypatch) -> None:
    monkeypatch.delenv("ASSISTANT_RUNTIME_DATABASE_URL", raising=False)
    monkeypatch.delenv("ASSISTANT_DATABASE_URL", raising=False)
    monkeypatch.delenv("ASSISTANT_DB_URL", raising=False)
    monkeypatch.delenv("GREENBOOK_DB_URL", raising=False)

    with pytest.raises(RuntimeError, match="requires"):
        RuntimePersistenceFactory.from_env(storage="postgres")


def test_database_url_alias_selects_durable_profile_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ASSISTANT_RUNTIME_STORAGE", raising=False)
    monkeypatch.delenv("ASSISTANT_RUNTIME_DATABASE_URL", raising=False)
    monkeypatch.setenv("ASSISTANT_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    persistence = RuntimePersistenceFactory.from_env()

    assert persistence.storage == "postgres"
    assert isinstance(persistence.execution_queue, PostgresExecutionQueue)
    persistence.close()


def test_explicit_memory_profile_wins_over_database_url(monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_DATABASE_URL", "postgresql://ignored")

    persistence = RuntimePersistenceFactory.from_env(storage="memory")

    assert persistence.storage == "memory"
    persistence.close()


def test_async_database_url_is_mapped_to_sync_adapter_driver() -> None:
    assert (
        RuntimePersistenceFactory._sync_database_url(
            "postgresql+asyncpg://user:pass@localhost/db"
        )
        == "postgresql+psycopg://user:pass@localhost/db"
    )
