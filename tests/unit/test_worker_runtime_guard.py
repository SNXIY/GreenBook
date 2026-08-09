"""Phase 6.10-B Worker RuntimeGuard hook tests."""

from __future__ import annotations

import pytest

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.invocation import ExecutionResult
from greenbook_assistant_core.execution.models import ExecutionStatus, StepStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, step):
        self.calls += 1
        return ExecutionResult.success(
            capability=step.capability,
            tool_name="fake.tool",
            tool_result={"ok": True},
        )


@pytest.fixture(autouse=True)
def clear_execution_store() -> None:
    ExecutionRepository.clear()


def _worker() -> tuple[ExecutionWorker, ExecutionStateManager, str, _Executor]:
    registry = CapabilityRegistry()
    plan = TaskOrchestrator(registry).generate_plan(
        task_id="guard-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    executor = _Executor()
    repository = ExecutionRepository()
    worker = ExecutionWorker(executor, repository=repository)
    execution = worker.init_from_plan(executable, task_id="guard-task")
    return worker, ExecutionStateManager(repository), execution.execution_id, executor


@pytest.mark.asyncio
async def test_running_execution_executes_step() -> None:
    worker, _, execution_id, executor = _worker()

    outcome = await worker.run(execution_id)

    assert outcome == RunOutcome.COMPLETED
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_paused_execution_is_blocked_before_step() -> None:
    worker, state, execution_id, executor = _worker()
    state.start_execution(execution_id)
    state.pause_execution(execution_id)

    outcome = await worker.run(execution_id)
    execution = worker._repo.find_by_id(execution_id)

    assert outcome == RunOutcome.PAUSED
    assert executor.calls == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.PAUSED
    assert execution.steps[0].status == StepStatus.PENDING


@pytest.mark.asyncio
async def test_waiting_approval_is_blocked() -> None:
    worker, state, execution_id, executor = _worker()
    state.start_execution(execution_id)
    step = state.list_steps(execution_id)[0]
    state.start_step(execution_id, step.step_execution_id)
    state.pause_for_approval(execution_id, step.step_execution_id)

    outcome = await worker.run(execution_id)

    assert outcome == RunOutcome.WAITING_APPROVAL
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_cancelled_execution_is_blocked_without_failure() -> None:
    worker, state, execution_id, executor = _worker()
    state.cancel_execution(execution_id)

    outcome = await worker.run(execution_id)
    execution = worker._repo.find_by_id(execution_id)

    assert outcome == RunOutcome.BLOCKED
    assert executor.calls == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED


@pytest.mark.asyncio
async def test_paused_execution_resumes_and_continues() -> None:
    worker, state, execution_id, executor = _worker()
    state.start_execution(execution_id)
    state.pause_execution(execution_id)
    assert await worker.run(execution_id) == RunOutcome.PAUSED

    state.resume_execution(execution_id)
    assert await worker.run(execution_id) == RunOutcome.COMPLETED
    assert executor.calls == 1
