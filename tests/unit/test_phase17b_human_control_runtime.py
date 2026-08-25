"""Phase17-B durable human-control Runtime contract tests."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
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


# ── approval double-decision guard (design goal 0813) ───────────────────────


@pytest.mark.asyncio
async def test_concurrent_approvals_only_one_resumes_execution(tmp_path) -> None:
    """Two concurrent APPROVE decisions for one PENDING request must run the
    side effect exactly once: the atomic PENDING->APPROVED flip rejects the
    losing writer before it can re-queue the execution."""
    from greenbook_agent_core.human import (
        ApprovalRequest,
        ApprovalRequestStatus,
        MemoryApprovalRequestStore,
    )
    from greenbook_agent_core.human.approval_runtime_service import (
        ApprovalRuntimeService,
    )

    bind = sa.create_engine(f"sqlite:///{tmp_path / 'approval-race.db'}")
    repository, events, checkpoints, state, manager = _runtime(bind)
    execution = PlanExecution(
        plan_id="approval-plan",
        task_id="approval-task",
        steps=[StepExecution(
            step_id="publish",
            capability="SCHEDULE_PUBLISH",
            ordinal=1,
        )],
    )
    execution.steps[0].execution_id = execution.execution_id
    execution.steps[0].status = StepStatus.WAITING_APPROVAL
    repository.save(execution)

    class _FakeQueue:
        def __init__(self) -> None:
            self.requeues = 0

        def get_by_execution_id(self, execution_id: str):
            from types import SimpleNamespace
            return SimpleNamespace(execution_id=execution_id, trace_id="t", payload={})

        def enqueue(self, execution_id, *, trace_id="", payload=None, requeue=False):
            self.requeues += 1
            from types import SimpleNamespace

            from greenbook_agent_core.execution.execution_queue import ExecutionQueueStatus
            return SimpleNamespace(status=ExecutionQueueStatus.READY)

    queue = _FakeQueue()
    store = MemoryApprovalRequestStore()
    request = ApprovalRequest(
        approval_id="approval-1",
        execution_id=execution.execution_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        operation="publication.publish_now",
        message="Publish draft now",
    )
    await store.save(request)
    service = ApprovalRuntimeService(
        store=store,
        runtime_manager=manager,
        state_manager=state,
        execution_queue=queue,
    )

    results = await asyncio.gather(
        service.decide(
            "approval-1",
            decision=ApprovalRequestStatus.APPROVED,
            user_id="u1",
            tenant_id="t1",
        ),
        service.decide(
            "approval-1",
            decision=ApprovalRequestStatus.APPROVED,
            user_id="u1",
            tenant_id="t1",
        ),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, Exception)]
    conflicts = [item for item in results if isinstance(item, ValueError)]
    assert len(successes) == 1, "exactly one concurrent decision may succeed"
    assert len(conflicts) == 1, "the losing writer must be rejected"
    assert queue.requeues == 1, "the execution must be re-queued exactly once"


@pytest.mark.asyncio
async def test_second_decision_after_terminal_is_rejected(tmp_path) -> None:
    from greenbook_agent_core.human import (
        ApprovalRequest,
        ApprovalRequestStatus,
        MemoryApprovalRequestStore,
    )
    from greenbook_agent_core.human.approval_runtime_service import (
        ApprovalRuntimeService,
    )

    bind = sa.create_engine(f"sqlite:///{tmp_path / 'approval-once.db'}")
    repository, events, checkpoints, state, manager = _runtime(bind)
    execution = PlanExecution(
        plan_id="approval-once-plan",
        task_id="approval-once-task",
        steps=[StepExecution(
            step_id="publish",
            capability="SCHEDULE_PUBLISH",
            ordinal=1,
        )],
    )
    execution.steps[0].execution_id = execution.execution_id
    execution.steps[0].status = StepStatus.WAITING_APPROVAL
    repository.save(execution)

    class _FakeQueue:
        def get_by_execution_id(self, execution_id: str):
            from types import SimpleNamespace
            return SimpleNamespace(execution_id=execution_id, trace_id="t", payload={})

        def enqueue(self, execution_id, *, trace_id="", payload=None, requeue=False):
            from types import SimpleNamespace

            from greenbook_agent_core.execution.execution_queue import ExecutionQueueStatus
            return SimpleNamespace(status=ExecutionQueueStatus.READY)

    store = MemoryApprovalRequestStore()
    request = ApprovalRequest(
        approval_id="approval-once",
        execution_id=execution.execution_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        message="Approve once",
    )
    await store.save(request)
    service = ApprovalRuntimeService(
        store=store,
        runtime_manager=manager,
        state_manager=state,
        execution_queue=_FakeQueue(),
    )

    first = await service.decide(
        "approval-once",
        decision=ApprovalRequestStatus.APPROVED,
        user_id="u1",
        tenant_id="t1",
    )
    assert first.success is True
    with pytest.raises(ValueError):
        await service.decide(
            "approval-once",
            decision=ApprovalRequestStatus.APPROVED,
            user_id="u1",
            tenant_id="t1",
        )


@pytest.mark.asyncio
async def test_foreign_user_cannot_decide_approval(tmp_path) -> None:
    """Ownership isolation on the durable approval boundary: only the
    approval's user+tenant may decide it.  A foreign writer must be rejected
    before any state transition, and the route layer maps that rejection to
    404 (this replaces the legacy in-memory store ownership contract removed
    in Phase 4)."""
    from greenbook_agent_core.human import (
        ApprovalRequest,
        ApprovalRequestStatus,
        MemoryApprovalRequestStore,
    )
    from greenbook_agent_core.human.approval_runtime_service import (
        ApprovalRuntimeService,
    )

    bind = sa.create_engine(f"sqlite:///{tmp_path / 'approval-owner.db'}")
    repository, _events, _checkpoints, state, manager = _runtime(bind)
    execution = PlanExecution(
        plan_id="approval-owner-plan",
        task_id="approval-owner-task",
        steps=[StepExecution(
            step_id="publish",
            capability="SCHEDULE_PUBLISH",
            ordinal=1,
        )],
    )
    execution.steps[0].execution_id = execution.execution_id
    execution.steps[0].status = StepStatus.WAITING_APPROVAL
    repository.save(execution)

    class _FakeQueue:
        def get_by_execution_id(self, execution_id: str):
            return None

        def enqueue(self, execution_id, *, trace_id="", payload=None, requeue=False):
            return None

    store = MemoryApprovalRequestStore()
    request = ApprovalRequest(
        approval_id="approval-owner",
        execution_id=execution.execution_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        operation="publication.publish_now",
        message="Publish draft now",
    )
    await store.save(request)
    service = ApprovalRuntimeService(
        store=store,
        runtime_manager=manager,
        state_manager=state,
        execution_queue=_FakeQueue(),
    )

    with pytest.raises(PermissionError):
        await service.decide(
            "approval-owner",
            decision=ApprovalRequestStatus.APPROVED,
            user_id="u2",
            tenant_id="t1",
        )
    # The foreign write changed nothing: the request stays PENDING.
    remaining = await store.find_by_id("approval-owner")
    assert remaining is not None
    assert remaining.status == ApprovalRequestStatus.PENDING
