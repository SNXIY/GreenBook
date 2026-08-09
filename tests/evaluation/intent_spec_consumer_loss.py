"""Phase 6.9.1 IntentSpec -> TaskIntent consumer loss report.

This report intentionally evaluates the legacy TaskIntent projection and does
not count the raw ``intent_spec`` snapshot as a consumer field.  The snapshot
is lossless, but consumers that read only requirements/resource_requests do
not receive all IntentSpec semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from greenbook_assistant_core.task.intent_compat import to_task_intent
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


@dataclass(frozen=True)
class ConsumerCase:
    case_id: str
    description: str
    spec: IntentSpec


@dataclass
class LossFinding:
    case_id: str
    action_loss: bool = False
    resource_loss: bool = False
    condition_loss: bool = False
    constraint_loss: bool = False
    details: list[str] = field(default_factory=list)


def consumer_cases() -> list[ConsumerCase]:
    """Return representative consumers, without invoking an LLM."""
    return [
        ConsumerCase(
            "search-create",
            "SEARCH + CREATE",
            IntentSpec(
                mode=IntentMode.COMPOSITE,
                goal="Find references and create an article",
                actions=[
                    IntentAction(action=ActionType.SEARCH, resource=ResourceType.POST),
                    IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT),
                ],
            ),
        ),
        ConsumerCase(
            "search-analyze-update",
            "SEARCH + ANALYZE + UPDATE",
            IntentSpec(
                mode=IntentMode.COMPOSITE,
                goal="Research and improve an article",
                actions=[
                    IntentAction(action=ActionType.SEARCH, resource=ResourceType.POST),
                    IntentAction(action=ActionType.ANALYZE, resource=ResourceType.POST),
                    IntentAction(action=ActionType.UPDATE, resource=ResourceType.CONTENT),
                ],
            ),
        ),
        ConsumerCase(
            "conditional-update-or-create",
            "CONDITIONAL UPDATE_OR_CREATE",
            IntentSpec(
                mode=IntentMode.CONDITIONAL,
                goal="Update an existing draft or create one",
                actions=[IntentAction(
                    action=ActionType.UPDATE_OR_CREATE,
                    resource=ResourceType.DRAFT,
                )],
                conditions=[IntentCondition(
                    type="IF_EXISTS",
                    resource=ResourceType.DRAFT,
                    then_action=ActionType.UPDATE,
                    else_action=ActionType.CREATE,
                )],
            ),
        ),
        ConsumerCase(
            "hitl-publish",
            "HITL PUBLISH",
            IntentSpec(
                mode=IntentMode.COMPOSITE,
                goal="Publish after user approval",
                actions=[IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT)],
                constraints=[IntentConstraint(
                    type=ConstraintType.APPROVAL,
                    value="BEFORE_PUBLISH",
                )],
            ),
        ),
    ]


def _requirement_types(task_intent: Any) -> set[str]:
    return {str(item.get("type", "")) for item in task_intent.requirements}


def _resource_pairs(task_intent: Any) -> set[tuple[str, str]]:
    return {
        (str(item.get("operation", "")), str(item.get("resource_type", "")))
        for item in task_intent.resource_requests
    }


def analyze_consumer_loss(case: ConsumerCase) -> LossFinding:
    """Compare IntentSpec semantics with fields available to legacy consumers."""
    task_intent = to_task_intent(case.spec)
    finding = LossFinding(case_id=case.case_id)

    requirement_types = _requirement_types(task_intent)
    expected_actions = {action.action.value for action in case.spec.actions}
    requirement_aliases = {
        ActionType.UPDATE.value: "IMPROVE",
        ActionType.UPDATE_OR_CREATE.value: "CREATE",
    }
    for action in expected_actions:
        represented = requirement_aliases.get(action, action)
        if represented not in requirement_types:
            finding.action_loss = True
            finding.details.append(f"action {action} is absent from requirements")
        elif represented != action:
            finding.action_loss = True
            finding.details.append(f"action {action} is aliased as {represented}")

    expected_resources = {
        (action.action.value, action.resource.value)
        for action in case.spec.actions
        if action.resource is not None
    }
    resource_pairs = _resource_pairs(task_intent)
    resource_aliases = {
        (ActionType.SEARCH.value, ResourceType.POST.value): ("QUERY", "POST"),
        (ActionType.CREATE.value, ResourceType.CONTENT.value): ("CREATE", "CONTENT_DRAFT"),
        (ActionType.UPDATE.value, ResourceType.CONTENT.value): ("UPDATE", "CONTENT_DRAFT"),
        (ActionType.UPDATE_OR_CREATE.value, ResourceType.DRAFT.value): ("CREATE", "CONTENT_DRAFT"),
        (ActionType.PUBLISH.value, ResourceType.CONTENT.value): ("CREATE", "CONTENT_DRAFT"),
    }
    for expected in expected_resources:
        projected = resource_aliases.get(expected)
        if projected is None or projected not in resource_pairs:
            finding.resource_loss = True
            finding.details.append(f"resource mapping missing for {expected[0]}:{expected[1]}")
        elif expected[0] != projected[0] or expected[1] != projected[1]:
            finding.resource_loss = True
            finding.details.append(
                f"resource {expected[0]}:{expected[1]} is projected as "
                f"{projected[0]}:{projected[1]}"
            )

    if case.spec.conditions:
        finding.condition_loss = True
        finding.details.append("TaskIntent has no conditions field; branches survive only in intent_spec")

    expected_constraints = {
        (constraint.type.value, constraint.value)
        for constraint in case.spec.constraints
    }
    projected_constraints = {
        (str(item.get("type", "")), str(item.get("value", "")))
        for item in task_intent.constraints
    }
    missing_constraints = expected_constraints - projected_constraints
    if missing_constraints:
        finding.constraint_loss = True
        finding.details.append(f"constraints missing from TaskIntent: {sorted(missing_constraints)}")

    return finding


def build_loss_report() -> dict[str, Any]:
    findings = [analyze_consumer_loss(case) for case in consumer_cases()]
    return {
        "cases": [finding.__dict__ for finding in findings],
        "summary": {
            "action_loss_cases": sum(item.action_loss for item in findings),
            "resource_loss_cases": sum(item.resource_loss for item in findings),
            "condition_loss_cases": sum(item.condition_loss for item in findings),
            "constraint_loss_cases": sum(item.constraint_loss for item in findings),
            "case_count": len(findings),
        },
        "planner_requirements": {
            "must_preserve": [
                "full action set and per-action resource",
                "conditional branches: condition type, then_action, else_action",
                "constraint semantics, especially APPROVAL and TIME",
            ],
            "compat_sufficient": [
                "goal",
                "target_hint",
                "confidence",
                "simple SEARCH/CREATE/UPDATE requirement aliases when exact action identity is not needed",
            ],
            "note": "TaskIntent.intent_spec is a lossless snapshot, but Planner must read it explicitly for the must-preserve fields.",
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_loss_report(), ensure_ascii=False, indent=2))
