"""Phase 11.6-D3 regression tests for the legacy run projection boundary."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from greenbook_assistant_api.api.routes import (
    _http_status_for_tool_error,
    _run_projection_fields,
    get_run,
)
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_core.compatibility.history import RunExecutionAdapter
from greenbook_assistant_core.execution.models import PlanExecution, StepExecution
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_contracts.identity import AuthContext


@pytest.mark.parametrize(
    "error_code",
    ["BUSINESS_REJECTED", "PARTIAL_FAILURE", "REVISION_CONFLICT", "SCHEDULE_CONFLICT"],
)
def test_revision_and_business_conflicts_map_to_http_409(error_code: str) -> None:
    assert _http_status_for_tool_error(error_code) == 409


def test_runtime_projection_does_not_copy_runtime_status_or_events() -> None:
    result = RuntimeResult(
        success=False,
        status="FAILED",
        execution_path="runtime",
        execution_id="execution-1",
        content="partial result",
        error_code="TIMEOUT",
        error_message="temporary failure",
        events=[{"event": "STEP_FAILED"}],
    )

    projection = _run_projection_fields(result, execution_id="execution-1")

    assert projection == {"content": "partial result"}


def test_legacy_projection_keeps_historical_status_and_events() -> None:
    result = RuntimeResult(
        success=False,
        status="FAILED",
        execution_path="legacy",
        error_code="BUSINESS_ERROR",
        error_message="rejected",
        events=[{"event": "RUN_FAILED"}],
    )

    projection = _run_projection_fields(result, execution_id=None)

    assert projection["status"] == "FAILED"
    assert projection["events"] == [{"event": "RUN_FAILED"}]
    assert projection["error_code"] == "BUSINESS_ERROR"


def _request(*, run_store: dict, adapter: RunExecutionAdapter,
             state_manager: ExecutionStateManager) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            run_store=run_store,
            execution_state_manager=state_manager,
            run_execution_adapter=adapter,
            execution_event_store=state_manager.event_store,
        )
    )
    request = Request({"type": "http", "app": app, "state": {}})
    request.state.auth_context = AuthContext(
        user_id="user-1",
        tenant_id="tenant-1",
        raw_access_token="test-token",
    )
    return request


@pytest.mark.asyncio
async def test_run_history_reads_mapped_status_from_execution() -> None:
    repository = ExecutionRepository()
    execution = PlanExecution(status="PENDING")
    execution.steps = [
        StepExecution(
            execution_id=execution.execution_id,
            step_id="search",
            capability="SEARCH",
            ordinal=0,
        )
    ]
    repository.save(execution)
    state = ExecutionStateManager(repository)
    state.start_execution(execution.execution_id)

    adapter = RunExecutionAdapter()
    adapter.bind_run_execution("run-1", execution.execution_id)
    request = _request(
        run_store={
            "run-1": {
                "run_id": "run-1",
                "user_id": "user-1",
                "tenant_id": "tenant-1",
                "conversation_id": "conversation-1",
                "status": "RUNTIME_BACKED",
                "events": [],
            }
        },
        adapter=adapter,
        state_manager=state,
    )

    response = await get_run("run-1", request)

    assert response.execution_id == execution.execution_id
    assert response.status == "RUNNING"
    assert response.steps[0]["status"] == "PENDING"
