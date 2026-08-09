"""Phase 2 acceptance tests for TaskUnderstanding (L1 path — no LLM needed)."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.task.models import TaskIntent
from greenbook_assistant_core.task.understanding import TaskUnderstanding


def _tu() -> TaskUnderstanding:
    return TaskUnderstanding(llm=None, model="")


def _tasks(*goals: str) -> list[dict[str, str]]:
    return [
        {"task_id": f"task-{i}", "goal": g, "goal_category": "",
         "goal_summary": g[:100]}
        for i, g in enumerate(goals, 1)
    ]


# ── Scenario 1: 创建一篇Java学习文章 ────────────────────────────────

@pytest.mark.asyncio
async def test_create_java_article() -> None:
    intent = await _tu().understand("创建一篇Java学习文章")
    assert intent.relation == "NEW_TASK"
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.source == "L1"


# ── Scenario 2: 修改刚才那篇文章标题 ────────────────────────────────

@pytest.mark.asyncio
async def test_modify_previous_article_title() -> None:
    intent = await _tu().understand(
        "修改刚才那篇文章标题",
        existing_tasks=_tasks("创建一篇Java入门文章"),
    )
    assert intent.relation == "MODIFY_TASK"
    assert intent.goal_category == "IMPROVE_CONTENT"
    assert intent.target_task_hint is not None


# ── Scenario 3: 参考热门Java帖子优化刚才文章 ─────────────────────────

@pytest.mark.asyncio
async def test_improve_with_reference() -> None:
    """L1 now directly handles '优化' as IMPROVE_CONTENT."""
    intent = await _tu().understand(
        "参考热门Java帖子优化刚才文章",
        existing_tasks=_tasks("创建一篇Java入门文章"),
    )
    assert intent.goal_category == "IMPROVE_CONTENT"
    assert intent.relation == "MODIFY_TASK"
    assert intent.source == "L1"


# ── Scenario 4: 搜索社区Java帖子并总结趋势 ──────────────────────────

@pytest.mark.asyncio
async def test_search_and_summarize() -> None:
    intent = await _tu().understand("搜索社区Java帖子并总结趋势")
    assert intent.goal_category in ("ANALYZE_COMMUNITY", "COMPOSITE")
    assert intent.relation == "NEW_TASK"
    assert intent.source == "L1"


# ── Scenario 5: 两个任务交替后修改第一个任务 ────────────────────────

@pytest.mark.asyncio
async def test_two_tasks_alternating() -> None:
    tasks = _tasks(
        "创建一篇Java入门文章",
        "创建一篇Python入门文章",
    )
    intent = await _tu().understand(
        "修改Java文章的标题",
        existing_tasks=tasks,
    )
    assert intent.relation == "MODIFY_TASK"
    assert intent.goal_category == "IMPROVE_CONTENT"
    # L1 should extract "Java文章" as hint
    assert intent.target_task_hint is not None


# ── Edge cases ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simple_greeting_is_direct() -> None:
    intent = await _tu().understand("你好")
    assert intent.relation == "DIRECT"
    assert intent.source == "L1"


@pytest.mark.asyncio
async def test_cancel_schedule() -> None:
    intent = await _tu().understand(
        "取消定时发布",
        existing_tasks=_tasks("创建一篇Java文章"),
    )
    assert intent.relation == "CANCEL_TASK"


@pytest.mark.asyncio
async def test_explicit_search() -> None:
    intent = await _tu().understand("帮我搜索社区中的Python教程")
    assert intent.goal_category == "ANALYZE_COMMUNITY"
    assert intent.relation == "NEW_TASK"


@pytest.mark.asyncio
async def test_create_and_schedule() -> None:
    intent = await _tu().understand("帮我写一篇Java文章，明天上午8点发布")
    assert intent.goal_category == "CREATE_CONTENT"
    assert intent.relation == "NEW_TASK"
    # Should have both CREATE and PUBLISH requirements
    req_types = [r.get("type") for r in intent.requirements]
    assert "CREATE" in req_types
    assert "PUBLISH" in req_types


@pytest.mark.asyncio
async def test_l2_trigger_ambiguous_verb() -> None:
    """L1 should return None for ambiguous-only messages, triggering L2."""
    tu = _tu()
    # Without LLM, "优化一下" → L1 returns DIRECT because no keyword matches
    # But _needs_l2() returns True because "优化" is ambiguous
    assert tu._needs_l2("优化一下刚才的文章") is True


@pytest.mark.asyncio
async def test_l2_trigger_composite() -> None:
    tu = _tu()
    assert tu._needs_l2("搜索Java帖子然后分析趋势然后生成文章") is True


@pytest.mark.asyncio
async def test_l1_no_l2_for_simple() -> None:
    tu = _tu()
    assert tu._needs_l2("创建一篇Java文章") is False
    assert tu._needs_l2("修改标题") is False
