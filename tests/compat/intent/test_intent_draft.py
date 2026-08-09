"""Phase 6.8.1 Stage D-A — IntentDraft & IntentCompiler tests."""
from __future__ import annotations

import pytest
from greenbook_assistant_core.task.intent_draft import IntentCompiler, IntentDraft
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConditionType,
    ConstraintType,
    IntentMode,
    ResourceType,
)


def _compile(draft: IntentDraft):
    return IntentCompiler().compile(draft)


# ═══════════════════════════════════════════════════════════════════════
# Simple cases
# ═══════════════════════════════════════════════════════════════════════

def test_simple_create() -> None:
    draft = IntentDraft(
        goal="写Java文章",
        actions=["create content about Java"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.SIMPLE
    assert len(spec.actions) == 1
    assert spec.actions[0].action == ActionType.CREATE
    assert spec.actions[0].resource == ResourceType.CONTENT


def test_simple_search() -> None:
    draft = IntentDraft(goal="搜索帖子", actions=["search for posts"])
    spec = _compile(draft)
    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions[0].action == ActionType.SEARCH


def test_simple_publish() -> None:
    draft = IntentDraft(goal="发布文章", actions=["publish content"])
    spec = _compile(draft)
    assert spec.actions[0].action == ActionType.PUBLISH


def test_simple_update() -> None:
    draft = IntentDraft(goal="修改标题", actions=["update the content"])
    spec = _compile(draft)
    assert spec.actions[0].action == ActionType.UPDATE


def test_simple_delete() -> None:
    draft = IntentDraft(goal="取消定时", actions=["cancel schedule"])
    spec = _compile(draft)
    assert spec.actions[0].action == ActionType.DELETE


# ═══════════════════════════════════════════════════════════════════════
# Composite cases
# ═══════════════════════════════════════════════════════════════════════

def test_composite_search_create() -> None:
    draft = IntentDraft(
        goal="搜索并写文章",
        actions=["search for popular posts", "create content about Java"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.COMPOSITE
    action_types = {a.action for a in spec.actions}
    assert ActionType.SEARCH in action_types
    assert ActionType.CREATE in action_types


def test_composite_search_analyze_create() -> None:
    draft = IntentDraft(
        goal="搜索分析创建",
        actions=["search for posts", "analyze trends", "create content"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.COMPOSITE
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.ANALYZE, ActionType.CREATE}


# ═══════════════════════════════════════════════════════════════════════
# Conditional cases
# ═══════════════════════════════════════════════════════════════════════

def test_conditional_update_or_create() -> None:
    draft = IntentDraft(
        goal="有则改无则建",
        actions=["create or update content"],
        conditions=["if draft exists update else create"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.CONDITIONAL
    assert len(spec.conditions) == 1
    assert spec.conditions[0].type == ConditionType.IF_EXISTS
    # The compiler extracts then/else actions from the condition text
    assert spec.conditions[0].then_action is not None
    assert spec.conditions[0].else_action is not None


def test_conditional_chinese() -> None:
    draft = IntentDraft(
        goal="运营专题",
        actions=["search for content", "create or update draft"],
        conditions=["if draft exists update it else create new one"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.CONDITIONAL
    assert len(spec.conditions) == 1


# ═══════════════════════════════════════════════════════════════════════
# Constraint cases
# ═══════════════════════════════════════════════════════════════════════

def test_approval_constraint() -> None:
    draft = IntentDraft(
        goal="发布前确认",
        actions=["publish content"],
        constraints=["approve before publish"],
    )
    spec = _compile(draft)
    assert any(c.type == ConstraintType.APPROVAL for c in spec.constraints)


def test_time_constraint() -> None:
    draft = IntentDraft(
        goal="定时发布",
        actions=["publish content"],
        constraints=["tomorrow 9am"],
    )
    spec = _compile(draft)
    assert any(c.type == ConstraintType.TIME for c in spec.constraints)


def test_both_constraints() -> None:
    draft = IntentDraft(
        goal="确认后定时发布",
        actions=["publish content"],
        constraints=["approve before publish", "5 minutes after approval"],
    )
    spec = _compile(draft)
    types = {c.type for c in spec.constraints}
    assert types >= {ConstraintType.APPROVAL, ConstraintType.TIME}


# ═══════════════════════════════════════════════════════════════════════
# Complex case (the one that failed before)
# ═══════════════════════════════════════════════════════════════════════

def test_complex_full_pipeline() -> None:
    """Simulate the complex operator case that failed with direct IntentSpec."""
    draft = IntentDraft(
        goal="运营Agent学习专题",
        actions=[
            "search for popular Agent content",
            "analyze trends",
            "create or update Agent learning draft",
        ],
        conditions=["if Agent draft exists update it else create new one"],
        constraints=["approve before publish", "publish 5 minutes after approval"],
        target_hint="Agent学习草稿",
        confidence=0.9,
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.CONDITIONAL
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.ANALYZE}
    assert len(spec.conditions) >= 1
    constraint_types = {c.type for c in spec.constraints}
    assert constraint_types >= {ConstraintType.APPROVAL, ConstraintType.TIME}
    assert spec.target_hint == "Agent学习草稿"


def test_complex_search_conditional_publish() -> None:
    draft = IntentDraft(
        goal="搜索并发相关内容",
        actions=["search for Java concurrency posts", "create or update draft", "publish"],
        conditions=["if existing draft found update else create"],
        constraints=["approve before publish"],
    )
    spec = _compile(draft)
    assert spec.mode == IntentMode.CONDITIONAL
    action_types = {a.action for a in spec.actions}
    assert action_types >= {ActionType.SEARCH, ActionType.PUBLISH}


# ═══════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════

def test_empty_actions_produces_empty_spec() -> None:
    draft = IntentDraft(goal="test", actions=[])
    spec = _compile(draft)
    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions == []


def test_draft_model_validation() -> None:
    draft = IntentDraft.model_validate({
        "goal": "test", "actions": ["do something"],
        "conditions": [], "constraints": [], "target_hint": None, "confidence": 0.9,
    })
    assert draft.goal == "test"
    assert draft.actions == ["do something"]


def test_unknown_action_text_still_handled() -> None:
    """Unknown action text should not crash — just skip it."""
    draft = IntentDraft(
        goal="unknown",
        actions=["xyzzy unknown verb", "create content"],
    )
    spec = _compile(draft)
    # Should have at least the CREATE action
    assert any(a.action == ActionType.CREATE for a in spec.actions)
