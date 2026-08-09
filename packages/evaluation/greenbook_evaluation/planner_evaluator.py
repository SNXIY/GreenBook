"""Deterministic quality checks for an existing IntentSpec -> TaskPlan pair."""

from __future__ import annotations

from pydantic import BaseModel, Field

from greenbook_assistant_core.orchestration.models import TaskPlan
from greenbook_assistant_core.task.intent_models import ActionType, IntentSpec, ResourceType


class PlannerEvaluation(BaseModel):
    action_coverage: float = Field(ge=0.0, le=1.0)
    resource_match: bool
    order_reasonable: bool
    constraints_forwarded: bool
    passed: bool
    issues: list[str] = Field(default_factory=list)


class PlannerEvaluator:
    _ACTION_BY_CAPABILITY = {
        "SEARCH_COMMUNITY": ActionType.SEARCH,
        "GET_POST_DETAIL": ActionType.SEARCH,
        "LIST_OWN_POSTS": ActionType.SEARCH,
        "ANALYZE_CONTENT_PATTERNS": ActionType.ANALYZE,
        "ANALYZE_PERFORMANCE": ActionType.ANALYZE,
        "GENERATE_CONTENT": ActionType.CREATE,
        "IMPROVE_CONTENT": ActionType.UPDATE,
        "SCHEDULE_PUBLISH": ActionType.PUBLISH,
        "PUBLISH_NOW": ActionType.PUBLISH,
        "MANAGE_SCHEDULE": ActionType.UPDATE,
        "CANCEL_SCHEDULE": ActionType.DELETE,
    }

    def evaluate(self, intent_spec: IntentSpec, task_plan: TaskPlan) -> PlannerEvaluation:
        observed_actions = [
            self._ACTION_BY_CAPABILITY[step.capability]
            for step in task_plan.steps
            if step.capability in self._ACTION_BY_CAPABILITY
        ]
        expected_actions = [action.action for action in intent_spec.actions]
        covered = sum(
            1 for action in expected_actions
            if action == ActionType.UPDATE_OR_CREATE
            and (ActionType.CREATE in observed_actions or ActionType.UPDATE in observed_actions)
            or action != ActionType.UPDATE_OR_CREATE and action in observed_actions
        )
        action_coverage = covered / len(expected_actions) if expected_actions else 1.0

        resource_match = self._resource_match(intent_spec, task_plan)
        order_reasonable = self._order_reasonable(task_plan)
        constraints_forwarded = self._constraints_forwarded(intent_spec, task_plan)
        issues: list[str] = []
        if action_coverage < 1.0:
            issues.append("ACTION_NOT_COVERED")
        if not resource_match:
            issues.append("RESOURCE_MISMATCH")
        if not order_reasonable:
            issues.append("ORDER_INVALID")
        if not constraints_forwarded:
            issues.append("CONSTRAINT_NOT_FORWARDED")
        return PlannerEvaluation(
            action_coverage=action_coverage,
            resource_match=resource_match,
            order_reasonable=order_reasonable,
            constraints_forwarded=constraints_forwarded,
            passed=not issues,
            issues=issues,
        )

    @staticmethod
    def _resource_match(intent: IntentSpec, plan: TaskPlan) -> bool:
        expected = {action.resource for action in intent.actions if action.resource}
        if not expected:
            return True
        observed: set[ResourceType] = set()
        for step in plan.steps:
            types = set(step.input_artifact_types + [step.output_artifact_type])
            if "DRAFT" in types:
                observed.add(ResourceType.DRAFT)
            if "SCHEDULE" in types:
                observed.add(ResourceType.SCHEDULE)
            if "POST_DETAIL" in types or step.capability in {"SEARCH_COMMUNITY", "GET_POST_DETAIL"}:
                observed.add(ResourceType.CONTENT)
        if ResourceType.CONTENT in expected and (
            ResourceType.DRAFT in observed
            or ResourceType.POST in observed
            or ResourceType.SCHEDULE in observed
        ):
            expected = expected - {ResourceType.CONTENT}
        return expected.issubset(observed)

    @staticmethod
    def _order_reasonable(plan: TaskPlan) -> bool:
        steps = sorted(plan.steps, key=lambda step: step.ordinal)
        position = {step.step_id: index for index, step in enumerate(steps)}
        for step in steps:
            if any(position.get(dep, -1) >= position[step.step_id] for dep in step.depends_on):
                return False
        order = [step.capability for step in steps]
        precedence = {
            "SEARCH_COMMUNITY": {"ANALYZE_CONTENT_PATTERNS", "GENERATE_CONTENT", "IMPROVE_CONTENT"},
            "ANALYZE_CONTENT_PATTERNS": {"GENERATE_CONTENT", "IMPROVE_CONTENT"},
            "GENERATE_CONTENT": {"SCHEDULE_PUBLISH", "PUBLISH_NOW"},
            "IMPROVE_CONTENT": {"SCHEDULE_PUBLISH", "PUBLISH_NOW"},
        }
        for before, afters in precedence.items():
            if before in order:
                before_index = order.index(before)
                if any(order.index(after) < before_index for after in afters if after in order):
                    return False
        return True

    @staticmethod
    def _constraints_forwarded(intent: IntentSpec, plan: TaskPlan) -> bool:
        for constraint in intent.constraints:
            key = constraint.type.value.lower()
            if not any(
                key in step.constraints
                and str(step.constraints[key]) == constraint.value
                for step in plan.steps
            ):
                return False
        return True


__all__ = ["PlannerEvaluation", "PlannerEvaluator"]
