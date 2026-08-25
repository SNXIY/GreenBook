"""Phase 10-F evidence-aware retry decision tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.events import EventType
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.failure_decision import FailureCategory
from greenbook_agent_core.execution.models import StepStatus
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.retry_decision import (
    RetryContext,
    RetryDecisionEngine,
)
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.worker import ExecutionWorker
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_agent_core.planning.models import ExecutablePlan
from greenbook_contracts import SideEffectState, ToolResult, normalize_external_failure


def _failure(
    evidence: ExecutionEvidence,
    *,
    code: str = "DEPENDENCY_UNAVAILABLE",
    retryable: bool = True,
):
    return normalize_external_failure(
        ToolResult(
            ok=False,
            code=code,
            retryable=retryable,
            request_sent=evidence.request_sent,
            state={"side_effect_state": evidence.side_effect_state.value},
        ),
        evidence=evidence,
    )


def _evidence(**overrides: Any) -> ExecutionEvidence:
    values: dict[str, Any] = {
        "execution_id": "execution-1",
        "step_id": "step-1",
        "invocation_id": "invocation-1",
        "operation_id": "operation-1",
        "request_sent": False,
        "side_effect_state": SideEffectState.NONE,
        "request_time": "2026-08-10T00:00:00+00:00",
    }
    values.update(overrides)
    return ExecutionEvidence(**values)


def _context(**overrides: Any) -> RetryContext:
    values: dict[str, Any] = {
        "attempt": 1,
        "retry_budget": 2,
        "max_attempts": 3,
    }
    values.update(overrides)
    return RetryContext(**values)


def test_explicit_not_sent_none_allows_retry() -> None:
    evidence = _evidence(
        request_sent=False,
        side_effect_state=SideEffectState.NONE,
    )
    decision = RetryDecisionEngine().decide(
        _failure(evidence),
        _context(),
        evidence=evidence,
    )

    assert decision.allowed is True
    assert decision.category == FailureCategory.DEPENDENCY_UNAVAILABLE
    assert decision.requires_reconciliation is False


def test_unknown_delivery_is_not_retryable() -> None:
    evidence = _evidence(
        request_sent=None,
        side_effect_state=SideEffectState.UNKNOWN,
    )
    decision = RetryDecisionEngine().decide(
        _failure(evidence),
        _context(),
        evidence=evidence,
    )

    assert decision.allowed is False
    assert decision.requires_reconciliation is True
    assert "request_sent" in decision.evidence_requirements


def test_sent_possible_enters_reconciliation() -> None:
    evidence = _evidence(
        request_sent=True,
        side_effect_state=SideEffectState.POSSIBLE,
        receipt_id="receipt-1",
    )
    decision = RetryDecisionEngine().decide(
        _failure(evidence),
        _context(),
        evidence=evidence,
    )

    assert decision.allowed is False
    assert decision.requires_reconciliation is True
    assert decision.operation_id == "operation-1"


def test_backoff_and_deadline_are_pure_decision_data() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    evidence = _evidence()
    engine = RetryDecisionEngine(now_factory=lambda: now)
    decision = engine.decide(
        _failure(evidence),
        _context(attempt=2, backoff_seconds=5),
        evidence=evidence,
    )

    assert decision.allowed is True
    assert decision.backoff == 10
    assert decision.retry_after == datetime(2026, 8, 10, 0, 0, 10, tzinfo=UTC)


def test_missing_evidence_fails_closed() -> None:
    failure = normalize_external_failure(
        ToolResult(
            ok=False,
            code="TIMEOUT",
            retryable=True,
            request_sent=None,
            state={"side_effect_state": "UNKNOWN"},
        )
    )
    decision = RetryDecisionEngine().decide(failure, _context())

    assert decision.allowed is False
    assert decision.requires_reconciliation is True
    assert decision.evidence_requirements == (
        "request_sent",
        "side_effect_state",
    )


def test_worker_persists_evidence_for_later_retry_decision() -> None:
    """The Worker event is the cross-process Evidence hand-off boundary."""

    # This test exercises the same object boundary without starting any
    # external service. The async handler is intentionally represented by a
    # synchronous coroutine below so the Worker path remains realistic.
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        evidence = _evidence(
            request_sent=False,
            side_effect_state=SideEffectState.NONE,
        )
        return {
            "ok": False,
            "code": "DEPENDENCY_UNAVAILABLE",
            "retryable": True,
            "request_sent": False,
            "state": {"side_effect_state": "NONE"},
            "evidence": evidence.model_dump(mode="json"),
        }

    registry = CapabilityRegistry()
    worker = ExecutionWorker(
        CapabilityExecutor(registry, handler),
        repository=ExecutionRepository(),
    )
    execution = worker.init_from_plan(
        ExecutablePlan(
            steps=[PlanStep(capability="SEARCH_COMMUNITY", ordinal=1, tool_name="community.search_public_posts")],
            is_valid=True,
        ),
        task_id="retry-evidence-task",
    )

    import asyncio

    asyncio.run(worker.run(execution.execution_id))
    step = worker._state.list_steps(execution.execution_id)[0]
    assert step.status == StepStatus.FAILED_RETRYABLE
    failed_events = [
        event
        for event in worker._state.event_store.list_events(execution.execution_id)
        if event.event_type == EventType.STEP_FAILED
    ]
    assert failed_events[-1].payload["evidence"]["request_sent"] is False
    assert failed_events[-1].payload["evidence"]["side_effect_state"] == "NONE"

    # The later RetryManager path reads the same persisted event snapshot.
    from greenbook_agent_core.execution.retry_manager import RetryManager

    pending = RetryManager(
        worker._state,
        runtime_manager=RuntimeManager(worker._state),
    ).retry_step(execution.execution_id, step.step_id)
    assert pending.status.value == "PENDING"
