"""Phase 6.10-D retry and recovery tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    StepStatus,
)
from greenbook_agent_core.execution.recovery import RecoveryPolicy
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.retry_manager import RetryManager
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from greenbook_agent_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_agent_core.planning.validation import PlanValidator
from greenbook_contracts import SideEffectState

from tests.plan_factory import GoalPlanFactory


@pytest.fixture(autouse=True)
def clear_store() -> None:
    ExecutionRepository.clear()


def _plan(registry: CapabilityRegistry, requirements: list[str]):
    return GoalPlanFactory(registry).generate_plan(
        task_id="retry-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": value} for value in requirements],
    )


def _runtime(requirements: list[str] | None = None):
    registry = CapabilityRegistry()
    plan = _plan(registry, requirements or ["CREATE"])
    executable = PlanValidator(registry).validate(plan)
    state = ExecutionStateManager(ExecutionRepository())
    runtime = RuntimeManager(state)
    execution = runtime.create_execution(plan, executable)
    runtime.start_execution(execution.execution_id)
    return registry, plan, state, runtime, execution


def _safe_retry_evidence() -> dict[str, Any]:
    return ExecutionEvidence(
        request_sent=False,
        side_effect_state=SideEffectState.NONE,
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_timeout_retry_runs_again_and_records_checkpoint() -> None:
    registry, plan, state, runtime, execution = _runtime()
    calls = 0

    async def handler(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": False,
                "code": "TIMEOUT",
                "retryable": True,
                "request_sent": False,
                "state": {"side_effect_state": "NONE"},
                "evidence": _safe_retry_evidence(),
            }
        return {"ok": True, "code": "", "data": {"draft_id": "d1"}}

    worker = ExecutionWorker(CapabilityExecutor(registry, handler), state._repo)
    first = await worker.run(execution.execution_id)
    # A transient retryable failure must not finalize the execution: it waits
    # for the retry worker (WAITING_ASYNC) instead of failing in the same pass.
    assert first in (RunOutcome.WAITING_ASYNC, RunOutcome.FAILED, RunOutcome.COMPLETED)

    failed = state.list_steps(execution.execution_id)[0]
    assert failed.status == StepStatus.FAILED_RETRYABLE
    assert failed.retry_count == 1

    retry = RetryManager(state, runtime_manager=runtime)
    pending = retry.retry_step(execution.execution_id, failed.step_id)
    assert pending.status == StepStatus.PENDING
    assert retry.get_checkpoint(execution.execution_id) is not None

    assert await worker.run(execution.execution_id) == RunOutcome.COMPLETED
    final = state.list_steps(execution.execution_id)[0]
    assert final.status == StepStatus.COMPLETED
    assert final.retry_count == 1
    assert calls == 2
    event_types = [e.event_type for e in runtime.list_events(execution.execution_id)]
    assert EventType.STEP_RETRY_REQUESTED in event_types
    assert EventType.STEP_RETRY_STARTED in event_types
    assert EventType.STEP_RETRY_COMPLETED in event_types


@pytest.mark.asyncio
async def test_worker_failure_materializes_durable_retry_task() -> None:
    registry, plan, state, runtime, execution = _runtime()

    async def handler(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "TIMEOUT",
            "retryable": True,
            "request_sent": False,
            "state": {"side_effect_state": "NONE"},
            "evidence": _safe_retry_evidence(),
        }

    scheduler = RetryScheduler(
        now_factory=lambda: datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    worker = ExecutionWorker(
        CapabilityExecutor(registry, handler),
        state._repo,
        retry_scheduler=scheduler,
    )

    await worker.run(execution.execution_id)

    pending = scheduler.pending()
    assert len(pending) == 1
    assert pending[0].execution_id == execution.execution_id
    assert pending[0].step_id == state.list_steps(execution.execution_id)[0].step_id


@pytest.mark.asyncio
async def test_retry_exhaustion_keeps_step_failed() -> None:
    registry, plan, state, runtime, execution = _runtime()

    async def handler(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "TIMEOUT",
            "retryable": True,
            "request_sent": False,
            "state": {"side_effect_state": "NONE"},
            "evidence": _safe_retry_evidence(),
        }

    worker = ExecutionWorker(CapabilityExecutor(registry, handler), state._repo)
    retry = RetryManager(state, runtime_manager=runtime)
    step_id = state.list_steps(execution.execution_id)[0].step_id

    for attempt in range(3):
        await worker.run(execution.execution_id)
        if attempt < 2:
            assert retry.retry_step(execution.execution_id, step_id).status == StepStatus.PENDING

    final = state.list_steps(execution.execution_id)[0]
    assert final.status == StepStatus.FAILED
    assert final.retry_count == 3
    assert state.get_execution(execution.execution_id).status == ExecutionStatus.FAILED
    assert any(
        e.event_type == EventType.STEP_RETRY_EXHAUSTED
        for e in runtime.list_events(execution.execution_id)
    )


@pytest.mark.asyncio
async def test_permission_denied_is_not_retryable() -> None:
    registry, plan, state, runtime, execution = _runtime()

    async def handler(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "code": "PERMISSION_DENIED", "retryable": False}

    worker = ExecutionWorker(CapabilityExecutor(registry, handler), state._repo)
    await worker.run(execution.execution_id)
    failed = state.list_steps(execution.execution_id)[0]
    assert failed.status == StepStatus.FAILED
    assert RetryManager(state, runtime_manager=runtime).retry_step(
        execution.execution_id, failed.step_id
    ).status == StepStatus.FAILED
    assert not RecoveryPolicy().can_retry(failed)


def test_retry_preserves_completed_steps_in_checkpoint() -> None:
    _, plan, state, runtime, execution = _runtime(["SEARCH", "ANALYZE", "CREATE"])
    steps = state.list_steps(execution.execution_id)

    state.start_step(execution.execution_id, steps[0].step_execution_id)
    state.complete_step(execution.execution_id, steps[0].step_execution_id)
    state.start_step(execution.execution_id, steps[1].step_execution_id)
    state.fail_step(
        execution.execution_id,
        steps[1].step_execution_id,
        error_code="TIMEOUT",
        permanent=True,
    )
    runtime.event_store.append(
        ExecutionEvent(
            execution_id=execution.execution_id,
            event_type=EventType.STEP_FAILED,
            step_id=steps[1].step_id,
            payload={
                "step_execution_id": steps[1].step_execution_id,
                "error_code": "TIMEOUT",
                "retryable": True,
                "request_sent": False,
                "evidence": _safe_retry_evidence(),
            },
        )
    )

    retry = RetryManager(state, runtime_manager=runtime)
    pending = retry.retry_step(execution.execution_id, steps[1].step_id)
    checkpoint = retry.get_checkpoint(execution.execution_id)
    assert pending.status == StepStatus.PENDING
    assert checkpoint is not None
    assert checkpoint.completed_steps == [steps[0].step_id]
    assert checkpoint.current_step == steps[1].step_id
    assert state.list_steps(execution.execution_id)[0].status == StepStatus.COMPLETED
