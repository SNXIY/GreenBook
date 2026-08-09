"""Phase 6.8.1 Stage D-B — IntentElements & IntentSpecBuilder tests."""
from __future__ import annotations

import pytest
from greenbook_assistant_core.task.intent_elements import (
    ActionMention,
    ConditionMention,
    IntentElements,
    IntentSpecBuilder,
)
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConditionType,
    ConstraintType,
    IntentMode,
    ResourceType,
)


def _build(elements: IntentElements):
    return IntentSpecBuilder().build(elements)


# ═══════════════════════════════════════════════════════════════════════
# Simple cases
# ═══════════════════════════════════════════════════════════════════════

def test_simple_create() -> None:
    el = IntentElements(
        goal="write Java article",
        action_mentions=[ActionMention(verb="write", object="article")],
    )
    spec = _build(el)
    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions[0].action == ActionType.CREATE
    assert spec.actions[0].resource == ResourceType.CONTENT


def test_simple_search() -> None:
    el = IntentElements(
        goal="search posts",
        action_mentions=[ActionMention(verb="search", object="community posts")],
    )
    spec = _build(el)
    assert spec.actions[0].action == ActionType.SEARCH
    assert spec.actions[0].resource == ResourceType.POST


def test_simple_update_schedule() -> None:
    el = IntentElements(
        goal="update schedule time",
        action_mentions=[ActionMention(verb="update", object="schedule")],
    )
    spec = _build(el)
    assert spec.actions[0].action == ActionType.UPDATE
    assert spec.actions[0].resource == ResourceType.SCHEDULE


def test_simple_delete() -> None:
    el = IntentElements(
        goal="cancel schedule",
        action_mentions=[ActionMention(verb="cancel", object="schedule")],
    )
    spec = _build(el)
    assert spec.actions[0].action == ActionType.DELETE


def test_simple_query_draft() -> None:
    el = IntentElements(
        goal="view drafts",
        action_mentions=[ActionMention(verb="view", object="draft")],
    )
    spec = _build(el)
    assert spec.actions[0].action == ActionType.QUERY
    assert spec.actions[0].resource == ResourceType.DRAFT


# ═══════════════════════════════════════════════════════════════════════
# Composite cases
# ═══════════════════════════════════════════════════════════════════════

def test_composite_search_create() -> None:
    el = IntentElements(
        goal="search and write",
        action_mentions=[
            ActionMention(verb="search", object="posts"),
            ActionMention(verb="write", object="article"),
        ],
    )
    spec = _build(el)
    assert spec.mode == IntentMode.COMPOSITE
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.CREATE}


def test_composite_search_analyze_create() -> None:
    el = IntentElements(
        goal="full pipeline",
        action_mentions=[
            ActionMention(verb="search", object="posts"),
            ActionMention(verb="analyze", object="content"),
            ActionMention(verb="write", object="article"),
        ],
    )
    spec = _build(el)
    assert spec.mode == IntentMode.COMPOSITE
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.ANALYZE, ActionType.CREATE}


# ═══════════════════════════════════════════════════════════════════════
# Conditional cases
# ═══════════════════════════════════════════════════════════════════════

def test_conditional_update_or_create() -> None:
    el = IntentElements(
        goal="update or create",
        action_mentions=[ActionMention(verb="create", object="article")],
        condition_mentions=[
            ConditionMention(text="if draft exists then update else create"),
        ],
    )
    spec = _build(el)
    assert spec.mode == IntentMode.CONDITIONAL
    assert len(spec.conditions) == 1
    assert spec.conditions[0].type == ConditionType.IF_EXISTS


# ═══════════════════════════════════════════════════════════════════════
# Constraint cases
# ═══════════════════════════════════════════════════════════════════════

def test_approval_constraint() -> None:
    el = IntentElements(
        goal="publish with approval",
        action_mentions=[ActionMention(verb="publish", object="content")],
        constraint_mentions=["approval before publish"],
    )
    spec = _build(el)
    assert any(c.type == ConstraintType.APPROVAL for c in spec.constraints)


def test_time_constraint() -> None:
    el = IntentElements(
        goal="publish tomorrow",
        action_mentions=[ActionMention(verb="publish", object="content")],
        constraint_mentions=["tomorrow 9am"],
    )
    spec = _build(el)
    assert any(c.type == ConstraintType.TIME for c in spec.constraints)


def test_both_constraints() -> None:
    el = IntentElements(
        goal="publish with approval and time",
        action_mentions=[ActionMention(verb="publish", object="content")],
        constraint_mentions=["approval before publish", "5 minutes after"],
    )
    spec = _build(el)
    types = {c.type for c in spec.constraints}
    assert types >= {ConstraintType.APPROVAL, ConstraintType.TIME}


# ═══════════════════════════════════════════════════════════════════════
# Complex case
# ═══════════════════════════════════════════════════════════════════════

def test_complex_operator_case() -> None:
    el = IntentElements(
        goal="operate Agent topic",
        action_mentions=[
            ActionMention(verb="search", object="community posts"),
            ActionMention(verb="analyze", object="content"),
            ActionMention(verb="create", object="article"),
            ActionMention(verb="publish", object="content"),
        ],
        condition_mentions=[
            ConditionMention(text="if Agent draft exists then update else create"),
        ],
        constraint_mentions=["approval before publish", "5 minutes after approval"],
        target_hint="Agent draft",
    )
    spec = _build(el)
    assert spec.mode == IntentMode.CONDITIONAL
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.ANALYZE, ActionType.CREATE, ActionType.PUBLISH}
    assert len(spec.conditions) >= 1
    constraint_types = {c.type for c in spec.constraints}
    assert constraint_types >= {ConstraintType.APPROVAL, ConstraintType.TIME}
    assert spec.target_hint == "Agent draft"


# ═══════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════

def test_empty_mentions() -> None:
    el = IntentElements(goal="test", action_mentions=[])
    spec = _build(el)
    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions == []


def test_unknown_verb_skipped() -> None:
    el = IntentElements(
        goal="test",
        action_mentions=[
            ActionMention(verb="xyzzy", object="thing"),
            ActionMention(verb="write", object="article"),
        ],
    )
    spec = _build(el)
    assert len(spec.actions) == 1
    assert spec.actions[0].action == ActionType.CREATE


def test_elements_model_validation() -> None:
    el = IntentElements.model_validate({
        "goal": "test",
        "action_mentions": [{"verb": "search", "object": "posts"}],
        "condition_mentions": [],
        "constraint_mentions": [],
        "target_hint": None,
        "confidence": 0.9,
    })
    assert el.goal == "test"
    assert len(el.action_mentions) == 1
