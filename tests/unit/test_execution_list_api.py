"""Tests for the Runtime-native execution list API."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from greenbook_assistant_api.api.runtime_routes import list_executions
from greenbook_assistant_core.execution.models import PlanExecution, StepExecution
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_contracts.identity import AuthContext


def _request(state_manager: ExecutionStateManager, authorizer=None) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            execution_state_manager=state_manager,
            execution_authorizer=authorizer,
        )
    )
    request = Request({"type": "http", "app": app, "state": {}})
    request.state.auth_context = AuthContext(
        user_id="user-1",
        tenant_id="tenant-1",
        raw_access_token="test-token",
    )
    return request


def _execution(execution_id: str, updated_at: str, task_id: str) -> PlanExecution:
    execution = PlanExecution(
        execution_id=execution_id,
        task_id=task_id,
        plan_id=f"plan-{execution_id}",
        status="RUNNING",
        updated_at=updated_at,
    )
    execution.steps = [
        StepExecution(
            execution_id=execution_id,
            step_id="search",
            capability="SEARCH",
            ordinal=0,
        )
    ]
    return execution


@pytest.mark.asyncio
async def test_execution_list_response_uses_runtime_metadata() -> None:
    repository = ExecutionRepository()
    repository.clear()
    repository.save(_execution("execution-1", "2026-08-09T00:01:00Z", "task-1"))
    state = ExecutionStateManager(repository)

    response = await list_executions(
        _request(state, lambda auth, execution: auth.user_id == "user-1")
    )

    assert [item.execution_id for item in response.items] == ["execution-1"]
    assert response.items[0].task_id == "task-1"
    assert response.items[0].plan_id == "plan-execution-1"
    assert response.items[0].status == "RUNNING"
    assert response.items[0].current_step == "search"
    assert response.items[0].progress == 0.0
    assert set(response.model_dump()) == {"items", "next_cursor"}
    assert "events" not in response.model_dump()


@pytest.mark.asyncio
async def test_execution_list_requires_authorization_policy() -> None:
    repository = ExecutionRepository()
    repository.clear()
    repository.save(_execution("execution-private", "2026-08-09T00:01:00Z", "task-private"))
    state = ExecutionStateManager(repository)

    with pytest.raises(Exception) as raised:
        await list_executions(_request(state))

    assert getattr(raised.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_execution_list_filters_unauthorized_executions() -> None:
    repository = ExecutionRepository()
    repository.clear()
    repository.save(_execution("execution-allowed", "2026-08-09T00:02:00Z", "task-a"))
    repository.save(_execution("execution-hidden", "2026-08-09T00:01:00Z", "task-b"))
    state = ExecutionStateManager(repository)

    response = await list_executions(
        _request(state, lambda auth, execution: execution.execution_id == "execution-allowed")
    )

    assert [item.execution_id for item in response.items] == ["execution-allowed"]


@pytest.mark.asyncio
async def test_execution_list_cursor_paginates_runtime_executions() -> None:
    repository = ExecutionRepository()
    repository.clear()
    for index in range(3):
        repository.save(
            _execution(
                f"execution-{index}",
                f"2026-08-09T00:0{index + 1}:00Z",
                f"task-{index}",
            )
        )
    state = ExecutionStateManager(repository)
    request = _request(state, lambda auth, execution: True)

    first = await list_executions(request, limit=2)
    assert [item.execution_id for item in first.items] == ["execution-2", "execution-1"]
    assert first.next_cursor is not None

    second = await list_executions(request, limit=2, cursor=first.next_cursor)
    assert [item.execution_id for item in second.items] == ["execution-0"]
    assert second.next_cursor is None
