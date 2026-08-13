"""Phase 9.2 Execution Runtime control API tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from datetime import UTC, datetime

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueStatus,
)
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.models import StepStatus
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from tests.plan_factory import GoalPlanFactory
from greenbook_agent_core.planning.validation import PlanValidator
from greenbook_contracts import SideEffectState

from apps.agent_api.greenbook_agent_api.api.runtime_routes import (
    cancel_execution,
    get_execution_control,
    pause_execution,
    retry_execution_step,
    resume_execution,
)


class _Request:
    def __init__(
        self,
        manager: RuntimeManager,
        *,
        authenticated: bool = True,
        queue=None,
    ) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                execution_runtime_manager=manager,
                execution_state_manager=manager._state,
                execution_authorizer=lambda _auth, _execution: True,
                execution_queue=queue,
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
    plan = GoalPlanFactory(registry).generate_plan(
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
    assert paused.status == "RUNNING"
    assert paused.control_state == "PAUSING"

    manager.save_checkpoint(execution_id)
    manager._state.confirm_pause(execution_id)

    resumed = await resume_execution(execution_id, request)
    assert resumed.status == "RUNNING"
    assert resumed.control_state == "RUNNING"

    cancelled = await cancel_execution(execution_id, request)
    assert cancelled.status == "CANCELLED"


@pytest.mark.asyncio
async def test_illegal_pause_returns_conflict() -> None:
    manager, execution_id, _ = _runtime()
    request = _Request(manager)
    manager.cancel_execution(execution_id)

    with pytest.raises(HTTPException) as error:
        await pause_execution(execution_id, request)

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_resume_requeues_even_when_previous_worker_is_finishing_claim() -> None:
    manager, execution_id, _ = _runtime()
    queue = ExecutionQueue()
    message = queue.enqueue(execution_id, payload={"dispatch": "snapshot"})
    queue.claim(datetime.now(UTC), worker_id="old-worker", lease_seconds=60, limit=1)
    request = _Request(manager, queue=queue)
    manager.start_execution(execution_id)
    await pause_execution(execution_id, request)
    manager._state.confirm_pause(execution_id)

    response = await resume_execution(execution_id, request)
    control = await get_execution_control(execution_id, request)

    assert response.control_state == "RESUMING"
    assert control.state == "RESUMING"
    requeued = queue.get(message.message_id)
    assert requeued is not None
    assert requeued.status == ExecutionQueueStatus.READY


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
    manager.event_store.append(
        ExecutionEvent(
            execution_id=execution_id,
            event_type=EventType.STEP_FAILED,
            step_id=step_id,
            payload={
                "step_execution_id": step.step_execution_id,
                "error_code": "TIMEOUT",
                "error_message": "temporary timeout",
                "retryable": True,
                "evidence": ExecutionEvidence(
                    request_sent=False,
                    side_effect_state=SideEffectState.NONE,
                ).model_dump(mode="json"),
            },
        )
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

