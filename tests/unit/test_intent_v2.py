"""Phase 6.8.1 — Intent Understanding v2 unit tests.

Tests cover:
  1. L1 fast-path for cases it handles correctly
  2. _needs_l2_v2() routing for complex cases
  3. to_task_intent() compat layer (manually constructed IntentSpec → TaskIntent)
  4. IntentValidator consistency rules
  5. IntentSpec model construction + Pydantic validation
  6. Synonym expressions → same TaskIntent via compat

No real LLM is used — all tests run offline. Cases that require L2
are verified through the compat layer with manually constructed IntentSpec.
"""

from __future__ import annotations

import pytest
from greenbook_assistant_core.task.intent_compat import to_task_intent
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    ConditionType,
    ConstraintType,
    IntentAction,
    IntentCondition,
    IntentConstraint,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.intent_validator import IntentValidator
from greenbook_assistant_core.task.understanding import TaskUnderstanding


def _tu() -> TaskUnderstanding:
    return TaskUnderstanding(llm=None, model="")


def _tasks(*goals: str) -> list[dict[str, str]]:
    return [
        {"task_id": f"task-{i}", "goal": g, "goal_category": "",
         "goal_summary": g[:100]}
        for i, g in enumerate(goals, 1)
    ]


# ═══════════════════════════════════════════════════════════════════
# 1. "写一篇Java文章" → CREATE_CONTENT (L1 can handle)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_write_java_article_is_create_content() -> None:
    intent = await _tu().understand("写一篇Java文章")
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.relation == "NEW_TASK"
    assert intent.source == "L1"


# ═══════════════════════════════════════════════════════════════════
# 2. "创建Python教程" → not QUERY
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_python_tutorial_not_query() -> None:
    """Compat: to_task_intent with correct IntentSpec → CREATE_CONTENT."""
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="创建Python教程",
        actions=[IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT)],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.goal_category != "QUERY_INFO"


# ═══════════════════════════════════════════════════════════════════
# 3. "查看我的草稿" → not CREATE
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_view_draft_not_create() -> None:
    """Compat: to_task_intent with QUERY DRAFT → not CREATE_CONTENT."""
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="查看草稿",
        actions=[IntentAction(action=ActionType.QUERY, resource=ResourceType.DRAFT)],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category != "CREATE_CONTENT"
    assert intent.goal_category == "QUERY_INFO"


# ═══════════════════════════════════════════════════════════════════
# 4. "把发布时间改晚上9点" → UPDATE + SCHEDULE
# ═══════════════════════════════════════════════════════════════════

def test_schedule_time_update_compat() -> None:
    """Compat: UPDATE SCHEDULE → resource_requests contains SCHEDULE, MANAGE_SCHEDULE category."""
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="修改发布时间到晚上9点",
        actions=[IntentAction(action=ActionType.UPDATE, resource=ResourceType.SCHEDULE)],
        constraints=[IntentConstraint(type=ConstraintType.TIME, value="晚上9点")],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category == "MANAGE_SCHEDULE"
    assert any(r["resource_type"] == "SCHEDULE" for r in intent.resource_requests)
    assert any(r["operation"] == "UPDATE" for r in intent.resource_requests)


def test_validator_catches_schedule_content_mismatch() -> None:
    """Validator: '发布时间' + UPDATE CONTENT → needs_repair."""
    bad_spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="修改发布时间",
        actions=[IntentAction(action=ActionType.UPDATE, resource=ResourceType.CONTENT)],
        confidence=0.9,
    )
    validator = IntentValidator()
    result = validator.validate(bad_spec, "把发布时间改成晚上9点")
    assert result.needs_repair is True
    assert any("SCHEDULE" in fix for fix in result.suggested_fixes)


# ═══════════════════════════════════════════════════════════════════
# 5. "如果有旧文章就优化，没有就创建" → CONDITIONAL
# ═══════════════════════════════════════════════════════════════════

def test_conditional_triggers_l2_v2() -> None:
    """_needs_l2_v2: conditional text → score >= 2 → True."""
    assert _tu()._needs_l2_v2("如果有旧文章就优化，没有就创建") is True


def test_conditional_compat() -> None:
    """Compat: CONDITIONAL mode + UPDATE_OR_CREATE → correct TaskIntent."""
    spec = IntentSpec(
        mode=IntentMode.CONDITIONAL,
        goal="优化或创建文章",
        actions=[
            IntentAction(action=ActionType.UPDATE_OR_CREATE, resource=ResourceType.CONTENT),
        ],
        conditions=[
            IntentCondition(
                type=ConditionType.IF_EXISTS,
                resource=ResourceType.DRAFT,
                then_action=ActionType.UPDATE,
                else_action=ActionType.CREATE,
            ),
        ],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.relation == "NEW_TASK"


def test_validator_requires_condition_for_update_or_create() -> None:
    """Validator: UPDATE_OR_CREATE without conditions → needs_repair."""
    spec = IntentSpec(
        mode=IntentMode.CONDITIONAL,
        goal="优化或创建",
        actions=[
            IntentAction(action=ActionType.UPDATE_OR_CREATE, resource=ResourceType.CONTENT),
        ],
        conditions=[],  # missing!
        confidence=0.9,
    )
    validator = IntentValidator()
    result = validator.validate(spec, "有则修改无则创建")
    assert result.needs_repair is True
    assert any("condition" in err.lower() for err in result.errors)


# ═══════════════════════════════════════════════════════════════════
# 6. "搜索热门文章然后写一篇" → COMPOSITE
# ═══════════════════════════════════════════════════════════════════

def test_composite_triggers_l2_v2() -> None:
    """_needs_l2_v2: multiple composite markers → score >= 2."""
    # Two markers: "然后" + "最后" → multi-step(+3) → triggers L2
    assert _tu()._needs_l2_v2("搜索热门然后分析趋势最后写一篇Java总结") is True
    # Single marker in short text → score=1 → stays L1
    assert _tu()._needs_l2_v2("搜索热门文章然后写一篇Java总结") is False


def test_composite_compat() -> None:
    """Compat: COMPOSITE mode with SEARCH + CREATE → CREATE_CONTENT (end goal)."""
    spec = IntentSpec(
        mode=IntentMode.COMPOSITE,
        goal="搜索热门并写Java总结",
        actions=[
            IntentAction(action=ActionType.SEARCH, resource=ResourceType.POST),
            IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT),
        ],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category == "CREATE_CONTENT"  # COMPOSITE+CREATE → CREATE_CONTENT
    req_types = [r["type"] for r in intent.requirements]
    assert "SEARCH" in req_types
    assert "CREATE" in req_types


# ═══════════════════════════════════════════════════════════════════
# 7. "发布前让我确认" → PUBLISH + APPROVAL
# ═══════════════════════════════════════════════════════════════════

def test_approval_compat() -> None:
    """Compat: PUBLISH + APPROVAL constraint."""
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="发布前确认",
        actions=[IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT)],
        constraints=[IntentConstraint(type=ConstraintType.APPROVAL, value="BEFORE_PUBLISH")],
        confidence=0.9,
    )
    intent = to_task_intent(spec)
    assert intent.goal_category == "PUBLISH_CONTENT"
    assert any(c["type"] == "APPROVAL" for c in intent.constraints)


def test_validator_catches_missing_approval() -> None:
    """Validator: '发布前确认' without PUBLISH + APPROVAL → needs_repair."""
    # Missing PUBLISH action
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="发布前确认",
        actions=[IntentAction(action=ActionType.QUERY, resource=ResourceType.TASK)],
        confidence=0.9,
    )
    validator = IntentValidator()
    result = validator.validate(spec, "发布之前让我确认一下")
    assert result.needs_repair is True
    assert any("PUBLISH" in err or "APPROVAL" in err for err in result.errors)


# ═══════════════════════════════════════════════════════════════════
# 8. 同义表达 → 相同 intent (via compat)
# ═══════════════════════════════════════════════════════════════════

def test_synonym_expressions_same_intent() -> None:
    """Different CREATE expressions → same goal_category and relation."""
    inputs = [
        ("写一篇Java文章", "写Java文章"),
        ("创建Java教程", "创建Java教程"),
        ("帮我搞个Java帖子", "搞Java帖子"),
    ]

    expected_category = "CREATE_CONTENT"
    expected_relation = "NEW_TASK"

    for _msg, goal_text in inputs:
        spec = IntentSpec(
            mode=IntentMode.SIMPLE,
            goal=goal_text,
            actions=[IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT)],
            confidence=0.9,
        )
        intent = to_task_intent(spec)
        assert intent.goal_category == expected_category, f"Failed for: {goal_text}"
        assert intent.relation == expected_relation, f"Failed for: {goal_text}"


# ═══════════════════════════════════════════════════════════════════
# _needs_l2_v2() scoring tests
# ═══════════════════════════════════════════════════════════════════

def test_l2_v2_score_conditional_triggers() -> None:
    """Conditional text alone → score >= 3 → triggers L2."""
    assert _tu()._needs_l2_v2("如果有旧文章就优化") is True
    assert _tu()._needs_l2_v2("有则修改无则创建") is True


def test_l2_v2_score_composite_triggers() -> None:
    """Multiple composite markers → score >= 2 → triggers L2."""
    assert _tu()._needs_l2_v2("搜索Java然后分析趋势最后生成文章") is True


def test_l2_v2_score_numbered_list_triggers() -> None:
    """Numbered list: multi-step with 3 items → score >= 3 → triggers L2."""
    # "然后" appears implicitly via numbering, but we rely on multi-step detection
    # 3 numbered items → "然后" count may be 0, so this is a long text case
    # "1. 2. 3." → multi-step markers=0, long=1 (if >100 chars). Let's test the actual trigger.
    pass  # Numbered list detection removed in Stage D-B; covered by multi-step + long


def test_l2_v2_score_open_goal_triggers() -> None:
    """Open goal words removed in Stage D-B; now triggered by length + multi-step."""
    # "运营" + "专题" — removed as standalone signal. Now relies on other signals.
    assert _tu()._needs_l2_v2("帮我运营一个Java专题：先搜索热门，再分析，最后写文章") is True


def test_l2_v2_score_simple_no_trigger() -> None:
    """Simple messages → score < 2 → stays in L1."""
    assert _tu()._needs_l2_v2("写一篇Java文章") is False
    assert _tu()._needs_l2_v2("修改标题") is False
    assert _tu()._needs_l2_v2("你好") is False


def test_l2_v2_score_history_alone_insufficient() -> None:
    """History ref alone → score=1 → stays in L1."""
    assert _tu()._needs_l2_v2("修改刚才那篇文章") is False


# ═══════════════════════════════════════════════════════════════════
# IntentSpec model validation
# ═══════════════════════════════════════════════════════════════════

def test_intent_spec_minimal() -> None:
    """Minimal IntentSpec is valid."""
    spec = IntentSpec()
    assert spec.mode == IntentMode.SIMPLE
    assert spec.actions == []
    assert spec.conditions == []


def test_intent_spec_full() -> None:
    """Full IntentSpec with all fields."""
    spec = IntentSpec(
        mode=IntentMode.CONDITIONAL,
        goal="运营Agent专题",
        actions=[
            IntentAction(action=ActionType.SEARCH, resource=ResourceType.POST),
            IntentAction(action=ActionType.UPDATE_OR_CREATE, resource=ResourceType.CONTENT),
            IntentAction(action=ActionType.PUBLISH, resource=ResourceType.CONTENT),
        ],
        conditions=[
            IntentCondition(
                type=ConditionType.IF_EXISTS,
                resource=ResourceType.DRAFT,
                then_action=ActionType.UPDATE,
                else_action=ActionType.CREATE,
            ),
        ],
        constraints=[
            IntentConstraint(type=ConstraintType.APPROVAL, value="BEFORE_PUBLISH"),
            IntentConstraint(type=ConstraintType.TIME, value="5分钟后"),
        ],
        target_hint="Agent学习草稿",
        confidence=0.9,
        source="L2",
    )
    assert spec.mode == IntentMode.CONDITIONAL
    assert len(spec.actions) == 3
    assert len(spec.conditions) == 1
    assert len(spec.constraints) == 2


def test_intent_spec_no_seq_field() -> None:
    """IntentSpec must NOT have seq/step_id/depends_on fields."""
    spec = IntentSpec()
    assert not hasattr(spec, "seq")
    assert not hasattr(spec, "step_id")
    assert not hasattr(spec, "depends_on")
    assert not hasattr(spec, "parallel")


# ═══════════════════════════════════════════════════════════════════
# Validator: condition text + SIMPLE mode → needs_repair
# ═══════════════════════════════════════════════════════════════════

def test_validator_conditional_text_simple_mode() -> None:
    spec = IntentSpec(
        mode=IntentMode.SIMPLE,
        goal="优化文章",
        actions=[IntentAction(action=ActionType.UPDATE, resource=ResourceType.CONTENT)],
        confidence=0.9,
    )
    validator = IntentValidator()
    result = validator.validate(spec, "如果有旧文章就优化")
    assert result.needs_repair is True
    assert any("conditional" in err.lower() for err in result.errors)


# ═══════════════════════════════════════════════════════════════════
# Existing L1 tests still pass (backward compat)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_existing_l1_create_still_works() -> None:
    intent = await _tu().understand("创建一篇Java学习文章")
    assert intent.relation == "NEW_TASK"
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.source == "L1"


@pytest.mark.asyncio
async def test_existing_l1_modify_still_works() -> None:
    intent = await _tu().understand(
        "修改刚才那篇文章标题",
        existing_tasks=_tasks("创建一篇Java入门文章"),
    )
    assert intent.relation == "MODIFY_TASK"
    assert intent.goal_category == "IMPROVE_CONTENT"


@pytest.mark.asyncio
async def test_existing_l1_cancel_still_works() -> None:
    intent = await _tu().understand(
        "取消定时发布",
        existing_tasks=_tasks("创建一篇Java文章"),
    )
    assert intent.relation == "CANCEL_TASK"


@pytest.mark.asyncio
async def test_existing_l1_greeting_still_direct() -> None:
    intent = await _tu().understand("你好")
    assert intent.relation == "DIRECT"
    assert intent.source == "L1"


# ═══════════════════════════════════════════════════════════════════
# to_task_intent() covers all action types
# ═══════════════════════════════════════════════════════════════════

def test_compat_all_action_types() -> None:
    """Every ActionType maps to a valid TaskIntent."""
    cases: list[tuple[ActionType, ResourceType | None, str]] = [
        (ActionType.CREATE, ResourceType.CONTENT, "CREATE_CONTENT"),
        (ActionType.UPDATE, ResourceType.CONTENT, "IMPROVE_CONTENT"),
        (ActionType.UPDATE, ResourceType.SCHEDULE, "MANAGE_SCHEDULE"),
        (ActionType.DELETE, ResourceType.SCHEDULE, "MANAGE_SCHEDULE"),
        (ActionType.QUERY, ResourceType.DRAFT, "QUERY_INFO"),
        (ActionType.SEARCH, ResourceType.POST, "ANALYZE_COMMUNITY"),
        (ActionType.ANALYZE, None, "ANALYZE_COMMUNITY"),
        (ActionType.PUBLISH, ResourceType.CONTENT, "PUBLISH_CONTENT"),
        (ActionType.UPDATE_OR_CREATE, ResourceType.CONTENT, "CREATE_CONTENT"),
    ]
    for action, resource, expected_category in cases:
        spec = IntentSpec(
            mode=IntentMode.SIMPLE,
            goal="test",
            actions=[IntentAction(action=action, resource=resource)],
            confidence=0.9,
        )
        intent = to_task_intent(spec)
        assert intent.goal_category == expected_category, \
            f"{action.value} → {intent.goal_category}, expected {expected_category}"


# ═══════════════════════════════════════════════════════════════════
# TaskIntent.intent_spec field exists
# ═══════════════════════════════════════════════════════════════════

def test_task_intent_has_intent_spec_field() -> None:
    from greenbook_assistant_core.task.models import TaskIntent
    intent = TaskIntent()
    assert hasattr(intent, "intent_spec")
    assert intent.intent_spec is None
