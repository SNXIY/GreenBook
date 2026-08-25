"""Tests for the run_id -> execution_id compatibility boundary."""

import pytest
import sqlalchemy as sa
from greenbook_agent_core.compatibility.history import (
    DuplicateRunExecutionBindingError,
    ExecutionReference,
    RunExecutionAdapter,
    RunExecutionLinkSource,
    SqlAlchemyRunExecutionLinkRepository,
)


def test_runtime_execution_can_be_bound_to_a_legacy_run() -> None:
    adapter = RunExecutionAdapter()

    link = adapter.bind_run_execution(
        "run-1",
        "execution-1",
        conversation_id="conversation-1",
        task_id="task-1",
    )

    assert link.execution_id == "execution-1"
    assert link.mapping_source == RunExecutionLinkSource.CREATED
    assert adapter.resolve_execution("run-1") == "execution-1"


def test_run_id_resolves_to_execution_id() -> None:
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-2", "execution-2")

    assert adapter.resolve_execution("run-2") == "execution-2"


def test_execution_id_resolves_to_run_id() -> None:
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-3", "execution-3")

    assert adapter.resolve_run("execution-3") == "run-3"


def test_legacy_only_link_has_no_execution_id() -> None:
    adapter = RunExecutionAdapter()
    link = adapter.register_legacy_only("legacy-run", task_id="legacy-task")

    assert link.mapping_source == RunExecutionLinkSource.LEGACY_ONLY
    assert link.execution_id is None
    assert adapter.resolve_execution("legacy-run") is None
    assert adapter.resolve_run("missing-execution") is None


def test_duplicate_binding_is_idempotent_for_same_pair() -> None:
    adapter = RunExecutionAdapter()
    first = adapter.bind_run_execution("run-4", "execution-4")
    second = adapter.bind_run_execution("run-4", "execution-4")

    assert second == first


def test_duplicate_binding_is_rejected_for_different_pair() -> None:
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-5", "execution-5")

    with pytest.raises(DuplicateRunExecutionBindingError):
        adapter.bind_run_execution("run-5", "execution-other")
    with pytest.raises(DuplicateRunExecutionBindingError):
        adapter.bind_run_execution("run-other", "execution-5")


def test_unmapped_runtime_id_does_not_change_runtime_api_semantics() -> None:
    adapter = RunExecutionAdapter()

    assert adapter.resolve_execution("unmapped-run") is None
    assert adapter.resolve_run("unmapped-execution") is None


def test_persistent_repository_saves_and_reads_link() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    repository = SqlAlchemyRunExecutionLinkRepository(engine)
    adapter = RunExecutionAdapter(repository)
    adapter.bind_run_execution("run-persist", "execution-persist")

    reloaded = RunExecutionAdapter(SqlAlchemyRunExecutionLinkRepository(engine))
    assert reloaded.resolve_execution("run-persist") == "execution-persist"
    assert reloaded.resolve_run("execution-persist") == "run-persist"


def test_repository_exists_supports_both_identifiers() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    repository = SqlAlchemyRunExecutionLinkRepository(engine)
    adapter = RunExecutionAdapter(repository)
    adapter.bind_run_execution("run-exists", "execution-exists")

    assert repository.exists(run_id="run-exists") is True
    assert repository.exists(execution_id="execution-exists") is True


def test_persistent_legacy_only_survives_repository_reload() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    repository = SqlAlchemyRunExecutionLinkRepository(engine)
    RunExecutionAdapter(repository).register_legacy_only("legacy-persist")

    reloaded = RunExecutionAdapter(SqlAlchemyRunExecutionLinkRepository(engine))
    link = reloaded.resolve_link_by_run("legacy-persist")
    assert link is not None
    assert link.mapping_source == RunExecutionLinkSource.LEGACY_ONLY
    assert reloaded.resolve_execution("legacy-persist") is None


def test_legacy_response_reference() -> None:
    reference = RunExecutionAdapter().to_execution_reference(run_id="legacy-run")

    assert reference == ExecutionReference(
        run_id="legacy-run",
        execution_id=None,
        task_id=None,
        source="LEGACY_ONLY",
    )


def test_runtime_response_reference() -> None:
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-runtime", "execution-runtime", task_id="task-1")

    reference = adapter.to_execution_reference(run_id="run-runtime")

    assert reference.run_id == "run-runtime"
    assert reference.execution_id == "execution-runtime"
    assert reference.task_id == "task-1"
    assert reference.source == "RUNTIME"


def test_execution_only_response_reference() -> None:
    reference = RunExecutionAdapter().to_execution_reference(
        execution_id="execution-only",
        task_id="task-only",
    )

    assert reference.source == "EXECUTION_ONLY"
    assert reference.run_id is None
    assert reference.execution_id == "execution-only"


def test_missing_mapping_remains_legacy_only() -> None:
    reference = RunExecutionAdapter().to_execution_reference(run_id="missing-run")

    assert reference.source == "LEGACY_ONLY"
    assert reference.execution_id is None


def test_reference_serialization() -> None:
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-json", "execution-json")
    reference = adapter.to_execution_reference(run_id="run-json")

    assert reference.model_dump() == {
        "run_id": "run-json",
        "execution_id": "execution-json",
        "task_id": None,
        "source": "RUNTIME",
    }
