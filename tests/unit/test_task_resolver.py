"""Phase 2.5 acceptance tests for TaskResolver."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    ResolvedTaskTarget,
    Task,
    TaskIntent,
    TaskStatus,
)
from greenbook_assistant_core.task.resolver import TaskResolver, resolve_target


def _java_task(task_id: str = "task-1", goal: str = "创建一篇Java入门文章") -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="t1",
        goal=goal,
        goal_category="CREATE_CONTENT",
        status=TaskStatus.COMPLETED,
        artifacts=[
            ArtifactRef(
                artifact_id="art-1",
                task_id=task_id,
                artifact_type="DRAFT",
                resource_id="draft-1",
                resource_kind="DRAFT",
                summary="Java入门文章草稿",
            )
        ],
    )


def _python_task(task_id: str = "task-2", goal: str = "创建一篇Python入门文章") -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="t1",
        goal=goal,
        goal_category="CREATE_CONTENT",
        status=TaskStatus.COMPLETED,
        artifacts=[],
    )


# ── Scenario 1: 创建Java文章 → 修改刚才文章 ─────────────────────────

def test_modify_recent_by_label() -> None:
    """'修改刚才Java文章' — label match on 'Java文章'."""
    tasks = [_java_task("task-1")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="Java文章",
        goal="修改Java文章标题",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-1"
    assert result.match_level == 2
    assert "label" in result.match_reason
    assert result.confidence >= 0.70


def test_modify_recent_by_temporal_only() -> None:
    """'修改刚才那篇' — purely temporal, no content match → recency."""
    tasks = [_java_task("task-1"), _python_task("task-2")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="刚才那篇",
        goal="修改刚才那篇文章",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-1"  # newest first
    # Temporal → skips label/artifact → category or recent fallback
    assert result.match_level in (4, 5)


# ── Scenario 2: 创建Java + Python → 修改Java文章 ────────────────────

def test_two_tasks_pick_java_by_label() -> None:
    """Two tasks, '修改Java文章' picks the Java one by label match."""
    tasks = [_python_task("task-2"), _java_task("task-1")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="Java文章",
        goal="修改Java文章标题",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-1"
    assert result.match_level == 2
    assert "label" in result.match_reason


# ── Scenario 3: 两个任务交替后引用第一个 ────────────────────────────

def test_alternating_tasks_reference_first() -> None:
    """Task B created after Task A; '修改第一个任务' matches Task A by label."""
    tasks = [
        _python_task("task-2"),  # newest
        _java_task("task-1"),    # older
    ]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="Java",  # partial label
        goal="修改Java文章",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-1"  # label match wins over recency
    assert result.match_level == 2


# ── Scenario 4: 模糊引用'刚才那个' → 低confidence ───────────────────

def test_ambiguous_recent_with_low_confidence() -> None:
    """'刚才那个' with no content → low confidence fallback."""
    tasks = [_python_task("task-2"), _java_task("task-1")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="刚才那个",
        goal="修改刚才那个",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-2"  # newest
    assert result.match_level in (4, 5)
    assert result.confidence <= 0.50  # low confidence


# ── Edge cases ──────────────────────────────────────────────────────

def test_exact_id_shortcuts() -> None:
    """Explicit task_id → highest confidence."""
    tasks = [_java_task("task-1"), _python_task("task-2")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_id="task-2",
        target_task_hint="whatever",
        goal="...",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-2"
    assert result.match_level == 1
    assert result.confidence == 1.0


def test_no_tasks_returns_none() -> None:
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="Java",
    )
    resolver = TaskResolver()
    assert resolver.resolve(intent, []) is None


def test_new_task_skips_resolution() -> None:
    """NEW_TASK intents should not resolve a target."""
    intent = TaskIntent(
        relation="NEW_TASK",
        goal_category="CREATE_CONTENT",
        goal="创建新文章",
    )
    tasks = [_java_task("task-1")]
    result = resolve_target(intent, tasks)
    assert result.target_task_id is None  # NEW_TASK, no target needed


def test_artifact_match() -> None:
    """Hint matches artifact summary."""
    tasks = [
        Task(
            task_id="task-1",
            conversation_id="conv-1",
            user_id="u1",
            tenant_id="t1",
            goal="分析社区Java帖子",
            goal_category="ANALYZE_COMMUNITY",
            status=TaskStatus.COMPLETED,
            artifacts=[
                ArtifactRef(
                    artifact_id="art-1",
                    task_id="task-1",
                    artifact_type="SEARCH_RESULT",
                    resource_kind="POST",
                    summary="热门Java帖子搜索结果",
                )
            ],
        )
    ]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="搜索结果",
        goal="把搜索结果加入文章",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert result.task_id == "task-1"
    assert result.match_level == 3
    assert result.match_reason == "artifact_match"


def test_multiple_label_matches_returns_candidates() -> None:
    """Two tasks both matching '文章' → best returned, others as candidates."""
    tasks = [
        _python_task("task-2", "创建Python文章"),
        _java_task("task-1", "创建Java文章"),
    ]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="文章",  # matches both
        goal="修改文章",
    )

    resolver = TaskResolver()
    result = resolver.resolve(intent, tasks)

    assert result is not None
    assert "ambiguous" in result.match_reason or "label" in result.match_reason
    assert len(result.candidates) >= 1
    assert result.confidence < 0.85  # lower because ambiguous


def test_resolve_target_mutates_intent() -> None:
    """Integration helper fills target_task_id."""
    tasks = [_java_task("task-1")]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="Java",
        goal="修改Java文章",
    )
    result = resolve_target(intent, tasks)
    assert result.target_task_id == "task-1"
