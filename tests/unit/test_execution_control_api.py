"""Phase 9.2 Execution Runtime control API tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.models import StepStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator

from apps.assistant_api.greenbook_assistant_api.api.runtime_routes import (
    cancel_execution,
    pause_execution,
    retry_execution_step,
    resume_execution,
)


class _Request:
    def __init__(self, manager: RuntimeManager, *, authenticated: bool = True) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                execution_runtime_manager=manager,
                execution_state_manager=manager._state,
                execution_authorizer=lambda _auth, _execution: True,
            )
        )
        self.state = SimpleNamespace(
            auth_context=(
                SimpleNamespace(user_id="user-1", tenant_id="tenant-1")
                if authenticated
                else None
            )
        )


@pytest.fixture(autouse=True)
def clear_store() -> None:
    ExecutionRepository.clear()


def _runtime() -> tuple[RuntimeManager, str, str]:
    registry = CapabilityRegistry()
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="control-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    state = ExecutionStateManager(ExecutionRepository())
    manager = RuntimeManager(state)
    execution = manager.create_execution(plan, executable)
    return manager, execution.execution_id, execution.steps[0].step_id


@pytest.mark.asyncio
async def test_pause_resume_cancel_control_flow() -> None:
    manager, execution_id, _ = _runtime()
    request = _Request(manager)

    manager.start_execution(execution_id)
    paused = await pause_execution(execution_id, request)
    assert paused.status == "PAUSED"

    resumed = await resume_execution(execution_id, request)
    assert resumed.status == "RUNNING"

    cancelled = await cancel_execution(execution_id, request)
    assert cancelled.status == "CANCELLED"


@pytest.mark.asyncio
async def test_illegal_pause_returns_conflict() -> None:
    manager, execution_id, _ = _runtime()
    request = _Request(manager)

    with pytest.raises(HTTPException) as error:
        await pause_execution(execution_id, request)

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_retryable_failed_step_is_reset_to_pending() -> None:
    manager, execution_id, step_id = _runtime()
    request = _Request(manager)
    manager.start_execution(execution_id)
    step = manager.list_steps(execution_id)[0]
    manager._state.start_step(execution_id, step.step_execution_id)
    manager._state.fail_step(
        execution_id,
        step.step_execution_id,
        error_code="TIMEOUT",
        error_message="temporary timeout",
    )

    response = await retry_execution_step(execution_id, step_id, request)

    assert response.status == StepStatus.PENDING.value
    assert response.retry_count == 1


@pytest.mark.asyncio
async def test_control_requires_authentication() -> None:
    manager, execution_id, _ = _runtime()
    manager.start_execution(execution_id)

    with pytest.raises(HTTPException) as error:
        await pause_execution(execution_id, _Request(manager, authenticated=False))

    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_authorizer_can_deny_execution() -> None:
    manager, execution_id, _ = _runtime()
    manager.start_execution(execution_id)
    request = _Request(manager)
    request.app.state.execution_authorizer = lambda _auth, _execution: False

    with pytest.raises(HTTPException) as error:
        await pause_execution(execution_id, request)

    assert error.value.status_code == 403

