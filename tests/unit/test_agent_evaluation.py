"""Phase 6.11 Agent Evaluation Platform tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConstraintType,
    IntentAction,
    IntentConstraint,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.observability.models import EventType as TraceEventType
from greenbook_assistant_core.observability.models import Trace, TraceEvent

from greenbook_evaluation.badcase import BadCase, BadCaseStore, FailureType
from greenbook_evaluation.metrics import ExecutionMetricsCalculator
from greenbook_evaluation.planner_evaluator import PlannerEvaluator
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


def test_planner_evaluation_checks_actions_resources_order_and_constraints() -> None:
    spec = IntentSpec(
        actions=[
            IntentAction(action=ActionType.SEARCH, resource=ResourceType.CONTENT),
            IntentAction(action=ActionType.CREATE, resource=ResourceType.DRAFT),
            IntentAction(action=ActionType.PUBLISH, resource=ResourceType.SCHEDULE),
        ],
        constraints=[IntentConstraint(type=ConstraintType.TIME, value="tomorrow")],
    )
    plan = TaskPlan(steps=[
        PlanStep(
            step_id="search",
            ordinal=1,
            capability="SEARCH_COMMUNITY",
            output_artifact_type="SEARCH_RESULT",
        ),
        PlanStep(
            step_id="create",
            ordinal=2,
            capability="GENERATE_CONTENT",
            depends_on=["search"],
            output_artifact_type="DRAFT",
        ),
        PlanStep(
            step_id="publish",
            ordinal=3,
            capability="SCHEDULE_PUBLISH",
            depends_on=["create"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="SCHEDULE",
            constraints={"time": "tomorrow"},
        ),
    ])
    result = PlannerEvaluator().evaluate(spec, plan)
    assert result.passed is True
    assert result.action_coverage == 1.0
    assert result.resource_match is True
    assert result.order_reasonable is True
    assert result.constraints_forwarded is True


def test_badcase_store_preserves_regression_snapshot() -> None:
    store = BadCaseStore()
    case = BadCase(
        case_id="case-1",
        category="EXECUTION",
        failure_type=FailureType.RECOVERY_FAILED,
        user_input="publish the article",
        intent_spec={"actions": ["PUBLISH"]},
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
