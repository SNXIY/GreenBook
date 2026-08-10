"""Phase 10-I in-process retry scheduler tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from greenbook_assistant_core.execution.retry_decision import RetryDecision
from greenbook_assistant_core.execution.retry_scheduler import RetryScheduler, RetryTask
from greenbook_assistant_core.execution.failure_decision import FailureCategory


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _decision(**overrides) -> RetryDecision:
    values = {
        "allowed": True,
        "reason": "safe retry",
        "category": FailureCategory.TIMEOUT,
        "raw_error_code": "TIMEOUT",
        "attempt": 1,
        "retry_budget": 2,
        "max_attempts": 2,
        "backoff": 30.0,
    }
    values.update(overrides)
    return RetryDecision(**values)


def test_scheduler_delays_and_claims_due_retry() -> None:
    clock = [NOW]
    scheduler = RetryScheduler(now_factory=lambda: clock[0])

    task = scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-1",
        decision=_decision(),
    )
    assert task is not None
    assert task.next_retry_time == NOW + timedelta(seconds=30)
    assert scheduler.due() == []

    clock[0] += timedelta(seconds=30)
    assert scheduler.due() == [task]
    assert scheduler.count() == 0


def test_duplicate_attempt_is_idempotent_before_and_after_dispatch() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW)
    decision = _decision()
    first = scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-1",
        decision=decision,
    )
    duplicate = scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-1",
        decision=decision,
    )
    assert duplicate == first
    assert scheduler.count() == 1

    scheduler.due(NOW + timedelta(minutes=1))
    after_dispatch = scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-1",
        decision=decision,
    )
    assert after_dispatch == first
    assert scheduler.count() == 0


def test_budget_and_deadline_are_enforced_before_enqueue() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW)
    assert scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-1",
        decision=_decision(retry_budget=0),
    ) is None
    assert scheduler.schedule_decision(
        execution_id="execution-1",
        step_id="step-2",
        decision=_decision(),
        deadline=NOW + timedelta(seconds=5),
    ) is None
    assert scheduler.count() == 0


def test_dispatch_due_uses_retry_manager_instead_of_executing_a_tool() -> None:
    scheduler = RetryScheduler(now_factory=lambda: NOW)
    task = scheduler.schedule(
        RetryTask(
            execution_id="execution-1",
            step_id="step-1",
            attempt=1,
            next_retry_time=NOW,
            backoff=0,
            reason="safe retry",
        )
    )
    assert task is not None

    calls: list[dict[str, object]] = []

    class RetryManager:
        def retry_step(self, execution_id: str, step_id: str, **kwargs):
            calls.append(
                {"execution_id": execution_id, "step_id": step_id, **kwargs}
            )
            return SimpleNamespace(status="PENDING")

    result = scheduler.dispatch_due(retry_manager=RetryManager(), now=NOW)
    assert result[0][0] == task
    assert result[0][1].status == "PENDING"
    assert calls == [
        {
            "execution_id": "execution-1",
            "step_id": "step-1",
            "source": "retry_scheduler",
            "user_requested_retry": False,
        }
    ]
