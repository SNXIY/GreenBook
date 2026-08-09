"""Phase 11.5-B API-boundary link completeness tests."""

import pytest

from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.runtime_linking import bind_runtime_result
from greenbook_assistant_core.compatibility.history import (
    DuplicateRunExecutionBindingError,
    RunExecutionAdapter,
)


def test_runtime_result_execution_id_creates_link() -> None:
    adapter = RunExecutionAdapter()
    result = RuntimeResult(
        status="COMPLETED",
        execution_path="runtime",
        execution_id="execution-1",
    )

    linked = bind_runtime_result(
        adapter,
        run_id="run-1",
        result=result,
        ctx=RuntimeContext(run_id="run-1", task_id="task-1"),
        conversation_id="conversation-1",
    )

    assert linked == "execution-1"
    assert adapter.resolve_execution("run-1") == "execution-1"


def test_runtime_context_id_is_compatibility_fallback() -> None:
    adapter = RunExecutionAdapter()
    result = RuntimeResult(status="COMPLETED", execution_path="runtime")

    bind_runtime_result(
        adapter,
        run_id="run-ctx",
        result=result,
        ctx=RuntimeContext(
            run_id="run-ctx",
            task_id="task-ctx",
            execution_id="execution-ctx",
        ),
        conversation_id="conversation-ctx",
    )

    assert adapter.resolve_execution("run-ctx") == "execution-ctx"


def test_legacy_result_does_not_create_execution_link() -> None:
    adapter = RunExecutionAdapter()
    result = RuntimeResult(status="COMPLETED", execution_path="legacy")

    linked = bind_runtime_result(
        adapter,
        run_id="legacy-run",
        result=result,
        ctx=RuntimeContext(run_id="legacy-run"),
        conversation_id="conversation-legacy",
    )

    assert linked is None
    assert adapter.resolve_execution("legacy-run") is None


def test_duplicate_runtime_binding_remains_protected() -> None:
    adapter = RunExecutionAdapter()
    first = RuntimeResult(
        status="COMPLETED", execution_path="runtime", execution_id="execution-a"
    )
    second = RuntimeResult(
        status="COMPLETED", execution_path="runtime", execution_id="execution-b"
    )
    ctx = RuntimeContext(run_id="run-duplicate")

    bind_runtime_result(
        adapter,
        run_id="run-duplicate",
        result=first,
        ctx=ctx,
        conversation_id="conversation-1",
    )
    with pytest.raises(DuplicateRunExecutionBindingError):
        bind_runtime_result(
            adapter,
            run_id="run-duplicate",
            result=second,
            ctx=ctx,
            conversation_id="conversation-1",
        )
