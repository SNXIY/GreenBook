"""Phase17-B durable human-control Runtime contract tests."""

from __future__ import annotations

import sqlalchemy as sa
import pytest

from greenbook_agent_core.execution.events import EventType
from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.models import (
    ExecutionControlState,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.execution.persistent_stores import (
    PostgresCheckpointStore,
    PostgresExecutionEventStore,
)
from greenbook_agent_core.execution.postgres_repository import (
    PostgresExecutionRepository,
)
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.execution.worker import ExecutionWorker, RunOutcome


def _execution() -> PlanExecution:
    steps = [
        StepExecution(
            step_id="generate",
            capability="GENERATE_CONTENT",
            ordinal=1,
        ),
        StepExecution(
            step_id="validate",
            capability="VALIDATE_QUALITY",
            ordinal=2,
            depends_on=["generate"],
        ),
        StepExecution(
            step_id="publish",
            capability="SCHEDULE_PUBLISH",
            ordinal=3,
            depends_on=["validate"],
        ),
    ]
    execution = PlanExecution(plan_id="phase17b-plan", task_id="phase17b-task", steps=steps)
    for step in execution.steps:
        step.execution_id = execution.execution_id
    return execution


class _RecordingExecutor:
    def __init__(self, *, after_generate=None) -> None:
        self.calls: list[str] = []
        self._after_generate = after_generate

    async def execute_step(self, step) -> ExecutionResult:
        self.calls.append(step.capability)
        if step.capability == "GENERATE_CONTENT" and self._after_generate is not None:
            self._after_generate()
        return ExecutionResult.success(step.capability, f"test.{step.capability.lower()}", {})


def _runtime(bind):
    repository = PostgresExecutionRepository(bind)
    event_store = PostgresExecutionEventStore(bind)
    checkpoint_store = PostgresCheckpointStore(bind)
    state = ExecutionStateManager(repository, event_store=event_store)
    manager = RuntimeManager(state, checkpoint_store=checkpoint_store)
    return repository, event_store, checkpoint_store, state, manager


@pytest.mark.asyncio
async def test_pause_after_generate_checkpoints_and_resume_at_validate(tmp_path) -> None:
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'phase17b.db'}")
    repository, events, checkpoints, state, manager = _runtime(bind)
    execution = _execution()
    repository.save(execution)

    first_executor = _RecordingExecutor(
        after_generate=lambda: manager.pause_execution(
            execution.execution_id,
            reason="Operator requested review",
        )
    )
    first_worker = ExecutionWorker(
        first_executor,
        repository=repository,
        event_store=events,
        checkpoint_store=checkpoints,
    )

    outcome = await first_worker.run(execution.execution_id)

    assert outcome == RunOutcome.PAUSED
    assert first_executor.calls == ["GENERATE_CONTENT"]
    paused = repository.find_by_id(execution.execution_id)
    assert paused is not None
    assert paused.status == ExecutionStatus.PAUSED
    assert paused.control_state == ExecutionControlState.PAUSED
    assert paused.steps[0].status == StepStatus.COMPLETED
    assert paused.steps[1].status == StepStatus.PENDING
    checkpoint = checkpoints.latest(execution.execution_id)
    assert checkpoint is not None
    assert checkpoint.completed_steps == ["generate"]
    assert checkpoint.current_step == "validate"
    assert EventType.EXECUTION_CHECKPOINT_SAVED in {
        event.event_type for event in events.list_events(execution.execution_id)
    }

    # Simulate a new Worker process: rebuild every adapter from the same DB.
    repository2, events2, checkpoints2, state2, manager2 = _runtime(bind)
    resuming = manager2.resume_execution(execution.execution_id)
    assert resuming.control_state == ExecutionControlState.RESUMING
    second_executor = _RecordingExecutor()
    second_worker = ExecutionWorker(
        second_executor,
        repository=repository2,
        event_store=events2,
        checkpoint_store=checkpoints2,
    )

    resumed_outcome = await second_worker.run(execution.execution_id)

    assert resumed_outcome == RunOutcome.COMPLETED
    assert second_executor.calls == ["VALIDATE_QUALITY", "SCHEDULE_PUBLISH"]
    completed = repository2.find_by_id(execution.execution_id)
    assert completed is not None
    assert completed.status == ExecutionStatus.COMPLETED
    assert completed.control_state == ExecutionControlState.RUNNING


@pytest.mark.asyncio
async def test_cancel_during_step_prevents_all_later_tool_calls(tmp_path) -> None:
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'cancel.db'}")
    repository, events, checkpoints, _state, manager = _runtime(bind)
    execution = _execution()
    repository.save(execution)
    executor = _RecordingExecutor(
        after_generate=lambda: manager.cancel_execution(
            execution.execution_id,
            reason="Operator cancelled publication",
        )
    )
    worker = ExecutionWorker(
        executor,
        repository=repository,
        event_store=events,
        checkpoint_store=checkpoints,
    )

    outcome = await worker.run(execution.execution_id)

    assert outcome == RunOutcome.BLOCKED
    assert executor.calls == ["GENERATE_CONTENT"]
    cancelled = repository.find_by_id(execution.execution_id)
    assert cancelled is not None
    assert cancelled.status == ExecutionStatus.CANCELLED
    assert cancelled.control_state == ExecutionControlState.CANCELLED
    assert cancelled.control_reason == "Operator cancelled publication"
    assert [step.status for step in cancelled.steps[1:]] == [
        StepStatus.SKIPPED,
        StepStatus.SKIPPED,
    ]


@pytest.mark.asyncio
async def test_pause_racing_with_last_step_does_not_turn_completed_into_paused(tmp_path) -> None:
    bind = sa.create_engine(f"sqlite:///{tmp_path / 'terminal-race.db'}")
    repository, events, checkpoints, _state, manager = _runtime(bind)
    execution = PlanExecution(
        plan_id="single-plan",
        task_id="single-task",
        steps=[StepExecution(step_id="generate", capability="GENERATE_CONTENT", ordinal=1)],
    )
    execution.steps[0].execution_id = execution.execution_id
    repository.save(execution)
    executor = _RecordingExecutor(
        after_generate=lambda: manager.pause_execution(execution.execution_id)
    )
    worker = ExecutionWorker(
        executor,
        repository=repository,
        event_store=events,
        checkpoint_store=checkpoints,
    )

    assert await worker.run(execution.execution_id) == RunOutcome.COMPLETED
    final = repository.find_by_id(execution.execution_id)
    assert final is not None
    assert final.status == ExecutionStatus.COMPLETED
    assert final.control_state == ExecutionControlState.RUNNING
