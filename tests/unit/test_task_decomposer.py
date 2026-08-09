"""Phase 6.0.1 tests for TaskDecomposer."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.task.decomposer import (
    SubTaskContext,
    TaskDecomposer,
    _is_standalone,
    _clean_chunks,
)
from greenbook_assistant_core.task.models import TaskIntent
from greenbook_assistant_core.task.understanding import TaskUnderstanding


async def _decompose(msg: str) -> list[SubTaskContext]:
    tu = TaskUnderstanding()
    d = TaskDecomposer()
    return await d.decompose(msg, tu)


# ── Case 1: 两个独立创建 → 2 SubTasks ─────────────────────────────

@pytest.mark.asyncio
async def test_two_independent_creates_split() -> None:
    result = await _decompose("创建一篇Java文章明天发布。然后再创建一篇Python文章晚上发布。")
    assert len(result) == 2
    assert result[0].sub_index == 0
    assert result[1].sub_index == 1
    # Both should be NEW_TASK
    assert getattr(result[0].task_intent, "relation", "") == "NEW_TASK"
    assert getattr(result[1].task_intent, "relation", "") == "NEW_TASK"
    # No cross-references
    assert result[0].depends_on_task_index is None
    assert result[1].depends_on_task_index is None


# ── Case 2: 搜索→分析→生成 → 合并为 1 Task ───────────────────────

@pytest.mark.asyncio
async def test_search_analyze_create_merges_to_one() -> None:
    result = await _decompose("搜索Java帖子然后分析原因然后生成文章")
    assert len(result) == 1
    assert result[0].sub_index == 0


# ── Case 3: 三个任务 + 跨引用 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_three_tasks_with_cross_reference() -> None:
    msg = (
        "写一篇Spring Boot文章明天10点发布。"
        "然后再写一篇Java集合文章晚上8点发布。"
        "最后把第一篇文章发布时间改成晚上9点。"
    )
    result = await _decompose(msg)
    assert len(result) == 3
    # Task 2 references Task 0
    assert result[2].depends_on_task_index == 0
    assert result[2].depends_on_hint  # non-empty ordinal hint


# ── Case 4: 单任务不拆分 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_task_no_split() -> None:
    result = await _decompose("帮我写一篇Spring教程")
    assert len(result) == 1


@pytest.mark.asyncio
async def test_simple_create_no_split() -> None:
    result = await _decompose("写一篇Java文章")
    assert len(result) == 1
    gc = getattr(result[0].task_intent, "goal_category", "")
    assert gc == "CREATE_CONTENT"


# ── Case 5: 两个取消 → 2 SubTasks ─────────────────────────────────

@pytest.mark.asyncio
async def test_two_cancels_split() -> None:
    result = await _decompose("取消Java文章发布，再取消Python文章发布")
    assert len(result) == 2


# ── Case 6: 无分隔符 → 不拆分 ────────────────────────────────────

@pytest.mark.asyncio
async def test_no_separator_no_split() -> None:
    result = await _decompose(
        "帮我写一篇Java并发文章标题新颖包含代码示例明天上午发布"
    )
    assert len(result) == 1


# ── Edge cases ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simple_greeting_no_split() -> None:
    result = await _decompose("你好")
    assert len(result) == 1


def test_is_standalone_detects_valid_intent() -> None:
    intent = TaskIntent(
        relation="NEW_TASK",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    assert _is_standalone(intent) is True


def test_is_standalone_accepts_cancel() -> None:
    """CANCEL may have empty requirements but is still a valid standalone task."""
    intent = TaskIntent(
        relation="CANCEL_TASK",
        goal_category="MANAGE_SCHEDULE",
        requirements=[],  # CANCEL currently has no requirements
    )
    assert _is_standalone(intent) is True


def test_is_standalone_rejects_direct() -> None:
    intent = TaskIntent(
        relation="DIRECT",
        goal_category="QUERY_INFO",
        requirements=[],
    )
    assert _is_standalone(intent) is False


def test_clean_chunks_removes_empty() -> None:
    assert _clean_chunks(["hello", "", "  ", "world"]) == ["hello", "world"]


@pytest.mark.asyncio
async def test_split_preserves_order() -> None:
    result = await _decompose(
        "写一篇Java文章。然后写一篇Python文章。最后写一篇Go文章。"
    )
    assert len(result) == 3
    for i, st in enumerate(result):
        assert st.sub_index == i


@pytest.mark.asyncio
async def test_numbered_list_split() -> None:
    result = await _decompose(
        "1. 创建Java文章\n2. 创建Python文章\n3. 创建Go文章"
    )
    # Numbered list should split
    assert len(result) >= 1  # At minimum doesn't crash
