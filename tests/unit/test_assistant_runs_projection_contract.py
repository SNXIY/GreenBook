"""Phase 11.6-D8.3 enforcement for the assistant_runs projection boundary."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from starlette.requests import Request

from greenbook_assistant_api.api.routes import _run_projection_fields, get_run
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_core.compatibility.history import RunExecutionAdapter
from greenbook_assistant_core.db.repositories import (
    RunProjectionContractError,
    LegacyRunHistoryRepository,
    RunRepository,
    _runs,
    metadata,
)
from greenbook_assistant_core.execution.models import PlanExecution, StepExecution
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_contracts.identity import AuthContext


RUNTIME_FIELDS = {
    "status": "RUNTIME_BACKED",
    "events": [],
    "error_code": "TIMEOUT",
    "error_message": "runtime failure",
    "tool_rounds": 9,
    "partial_results": [{"x": 1}],
}


def _runtime_result() -> RuntimeResult:
    fields = dict(RUNTIME_FIELDS)
    status = fields.pop("status")
    return RuntimeResult(
        success=False,
        status=status,
        execution_path="runtime",
        execution_id="execution-1",
        content="runtime answer",
        **fields,
    )


def test_runtime_projection_is_metadata_only_even_with_fake_values() -> None:
    projection = _run_projection_fields(_runtime_result(), execution_id="execution-1")
    assert set(projection) == {"content"}
    assert not RUNTIME_FIELDS.keys() & projection.keys()


def test_run_repository_name_remains_a_compatibility_alias() -> None:
    assert RunRepository is LegacyRunHistoryRepository


def test_legacy_projection_retains_historical_fields() -> None:
    result = RuntimeResult(
        success=False,
        status="FAILED",
        execution_path="legacy",
        content="legacy answer",
        error_code="BUSINESS_ERROR",
        error_message="rejected",
        events=[{"event": "RUN_FAILED"}],
        tool_rounds=2,
    )
    projection = _run_projection_fields(result, execution_id=None)
    assert projection["status"] == "FAILED"
    assert projection["events"] == [{"event": "RUN_FAILED"}]
    assert projection["error_code"] == "BUSINESS_ERROR"
    assert projection["tool_rounds"] == 2


@pytest.mark.asyncio
async def test_repository_requires_explicit_mode_and_rejects_runtime_fields() -> None:
    repository = LegacyRunHistoryRepository(SimpleNamespace())
    with pytest.raises(RunProjectionContractError):
        await repository.create(run_id="run-1")
    with pytest.raises(RunProjectionContractError):
        await repository.update("run-1", status="COMPLETED")
    with pytest.raises(RunProjectionContractError):
        await repository.create(
            _legacy_projection=False,
            run_id="run-1",
            conversation_id="conversation-1",
            user_id="user-1",
            tenant_id="tenant-1",
            content="answer",
            status="UNKNOWN",
            events=[],
        )


def test_database_projection_allows_null_runtime_status_and_legacy_status() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    assert _runs.c.status.nullable is True
    runs = sa.Table(
        "assistant_runs", sa.MetaData(),
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text),
        sa.Column("status", sa.String(32), nullable=True),
    )
    runs.create(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(runs).values(
                run_id="11111111-1111-1111-1111-111111111111",
                conversation_id="22222222-2222-2222-2222-222222222222",
                user_id="user-1", tenant_id="tenant-1", content="runtime",
                status=None,
            )
        )
        connection.execute(
            sa.insert(runs).values(
                run_id="33333333-3333-3333-3333-333333333333",
                conversation_id="22222222-2222-2222-2222-222222222222",
                user_id="user-1", tenant_id="tenant-1", content="legacy",
                status="COMPLETED",
            )
        )


def _request(run_store: dict, adapter: RunExecutionAdapter,
             state_manager: ExecutionStateManager) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(
        run_store=run_store,
        execution_state_manager=state_manager,
        run_execution_adapter=adapter,
        execution_event_store=state_manager.event_store,
    ))
    request = Request({"type": "http", "app": app, "state": {}})
    request.state.auth_context = AuthContext(
        user_id="user-1", tenant_id="tenant-1", raw_access_token="test-token"
    )
    return request


@pytest.mark.asyncio
async def test_runtime_read_uses_execution_and_ignores_legacy_runtime_state() -> None:
    execution = PlanExecution(status="PENDING")
    execution.steps = [StepExecution(
        execution_id=execution.execution_id, step_id="step-1", capability="SEARCH", ordinal=0
    )]
    repository = ExecutionRepository()
    repository.save(execution)
    state_manager = ExecutionStateManager(repository)
    state_manager.start_execution(execution.execution_id)
    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-1", execution.execution_id)
    response = await get_run("run-1", _request({
        "run-1": {
            "run_id": "run-1", "conversation_id": "conversation-1",
            "user_id": "user-1", "tenant_id": "tenant-1", "content": "answer",
            "status": "UNKNOWN", "events": [{"event": "legacy"}],
            "error_code": "LEGACY_ERROR", "tool_rounds": 99,
        }
    }, adapter, state_manager))
    assert response.status == "RUNNING"
    assert response.steps[0]["status"] == "PENDING"
    assert response.error_code is None
    assert response.error is None
    assert response.budget["tool_calls"] == 0


@pytest.mark.asyncio
async def test_legacy_read_uses_assistant_runs_history() -> None:
    response = await get_run("legacy-1", _request({
        "legacy-1": {
            "run_id": "legacy-1", "conversation_id": "conversation-1",
            "user_id": "user-1", "tenant_id": "tenant-1", "content": "old",
            "status": "COMPLETED", "events": [{"event": "RUN_DONE"}],
            "error_code": "OLD_ERROR", "error_message": "old failure", "tool_rounds": 3,
        }
    }, RunExecutionAdapter(), ExecutionStateManager(ExecutionRepository())))
    assert response.status == "COMPLETED"
    assert response.error_code == "OLD_ERROR"
    assert response.budget["tool_calls"] == 3
