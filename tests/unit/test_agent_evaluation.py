"""Phase 6.11 Agent Evaluation Platform tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.observability.models import EventType as TraceEventType
from greenbook_agent_core.observability.models import Trace, TraceEvent
from greenbook_evaluation.badcase import BadCase, BadCaseStore, FailureType
from greenbook_evaluation.metrics import ExecutionMetricsCalculator
from greenbook_evaluation.runtime_evaluator import (
    ExecutionEvaluator,
    ExecutionRecord,
)


def _step(step_id: str, status: StepStatus, *, retry_count: int = 0) -> StepExecution:
    return StepExecution(
        execution_id="execution-1",
        step_id=step_id,
        capability="SEARCH_COMMUNITY",
        status=status,
        retry_count=retry_count,
    )


def _event(event_type: EventType, *, step_id: str = "") -> ExecutionEvent:
    return ExecutionEvent(
        execution_id="execution-1",
        event_type=event_type,
        step_id=step_id or None,
    )


def test_successful_execution_evaluation() -> None:
    created = datetime.now(UTC) - timedelta(seconds=2)
    execution = PlanExecution(
        execution_id="execution-1",
        status=ExecutionStatus.COMPLETED,
        steps=[_step("search", StepStatus.COMPLETED)],
        created_at=created.isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
    )
    trace = Trace(
        execution_id=execution.execution_id,
        events=[TraceEvent(
            execution_id=execution.execution_id,
            event_type=TraceEventType.TOOL_INVOKED,
        )],
    )
    result = ExecutionEvaluator().evaluate(ExecutionRecord(
        execution=execution,
        events=[_event(EventType.EXECUTION_COMPLETED)],
        trace=trace,
    ))

    assert result.success is True
    assert result.step_count == 1
    assert result.tool_call_count == 1
    assert result.latency >= 1900
    assert result.quality_score == 1.0


def test_failed_execution_and_retry_evaluation() -> None:
    execution = PlanExecution(
        execution_id="execution-1",
        status=ExecutionStatus.FAILED,
        steps=[_step("search", StepStatus.FAILED, retry_count=2)],
    )
    result = ExecutionEvaluator().evaluate(ExecutionRecord(
        execution=execution,
        events=[
            _event(EventType.STEP_FAILED, step_id="search"),
            _event(EventType.STEP_RETRY_REQUESTED, step_id="search"),
            _event(EventType.APPROVAL_REQUIRED, step_id="search"),
        ],
    ))
    assert result.success is False
    assert result.retry_count == 2
    assert result.failure_count == 1
    assert result.human_intervention is True
    assert result.quality_score < 1.0

    metrics = ExecutionMetricsCalculator.compute([result])
    assert metrics.retry_rate == 1.0
    assert metrics.failure_rate == 1.0
    assert metrics.human_approval_rate == 1.0


def test_badcase_store_preserves_regression_snapshot() -> None:
    store = BadCaseStore()
    case = BadCase(
        case_id="case-1",
        category="EXECUTION",
        failure_type=FailureType.RECOVERY_FAILED,
        user_input="publish the article",
        understanding_snapshot={"command": "PUBLISH"},
        task_plan={"steps": ["PUBLISH"]},
        execution_trace={"events": ["STEP_FAILED"]},
        failure_reason="publish step failed",
        expected_behavior={"status": "COMPLETED"},
    )
    store.save(case)
    saved = store.list_cases()
    assert len(saved) == 1
    assert saved[0].user_input == case.user_input
    assert saved[0].expected_behavior["status"] == "COMPLETED"


def test_behavioral_evaluation_runner_reports_runtime_metrics() -> None:
    from greenbook_evaluation.models import EvalCase
    from greenbook_evaluation.runner import EvaluationRunner

    case = EvalCase(
        case_id="phase55-create",
        category="COMMAND",
        conversation_turns=[{"role": "user", "content": "写一篇Java学习路线文章"}],
        expected_command="CREATE",
        expected_tools=["content.create_draft"],
        expected_task_state="COMPLETED",
    )
    actual = {
        "command": "CREATE",
        "tools": ["content.create_draft"],
        "task_state": "COMPLETED",
        "trace": {
            "conversation_id": "conv-1",
            "task_id": "task-1",
            "goal_id": "goal-1",
            "plan_version": 1,
            "events": [{"name": "TOOL_INVOKED"}],
        },
        "tool_call_count": 1,
    }
    report = EvaluationRunner().run_sync(
        [case],
        handler=lambda _case: actual,
    )

    assert report.total_passed == 1
    assert report.metrics["command_accuracy"] == 1.0
    assert report.results[0].trace["task_id"] == "task-1"
