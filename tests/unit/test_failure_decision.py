"""Phase 10-D failure decision and Worker-consumption tests."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.capability_executor import CapabilityExecutor
from greenbook_assistant_core.execution.events import EventType
from greenbook_assistant_core.execution.failure_decision import (
    FailureCategory,
    FailureClassifier,
    FailureDecisionEngine,
    FailurePolicyContext,
    RecoveryAction,
)
from greenbook_assistant_core.execution.models import ExecutionStatus, StepStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_assistant_core.orchestration.models import PlanStep
from greenbook_assistant_core.planning.models import ExecutablePlan
from greenbook_contracts import ToolResult, normalize_external_failure


@pytest.fixture(autouse=True)
def _clear_store() -> None:
    ExecutionRepository.clear()


def _failure(
    code: str,
    *,
    retryable: bool = True,
    request_sent: bool | None = False,
    state: dict[str, Any] | None = None,
):
    return normalize_external_failure(
        ToolResult(
            ok=False,
            code=code,
            message="test failure",
            user_message="test failure",
            retryable=retryable,
            request_sent=request_sent,
            state=state,
        )
    )


def test_java_backend_unavailable_is_dependency_failure() -> None:
    classification = FailureClassifier().classify(
        _failure("JAVA_BACKEND_UNAVAILABLE")
    )

    assert classification.category == FailureCategory.DEPENDENCY_UNAVAILABLE
    assert classification.raw_error_code == "JAVA_BACKEND_UNAVAILABLE"


def test_invalid_argument_fails_fast() -> None:
    decision = FailureDecisionEngine().decide(
        _failure("INVALID_ARGUMENT", retryable=False)
    )

    assert decision.category == FailureCategory.INVALID_ARGUMENT
    assert decision.action == RecoveryAction.FAIL_FAST
    assert decision.retry_allowed is False


def test_auth_failure_fails_fast() -> None:
    decision = FailureDecisionEngine().decide(
        _failure("AUTH_FAILURE", retryable=True)
    )

    assert decision.category == FailureCategory.AUTH_FAILURE
    assert decision.action == RecoveryAction.FAIL_FAST
    assert decision.retry_allowed is False


def test_unknown_delivery_cannot_be_automatically_retried() -> None:
    decision = FailureDecisionEngine().decide(
        _failure(
            "JAVA_BACKEND_UNAVAILABLE",
            request_sent=None,
            state={"side_effect_state": "UNKNOWN"},
        )
    )

    assert decision.retry_allowed is False
    assert decision.reconciliation_required is True


@pytest.mark.asyncio
async def test_executor_preserves_unknown_delivery_evidence() -> None:
    registry = CapabilityRegistry()

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "JAVA_BACKEND_UNAVAILABLE",
            "retryable": True,
            "request_sent": None,
            "state": {"side_effect_state": "UNKNOWN"},
        }

    result = await CapabilityExecutor(registry, handler).execute_step(
        PlanStep(capability="SEARCH_COMMUNITY", ordinal=1)
    )

    assert result.external_failure is not None
    assert result.request_sent is None
    assert result.external_failure.side_effect_state.value == "UNKNOWN"
    assert result.external_failure.metadata["side_effect_state"] == "UNKNOWN"


def test_request_user_input_is_an_explicit_supported_action() -> None:
    decision = FailureDecisionEngine().decide(
        _failure("INVALID_ARGUMENT", retryable=False),
        FailurePolicyContext(user_input_allowed=True),
    )

    assert decision.action == RecoveryAction.REQUEST_USER_INPUT
    assert decision.human_required is True


@pytest.mark.asyncio
async def test_worker_consumes_failure_decision() -> None:
    registry = CapabilityRegistry()

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "JAVA_BACKEND_UNAVAILABLE",
            "retryable": True,
            "request_sent": False,
        }

    worker = ExecutionWorker(CapabilityExecutor(registry, handler))
    executable = ExecutablePlan(
        steps=[PlanStep(capability="SEARCH_COMMUNITY", ordinal=1)],
        is_valid=True,
    )
    execution = worker.init_from_plan(executable, task_id="failure-decision-task")

    assert await worker.run(execution.execution_id) == RunOutcome.FAILED
    current = worker._repo.find_by_id(execution.execution_id)
    assert current is not None
    assert current.status == ExecutionStatus.FAILED
    assert current.steps[0].status == StepStatus.FAILED

    failure_events = [
        event
        for event in worker._state.event_store.list_events(execution.execution_id)
        if event.event_type == EventType.STEP_FAILED
    ]
    assert failure_events
    assert failure_events[-1].payload["failure_category"] == (
        FailureCategory.DEPENDENCY_UNAVAILABLE.value
    )
    assert failure_events[-1].payload["recovery_action"] == RecoveryAction.FAIL_FAST.value
