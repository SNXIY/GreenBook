"""Phase 10-D failure decision and Worker-consumption tests."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.events import EventType
from greenbook_agent_core.execution.failure_decision import (
    FailureCategory,
    FailureClassifier,
    FailureDecisionEngine,
    FailurePolicyContext,
    RecoveryAction,
)
from greenbook_agent_core.execution.models import ExecutionStatus, StepStatus
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.worker import ExecutionWorker, RunOutcome
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_agent_core.planning.models import ExecutablePlan
from greenbook_contracts import ExternalAgentFailure, ToolResult, normalize_external_failure
from greenbook_contracts.external_agent_failure import RecoveryAction as ExternalRecoveryAction
from greenbook_contracts.external_agent_failure import SideEffectState


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


def test_business_rejection_is_not_reconciliation() -> None:
    decision = FailureDecisionEngine().decide(
        _failure("BUSINESS_REJECTED", retryable=False, request_sent=True)
    )

    assert decision.category == FailureCategory.BUSINESS_REJECTED
    assert decision.reconciliation_required is False
    assert decision.retry_allowed is False


def test_internal_runtime_failure_is_not_reconciled() -> None:
    decision = FailureDecisionEngine().decide(
        _failure("TOOL_EXECUTION_FAILED", retryable=False, request_sent=None)
    )

    assert decision.category == FailureCategory.SERVER_FAILURE
    assert decision.reconciliation_required is False
    assert decision.retry_allowed is False


def test_direct_internal_failure_fact_does_not_reconcile_without_effect_evidence() -> None:
    failure = ExternalAgentFailure(
        error_code="INTERNAL_ERROR",
        dependency="runtime",
        retryable=False,
        user_visible_message="internal",
        recovery_action=ExternalRecoveryAction.FAIL,
        request_sent=None,
        side_effect_state=SideEffectState.UNKNOWN,
    )
    decision = FailureDecisionEngine().decide(failure)
    assert decision.reconciliation_required is False
    assert decision.classification.recovery_action == ExternalRecoveryAction.FAIL


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
        PlanStep(capability="SEARCH_COMMUNITY", ordinal=1, tool_name="community.search_public_posts")
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
        steps=[PlanStep(capability="SEARCH_COMMUNITY", ordinal=1, tool_name="community.search_public_posts")],
        is_valid=True,
    )
    execution = worker.init_from_plan(executable, task_id="failure-decision-task")

    # A transient, retryable failure with a proven-safe boundary (not sent,
    # no side effect) must not be finalized in the same pass: the execution
    # waits for the retry worker (design goal 0813 — recoverable failures are
    # never reported as terminal failures).
    assert await worker.run(execution.execution_id) == RunOutcome.WAITING_ASYNC
    current = worker._repo.find_by_id(execution.execution_id)
    assert current is not None
    assert current.status == ExecutionStatus.RUNNING
    assert current.steps[0].status == StepStatus.FAILED_RETRYABLE

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


@pytest.mark.asyncio
async def test_worker_routes_unknown_write_outcome_to_reconciliation() -> None:
    """RESULT_UNKNOWN must wait for reconciliation, never report failure/success."""

    registry = CapabilityRegistry()

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == "publication.cancel_schedule"
        assert tool_args == {"schedule_id": "schedule-java"}
        return {
            "ok": False,
            "code": "RESULT_UNKNOWN",
            "message": "Java response timed out after the cancellation write",
            "request_sent": None,
            "retryable": False,
            "state": {
                "idempotency_key": "cancel-schedule-java",
                "side_effect_state": "UNKNOWN",
            },
        }

    worker = ExecutionWorker(CapabilityExecutor(registry, handler))
    executable = ExecutablePlan(
        steps=[
            PlanStep(
                capability="CANCEL_SCHEDULE",
                ordinal=1,
                constraints={"schedule_id": "schedule-java"},
            )
        ],
        is_valid=True,
    )
    execution = worker.init_from_plan(executable, task_id="result-unknown-task")

    assert await worker.run(execution.execution_id) == RunOutcome.WAITING_HUMAN
    current = worker._repo.find_by_id(execution.execution_id)
    assert current is not None
    assert current.status == ExecutionStatus.WAITING_HUMAN
    # The actual schedule outcome remains deliberately uncommitted until a
    # reconciler reads the authoritative Java business state.
    assert current.steps[0].status == StepStatus.RUNNING
    assert current.steps[0].checkpoint_data["reconciliation_required"] is True
    assert any(
        event.event_type == EventType.EXECUTION_RECONCILIATION_REQUIRED
        for event in worker._state.event_store.list_events(execution.execution_id)
    )
