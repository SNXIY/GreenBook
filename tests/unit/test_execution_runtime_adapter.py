"""Phase 6.10-A RuntimeManager and RuntimeGuard tests."""

from __future__ import annotations

import pytest

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.checkpoint import ExecutionCheckpoint
from greenbook_assistant_core.execution.models import ExecutionStatus, StepStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_guard import (
    ExecutionBlockedError,
    RuntimeGuard,
)
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


@pytest.fixture(autouse=True)
def clear_execution_store() -> None:
    ExecutionRepository.clear()


def _runtime() -> tuple[RuntimeManager, str]:
    registry = CapabilityRegistry()
    orchestrator = TaskOrchestrator(registry)
    plan = orchestrator.generate_plan(
        task_id="task-runtime",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    manager = RuntimeManager(ExecutionStateManager(ExecutionRepository()))
    execution = manager.create_execution(plan, executable)
    return manager, execution.execution_id


def _full_runtime() -> tuple[RuntimeManager, str]:
    registry = CapabilityRegistry()
    orchestrator = TaskOrchestrator(registry)
    plan = orchestrator.generate_plan(
        task_id="task-runtime-full",
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    executable = PlanValidator(registry).validate(plan)
    manager = RuntimeManager(ExecutionStateManager(ExecutionRepository()))
    execution = manager.create_execution(plan, executable)
    return manager, execution.execution_id


def test_create_and_query_execution() -> None:
    manager, execution_id = _runtime()

    execution = manager.get_execution(execution_id)
    assert execution.status == ExecutionStatus.PENDING
    assert manager.list_steps(execution_id)[0].status == StepStatus.PENDING


def test_pause_resume_and_cancel() -> None:
    manager, execution_id = _runtime()
    manager.start_execution(execution_id)

    assert manager.pause_execution(execution_id).status == ExecutionStatus.PAUSED
    assert manager.resume_execution(execution_id).status == ExecutionStatus.RUNNING
    assert manager.cancel_execution(execution_id).status == ExecutionStatus.CANCELLED


def test_guard_blocks_non_running_states() -> None:
    manager, execution_id = _runtime()
    guard = RuntimeGuard(manager)
    assert guard.can_execute(execution_id) is False

    with pytest.raises(ExecutionBlockedError):
        guard.check_execution(execution_id)

    manager.start_execution(execution_id)
    assert guard.can_execute(execution_id) is True
    manager.pause_execution(execution_id)
    with pytest.raises(ExecutionBlockedError):
        guard.check_execution(execution_id)


def test_checkpoint_save_and_restore_uses_plan_execution_state() -> None:
    manager, execution_id = _full_runtime()
    manager.start_execution(execution_id)
    steps = manager.list_steps(execution_id)
    for step in steps[:2]:
        manager._state.start_step(execution_id, step.step_execution_id)
        manager._state.complete_step(execution_id, step.step_execution_id)

    checkpoint = manager.save_checkpoint(execution_id, {"draft_id": "draft-1"})
    assert isinstance(checkpoint, ExecutionCheckpoint)
    assert checkpoint.completed_steps == [steps[0].step_id, steps[1].step_id]
    assert checkpoint.current_step == steps[2].step_id
    assert checkpoint.snapshot == {"draft_id": "draft-1"}

    restored = manager.restore_checkpoint(execution_id)
    assert restored == checkpoint
    restored_execution = manager.get_execution(execution_id)
    assert [step.status for step in restored_execution.steps[:2]] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert restored_execution.steps[2].status == StepStatus.PENDING
