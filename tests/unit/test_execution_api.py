"""Phase 6.10-F Execution Runtime API and SSE tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.planning.validation import PlanValidator

from apps.agent_api.greenbook_agent_api.api.runtime_routes import (
    get_execution_events,
    get_execution_status,
    get_execution_steps,
    stream_execution_events,
)
from tests.plan_factory import GoalPlanFactory


class _Request:
    def __init__(self, manager: RuntimeManager) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                execution_runtime_manager=manager,
                execution_authorizer=lambda _auth, _execution: True,
            )
        )
        self.state = SimpleNamespace(
            auth_context=SimpleNamespace(user_id="user-1", tenant_id="tenant-1")
        )

    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def clear_store() -> None:
    ExecutionRepository.clear()


def _runtime() -> tuple[RuntimeManager, str]:
    registry = CapabilityRegistry()
    plan = GoalPlanFactory(registry).generate_plan(
        task_id="api-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    state = ExecutionStateManager(ExecutionRepository())
    manager = RuntimeManager(state)
    execution = manager.create_execution(plan, executable)
    return manager, execution.execution_id


@pytest.mark.asyncio
async def test_query_execution_status() -> None:
    manager, execution_id = _runtime()
    manager.start_execution(execution_id)
    response = await get_execution_status(execution_id, _Request(manager))

    assert response.execution_id == execution_id
    assert response.status == "RUNNING"
    assert response.current_step
    assert response.progress == 0.0
    assert response.total_steps == len(manager.list_steps(execution_id))
    assert response.completed_steps == 0
    assert response.created_at
    assert response.updated_at


@pytest.mark.asyncio
async def test_query_steps_and_events() -> None:
    manager, execution_id = _runtime()
    manager.start_execution(execution_id)
    step = manager.list_steps(execution_id)[0]
    manager.event_store.append(ExecutionEvent(
        execution_id=execution_id,
        event_type=EventType.STEP_STARTED,
        step_id=step.step_id,
    ))

    steps = await get_execution_steps(execution_id, _Request(manager))
    events = await get_execution_events(execution_id, _Request(manager))
    assert steps.execution_id == execution_id
    assert steps.steps[0].step_id == step.step_id
    assert steps.steps[0].status == "PENDING"
    assert events.execution_id == execution_id
    assert events.events[-1].event_type == EventType.STEP_STARTED


@pytest.mark.asyncio
async def test_query_missing_execution_returns_not_found() -> None:
    from fastapi import HTTPException

    manager, _ = _runtime()

    with pytest.raises(HTTPException) as error:
        await get_execution_steps("missing-execution", _Request(manager))
    assert error.value.status_code == 404

    with pytest.raises(HTTPException) as error:
        await get_execution_events("missing-execution", _Request(manager))
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_sse_returns_events_and_closes_after_execution_complete() -> None:
    manager, execution_id = _runtime()
    manager.start_execution(execution_id)
    step = manager.list_steps(execution_id)[0]
    manager.event_store.append(ExecutionEvent(
        execution_id=execution_id,
        event_type=EventType.STEP_STARTED,
        step_id=step.step_id,
        payload={"step_execution_id": step.step_execution_id},
    ))
    manager._state.start_step(execution_id, step.step_execution_id)
    manager.event_store.append(ExecutionEvent(
        execution_id=execution_id,
        event_type=EventType.STEP_COMPLETED,
        step_id=step.step_id,
    ))
    manager._state.complete_step(execution_id, step.step_execution_id)

    response = await stream_execution_events(execution_id, _Request(manager))
    chunks = [chunk async for chunk in response.body_iterator]
    payload = "".join(chunks)

    assert response.media_type == "text/event-stream"
    assert "event: EXECUTION_CREATED" in payload
    assert "event: STEP_STARTED" in payload
    assert "event: STEP_COMPLETED" in payload
    assert "event: EXECUTION_COMPLETED" in payload
    assert payload.endswith("\n\n")
