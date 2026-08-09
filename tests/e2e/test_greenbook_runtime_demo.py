"""Deterministic GreenBook Runtime demo for a complex community operation."""

from __future__ import annotations

import pytest

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.models import ArtifactHandle, StepStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.retry_manager import RetryManager
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.orchestration.context import build_planning_context
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConditionType,
    IntentAction,
    IntentCondition,
    IntentConstraint,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.models import TaskIntent


@pytest.fixture(autouse=True)
def clear_demo_state() -> None:
    ExecutionRepository.clear()


def _intent_spec() -> IntentSpec:
    return IntentSpec(
        mode=IntentMode.CONDITIONAL,
        goal="运营一个 Agent 学习专题",
        actions=[
            IntentAction(action=ActionType.SEARCH, resource=ResourceType.CONTENT, confidence=0.99),
            IntentAction(action=ActionType.ANALYZE, resource=ResourceType.CONTENT, confidence=0.98),
            IntentAction(action=ActionType.UPDATE_OR_CREATE, resource=ResourceType.DRAFT, confidence=0.97),
            IntentAction(action=ActionType.PUBLISH, resource=ResourceType.POST, confidence=0.99),
        ],
        conditions=[
            IntentCondition(
                type=ConditionType.IF_EXISTS,
                resource=ResourceType.DRAFT,
                then_action=ActionType.UPDATE,
                else_action=ActionType.CREATE,
            )
        ],
        constraints=[
            IntentConstraint(type="APPROVAL", value="BEFORE_PUBLISH"),
            IntentConstraint(type="TIME", value="5分钟后"),
        ],
        source="L2",
    )


def _event(
    execution_id: str,
    event_type: EventType,
    step_id: str | None = None,
    **payload: object,
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=execution_id,
        event_type=event_type,
        step_id=step_id,
        payload=payload,
    )


def test_complex_growth_operation_reaches_plan_execution_and_approval_events() -> None:
    user_request = (
        "帮我运营一个 Agent 学习专题：搜索最近热门文章，分析为什么受欢迎，"
        "如果有旧稿就优化，没有就创建，发布前让我确认，确认后五分钟发布"
    )
    intent = _intent_spec()
    task_intent = TaskIntent(
        goal=intent.goal,
        goal_category="CREATE_CONTENT",
        source="L2",
        intent_spec=intent.model_dump(mode="json"),
    )
    context = build_planning_context(task_intent, intent)

    registry = CapabilityRegistry()
    planner = TaskOrchestrator(registry)
    plan = planner.generate_plan(task_id="demo-task-001", planning_context=context)
    executable = PlanValidator(registry).validate(plan)
    event_store = ExecutionEventStore()
    state = ExecutionStateManager(ExecutionRepository(), event_store=event_store)
    runtime = RuntimeManager(state)
    execution = runtime.create_execution(plan, executable)
    retry_manager = RetryManager(state, runtime_manager=runtime)

    assert user_request.startswith("帮我运营")
    assert context.intent_spec == intent
    assert [action.action for action in intent.actions] == [
        ActionType.SEARCH,
        ActionType.ANALYZE,
        ActionType.UPDATE_OR_CREATE,
        ActionType.PUBLISH,
    ]
    assert executable.is_valid
    assert [step.capability for step in plan.steps] == [
        "SEARCH_COMMUNITY",
        "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT",
        "VALIDATE_QUALITY",
        "SCHEDULE_PUBLISH",
    ]
    assert plan.steps[-1].constraints == {
        "approval": "BEFORE_PUBLISH",
        "time": "5分钟后",
    }
    assert execution.status.value == "PENDING"

    runtime.start_execution(execution.execution_id)
    first, second = execution.steps[:2]

    # SEARCH succeeds and emits a materialized artifact event pair.
    state.start_step(execution.execution_id, first.step_execution_id)
    event_store.append(_event(execution.execution_id, EventType.STEP_STARTED, first.step_id))
    state.complete_step(
        execution.execution_id,
        first.step_execution_id,
        ArtifactHandle(artifact_type="SEARCH_RESULT", summary="Found recent popular Agent posts"),
    )
    event_store.append(_event(execution.execution_id, EventType.STEP_COMPLETED, first.step_id))

    # ANALYZE fails transiently, then the existing RetryManager reopens it.
    state.start_step(execution.execution_id, second.step_execution_id)
    event_store.append(_event(execution.execution_id, EventType.STEP_STARTED, second.step_id))
    state.fail_step(
        execution.execution_id,
        second.step_execution_id,
        error_code="TIMEOUT",
        error_message="analysis provider timed out",
    )
    event_store.append(
        _event(
            execution.execution_id,
            EventType.STEP_FAILED,
            second.step_id,
            error_code="TIMEOUT",
            retryable=True,
        )
    )
    assert state.get_execution(execution.execution_id).steps[0].status == StepStatus.COMPLETED
    assert state.get_execution(execution.execution_id).steps[1].status == StepStatus.FAILED_RETRYABLE

    retried = retry_manager.retry_step(execution.execution_id, second.step_id)
    assert retried.status == StepStatus.PENDING
    assert retried.retry_count == 1

    # User-controlled pause/resume is separate from the approval event below.
    runtime.pause_execution(execution.execution_id)
    runtime.resume_execution(execution.execution_id)

    # Finish the retried step and remaining planned steps.
    for step in execution.steps[1:]:
        current = state.get_execution(execution.execution_id).steps[step.ordinal - 1]
        if current.status == StepStatus.PENDING:
            state.start_step(execution.execution_id, current.step_execution_id)
            event_store.append(_event(execution.execution_id, EventType.STEP_STARTED, current.step_id))
        if current.step_id == plan.steps[-1].step_id:
            state.pause_for_approval(execution.execution_id, current.step_execution_id)
            state.approve_and_resume(execution.execution_id, current.step_execution_id)
        state.complete_step(execution.execution_id, current.step_execution_id)
        event_store.append(_event(execution.execution_id, EventType.STEP_COMPLETED, current.step_id))

    final = state.get_execution(execution.execution_id)
    events = event_store.list_events(execution.execution_id)
    event_types = [event.event_type for event in events]

    assert final.status.value == "COMPLETED"
    assert final.completed_step_count == len(plan.steps)
    assert EventType.EXECUTION_CREATED in event_types
    assert EventType.EXECUTION_STARTED in event_types
    assert EventType.STEP_FAILED in event_types
    assert EventType.STEP_RETRY_REQUESTED in event_types
    assert EventType.STEP_RETRY_STARTED in event_types
    assert EventType.EXECUTION_PAUSED in event_types
    assert EventType.EXECUTION_RESUMED in event_types
    assert EventType.APPROVAL_REQUIRED in event_types
    assert EventType.EXECUTION_COMPLETED in event_types
