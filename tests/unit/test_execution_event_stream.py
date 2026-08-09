"""Phase 6.10-C execution event stream tests."""

from __future__ import annotations

from typing import Any

import pytest

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.capability_executor import CapabilityExecutor
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.events import EventType
from greenbook_assistant_core.execution.invocation import ExecutionResult
from greenbook_assistant_core.execution.models import ExecutionStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


@pytest.fixture(autouse=True)
def clear_stores() -> None:
    ExecutionRepository.clear()


def _runtime() -> tuple[RuntimeManager, str]:
    registry = CapabilityRegistry()
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="event-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    manager = RuntimeManager(ExecutionStateManager(ExecutionRepository()))
    return manager, manager.create_execution(plan, executable).execution_id


def test_execution_lifecycle_events() -> None:
    manager, execution_id = _runtime()

    assert [event.event_type for event in manager.list_events(execution_id)] == [
        EventType.EXECUTION_CREATED,
    ]
    manager.start_execution(execution_id)
    manager.pause_execution(execution_id)
    manager.resume_execution(execution_id)
    manager.cancel_execution(execution_id)

    assert [event.event_type for event in manager.list_events(execution_id)] == [
        EventType.EXECUTION_CREATED,
        EventType.EXECUTION_STARTED,
        EventType.EXECUTION_PAUSED,
        EventType.EXECUTION_RESUMED,
        EventType.EXECUTION_CANCELLED,
    ]


@pytest.mark.asyncio
async def test_worker_emits_step_started_and_completed() -> None:
    registry = CapabilityRegistry()

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "code": "", "data": {"draft_id": "d1"}}

    worker = ExecutionWorker(CapabilityExecutor(registry, handler))
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="event-worker-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    execution = worker.init_from_plan(executable, task_id=plan.task_id)

    assert await worker.run(execution.execution_id) == RunOutcome.COMPLETED
    events = worker._state.event_store.list_events(execution.execution_id)
    types = [event.event_type for event in events]
    assert types == [
        EventType.EXECUTION_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
        EventType.EXECUTION_COMPLETED,
    ]
    assert events[1].step_id == execution.steps[0].step_id


def test_event_store_query_and_clear() -> None:
    store = ExecutionEventStore()
    manager, execution_id = _runtime()
    event = manager.list_events(execution_id)[0]
    store.append(event)

    assert store.list_events(execution_id)[0].event_id == event.event_id
    store.clear(execution_id)
    assert store.list_events(execution_id) == []


def test_event_store_does_not_change_execution_source_of_truth() -> None:
    manager, execution_id = _runtime()
    assert manager.get_execution(execution_id).status == ExecutionStatus.PENDING
    manager.start_execution(execution_id)
    assert manager.get_execution(execution_id).status == ExecutionStatus.RUNNING
