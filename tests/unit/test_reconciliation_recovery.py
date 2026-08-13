"""Phase 11-C reconciliation-to-Execution recovery tests."""

from __future__ import annotations

from typing import Any

import pytest

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.events import EventType
from greenbook_agent_core.execution.models import ExecutionStatus, StepStatus
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.reconciliation import (
    ReconciliationAction,
    ReconciliationRecoveryService,
    ReconciliationService,
)
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.state_manager import ExecutionStateManager
from tests.plan_factory import GoalPlanFactory
from greenbook_agent_core.planning.validation import PlanValidator


@pytest.fixture(autouse=True)
def clear_store() -> None:
    ExecutionRepository.clear()


def _failed_execution(*, retryable: bool = False):
    registry = CapabilityRegistry()
    plan = GoalPlanFactory(registry).generate_plan(
        task_id="reconciliation-task",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    executable = PlanValidator(registry).validate(plan)
    state = ExecutionStateManager(ExecutionRepository())
    execution = state.init_execution(plan, executable)
    state.start_execution(execution.execution_id)
    step = state.start_step(execution.execution_id, execution.steps[0].step_execution_id)
    state.fail_step(
        execution.execution_id,
        step.step_execution_id,
        error_code="TIMEOUT",
        error_message="Runtime lost the response",
        permanent=not retryable,
    )
    return state, execution.execution_id, step.step_id, step.step_execution_id


def _operation(execution_id: str, step_id: str) -> ExternalOperationRecord:
    return ExternalOperationRecord(
        operation_id="operation-reconcile-1",
        execution_id=execution_id,
        step_id=step_id,
        tool_name="content.publish",
        status=OperationStatus.UNKNOWN,
        external_operation_id="external-reconcile-1",
    )


def _service(status: Any, store: ExternalOperationStore) -> ReconciliationService:
    return ReconciliationService(
        store=store,
        query=lambda **_identifiers: status,
    )


def test_external_success_recovers_failed_execution() -> None:
    state, execution_id, step_id, _ = _failed_execution()
    operation = _operation(execution_id, step_id)
    recovery = ReconciliationRecoveryService(
        state_manager=state,
        reconciliation=_service(OperationStatus.SUCCEEDED, ExternalOperationStore()),
    )

    result = recovery.reconcile_operation(operation)

    assert result.action == ReconciliationAction.RECOVER_EXECUTION
    assert result.execution_updated is True
    assert state.list_steps(execution_id)[0].status == StepStatus.COMPLETED
    assert state.get_execution(execution_id).status == ExecutionStatus.COMPLETED
    assert any(
        event.event_type == EventType.STEP_RECONCILIATION_SUCCEEDED
        for event in state.event_store.list_events(execution_id)
    )


def test_external_failure_marks_step_failed_without_retry() -> None:
    state, execution_id, step_id, _ = _failed_execution(retryable=True)
    operation = _operation(execution_id, step_id)
    recovery = ReconciliationRecoveryService(
        state_manager=state,
        reconciliation=_service(OperationStatus.FAILED, ExternalOperationStore()),
    )

    result = recovery.reconcile_operation(operation)

    assert result.action == ReconciliationAction.MARK_FAILED
    assert result.execution_updated is True
    assert state.list_steps(execution_id)[0].status == StepStatus.FAILED
    assert state.get_execution(execution_id).status == ExecutionStatus.FAILED


def test_not_found_routes_to_manual_handling_without_claiming_success() -> None:
    state, execution_id, step_id, _ = _failed_execution()
    operation = _operation(execution_id, step_id)
    recovery = ReconciliationRecoveryService(
        state_manager=state,
        reconciliation=_service(OperationStatus.NOT_FOUND, ExternalOperationStore()),
    )

    result = recovery.reconcile_operation(operation)

    assert result.action == ReconciliationAction.REQUIRE_MANUAL_INTERVENTION
    assert result.execution_updated is True
    assert state.get_execution(execution_id).status == ExecutionStatus.WAITING_HUMAN
    assert state.list_steps(execution_id)[0].status == StepStatus.FAILED


def test_unknown_does_not_mutate_execution() -> None:
    state, execution_id, step_id, step_execution_id = _failed_execution(retryable=True)
    before = state.get_execution(execution_id)
    operation = _operation(execution_id, step_id)
    recovery = ReconciliationRecoveryService(
        state_manager=state,
        reconciliation=ReconciliationService(
            store=ExternalOperationStore(),
            query=lambda **_identifiers: OperationStatus.UNKNOWN,
        ),
    )

    result = recovery.reconcile_operation(operation)

    assert result.action == ReconciliationAction.KEEP_UNKNOWN
    assert result.execution_updated is False
    after = state.get_execution(execution_id)
    assert after.status == before.status
    assert state.list_steps(execution_id)[0].step_execution_id == step_execution_id
    assert state.list_steps(execution_id)[0].status == StepStatus.FAILED_RETRYABLE
