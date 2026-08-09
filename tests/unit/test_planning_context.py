"""Phase 6.9.2-A PlanningContext tests."""

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.orchestration.context import build_planning_context
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConstraintType,
    IntentAction,
    IntentCondition,
    IntentConstraint,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.models import TaskIntent


def _complex_spec() -> IntentSpec:
    return IntentSpec(
        mode=IntentMode.CONDITIONAL,
        goal="Operate an Agent learning topic",
        actions=[
            IntentAction(action=ActionType.SEARCH, resource=ResourceType.POST),
            IntentAction(action=ActionType.ANALYZE, resource=ResourceType.POST),
            IntentAction(action=ActionType.UPDATE_OR_CREATE, resource=ResourceType.DRAFT),
            IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT),
        ],
        conditions=[IntentCondition(
            type="IF_EXISTS",
            resource=ResourceType.DRAFT,
            then_action=ActionType.UPDATE,
            else_action=ActionType.CREATE,
        )],
        constraints=[
            IntentConstraint(type=ConstraintType.APPROVAL, value="BEFORE_PUBLISH"),
            IntentConstraint(type=ConstraintType.TIME, value="5 minutes later"),
        ],
    )


def test_context_preserves_rich_intent_spec() -> None:
    spec = _complex_spec()
    task_intent = TaskIntent(
        goal=spec.goal,
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "SEARCH"}, {"type": "ANALYZE"}, {"type": "CREATE"}, {"type": "PUBLISH"}],
        intent_spec=spec.model_dump(mode="json"),
    )

    context = build_planning_context(task_intent)

    assert context.intent_spec is not None
    assert [action.action for action in context.actions] == [
        ActionType.SEARCH,
        ActionType.ANALYZE,
        ActionType.UPDATE_OR_CREATE,
        ActionType.PUBLISH,
    ]
    assert [action.resource for action in context.actions] == [
        ResourceType.POST,
        ResourceType.POST,
        ResourceType.DRAFT,
        ResourceType.CONTENT,
    ]
    assert context.conditions[0].then_action == ActionType.UPDATE
    assert context.conditions[0].else_action == ActionType.CREATE
    assert {constraint.type for constraint in context.constraints} == {
        ConstraintType.APPROVAL,
        ConstraintType.TIME,
    }


def test_planner_prefers_intent_spec_for_template_selection() -> None:
    spec = _complex_spec()
    task_intent = TaskIntent(
        goal=spec.goal,
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    context = build_planning_context(task_intent, spec)

    plan = TaskOrchestrator(CapabilityRegistry()).generate_plan(
        task_id="task-69",
        planning_context=context,
    )

    assert plan.template_name == "FULL_PIPELINE"
    assert len(context.actions) == 4
    assert len(context.conditions) == 1
    assert len(context.constraints) == 2
    publish_step = plan.steps[-1]
    assert publish_step.capability == "SCHEDULE_PUBLISH"
    assert publish_step.constraints == {
        "approval": "BEFORE_PUBLISH",
        "time": "5 minutes later",
    }
