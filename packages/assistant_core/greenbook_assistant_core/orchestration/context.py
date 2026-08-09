"""Planner input adapter carrying legacy and richer intent representations."""

from __future__ import annotations

from pydantic import BaseModel

from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.models import TaskIntent


class PlanningContext(BaseModel):
    """Context passed into planning without changing TaskIntent semantics."""

    task_intent: TaskIntent
    intent_spec: IntentSpec | None = None

    @property
    def actions(self):
        return list(self.intent_spec.actions) if self.intent_spec else []

    @property
    def resources(self):
        return [action.resource for action in self.actions if action.resource is not None]

    @property
    def conditions(self):
        return list(self.intent_spec.conditions) if self.intent_spec else []

    @property
    def constraints(self):
        return list(self.intent_spec.constraints) if self.intent_spec else [
            item for item in self.task_intent.constraints
        ]


def build_planning_context(
    task_intent: TaskIntent,
    intent_spec: IntentSpec | None = None,
) -> PlanningContext:
    """Build a context, recovering the lossless snapshot when available."""
    if intent_spec is None and task_intent.intent_spec:
        try:
            intent_spec = IntentSpec.model_validate(task_intent.intent_spec)
        except Exception:
            intent_spec = None
    return PlanningContext(task_intent=task_intent, intent_spec=intent_spec)
