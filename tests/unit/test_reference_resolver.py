"""Phase 6.2.2-A tests for TaskReferenceResolver."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from greenbook_assistant_core.task.models import Task, TaskStatus
from greenbook_assistant_core.task.reference_resolver import (
    ReferenceHint,
    TaskReferenceResolver,
    _parse_ordinal,
)


# ── helpers ──────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _hours_ago_sec(hours: float) -> int:
    return int(hours * 3600)


def _task(
    task_id: str, goal: str,
    category: str = "CREATE_CONTENT",
    created_ago: int = 0,
) -> Task:
    return Task(
        task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
        goal=goal, goal_category=category, status=TaskStatus.COMPLETED,
        created_at=_ago(created_ago),
    )


# ── Case 1: "昨天那个文章" → time-filtered ──────────────────────

def test_parse_hint_time_plus_keyword() -> None:
    resolver = TaskReferenceResolver()
    hint = resolver._parse_hint("昨天那个文章")
    assert hint is not None
    assert hint.time_ref == "昨天"
    assert hint.keyword == "文章"
    assert hint.category_hint == "CREATE_CONTENT"


def test_yesterday_article_matches_correct_task() -> None:
    tasks = [
        _task("t1", "创建Java文章", "CREATE_CONTENT", created_ago=10),    # 10s ago
        _task("t2", "创建Python文章", "CREATE_CONTENT", created_ago=_hours_ago_sec(30)),  # 30h ago = yesterday
    ]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("昨天那个文章", tasks)
    assert result.best_match is not None
    assert result.best_match.task_id == "t2"  # yesterday's task
    assert result.is_ambiguous is False


def test_yesterday_no_match_falls_back() -> None:
    tasks = [_task("t1", "创建Java文章", "CREATE_CONTENT", created_ago=10)]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("昨天那个文章", tasks)
    # No task from yesterday → falls back to keyword match (without time)
    assert result.best_match is not None
    assert result.best_match.task_id == "t1"


# ── Case 2: "第一篇文章" → ordinal ───────────────────────────────

def test_parse_ordinal() -> None:
    assert _parse_ordinal("一") == 1
    assert _parse_ordinal("三") == 3
    assert _parse_ordinal("5") == 5


def test_parse_hint_ordinal() -> None:
    resolver = TaskReferenceResolver()
    hint = resolver._parse_hint("第一篇文章")
    assert hint is not None
    assert hint.ordinal == 1


# ── Case 3: "刚才那个" → ambiguity with multiple recent tasks ───

def test_parse_hint_temporal_only() -> None:
    resolver = TaskReferenceResolver()
    hint = resolver._parse_hint("刚才那个")
    assert hint is not None
    assert hint.time_ref == "刚才"
    assert hint.is_temporal_only is True


def test_recent_ambiguous_with_multiple_tasks() -> None:
    tasks = [
        _task("t1", "创建Java文章", "CREATE_CONTENT", created_ago=10),
        _task("t2", "创建Python文章", "CREATE_CONTENT", created_ago=20),
    ]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("刚才那个", tasks)
    # Both created within 5 min → ambiguity
    assert result.is_ambiguous is True
    assert result.needs_clarification is True
    assert len(result.targets) >= 2


def test_recent_single_task_not_ambiguous() -> None:
    tasks = [_task("t1", "创建Java文章", "CREATE_CONTENT", created_ago=10)]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("刚才那个", tasks)
    assert result.best_match is not None
    assert result.best_match.task_id == "t1"
    assert result.is_ambiguous is False


# ── Case 4: "上周的Java文章" → time + keyword ───────────────────

def test_last_week_java_article() -> None:
    tasks = [
        _task("t1", "创建Java文章", "CREATE_CONTENT", created_ago=10),
        _task("t2", "搜索Spring帖子", "ANALYZE_COMMUNITY", created_ago=_hours_ago_sec(200)),  # ~8 days ago
    ]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("上周的Java文章", tasks)
    # "上周" → 7-14 days → t2 falls in range, but t2 is SEARCH not CREATE
    # Check: does "Java" keyword match t1 (recent) or t2 (last week)?
    # t2.goal = "搜索Spring帖子" → doesn't contain "Java" → keyword doesn't match
    # category_hint = "CREATE_CONTENT" (from "文章" keyword)
    # Time filter returns t2, category filter keeps only CREATE_CONTENT → t2 is SEARCH → removed
    # Fallback: without time filter, keyword "Java" in t1.goal → t1 matches
    # Actually let me trace more carefully:
    # 1. parse: time="上周", keyword="文章", category="CREATE_CONTENT"
    # 2. time filter: t2 (200h ≈ 8.3 days, in 7-14 range), t1 (10s, not in range)
    #    → candidates = [t2]
    # 3. keyword+category filter: t2 category="ANALYZE_COMMUNITY" → doesn't match CREATE_CONTENT
    #    → candidates = []
    # 4. fallback: without time filter → keyword category filter on all tasks
    #    → t1 matches (CREATE_CONTENT), t2 doesn't (SEARCH)
    #    → candidates = [t1] → single match
    # This is correct behavior: "Java" keyword match falls back to t1 when no time match.
    assert result.best_match is not None
    # Falls back because no CREATE_CONTENT task in last week's window
    assert result.best_match.task_id == "t1"


# ── Case 5: "创建Java文章" (no time) → keyword category match ───

def test_no_time_fallback_to_keyword_category() -> None:
    tasks = [
        _task("t1", "创建Java文章", "CREATE_CONTENT"),
        _task("t2", "搜索Java帖子", "ANALYZE_COMMUNITY"),
    ]
    resolver = TaskReferenceResolver()
    result = resolver.resolve("创建Java文章", tasks)
    assert result.best_match is not None
    # "创建" keyword → category_hint="CREATE_CONTENT" → t1 matches
    assert result.best_match.task_id == "t1"


# ── Edge cases ────────────────────────────────────────────────────

def test_empty_hint_returns_empty() -> None:
    resolver = TaskReferenceResolver()
    result = resolver.resolve("", [])
    assert result.best_match is None
    assert not result.targets


def test_empty_tasks_returns_empty() -> None:
    resolver = TaskReferenceResolver()
    result = resolver.resolve("昨天那个文章", [])
    assert result.best_match is None


def test_parse_hint_compound() -> None:
    resolver = TaskReferenceResolver()
    hint = resolver._parse_hint("上一次发布任务")
    assert hint is not None
    assert hint.time_ref == "上一次"
    assert hint.keyword == "发布"
    assert hint.category_hint == "PUBLISH_CONTENT"


def test_parse_hint_just_created() -> None:
    resolver = TaskReferenceResolver()
    hint = resolver._parse_hint("刚才创建的帖子")
    assert hint is not None
    assert hint.time_ref == "刚才"
    assert hint.keyword in ("创建", "帖子")  # dict iteration order
    assert hint.category_hint == "CREATE_CONTENT"
