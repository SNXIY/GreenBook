from __future__ import annotations

from typing import Any

import pytest

from app.search_retrieval import build_search_query_plan, search_with_fallback


def test_query_plan_extracts_topic_from_natural_search_request() -> None:
    plan = build_search_query_plan("帮我检索一些如何学好 Agent 的帖子")

    assert plan.candidates[0] == "帮我检索一些如何学好 Agent 的帖子"
    assert "Agent" in plan.candidates
    assert "帖子" not in plan.candidates[1:]


@pytest.mark.parametrize(
    ("query", "topic"),
    [
        ("如何学好agent", "agent"),
        ("帮我找几篇关于 Java 并发的帖子", "Java 并发"),
        ("在社区里搜索如何学习消息队列的文章", "消息队列"),
        ("检索 Redis 缓存三剑客", "Redis 缓存三剑客"),
    ],
)
def test_query_plan_is_topic_agnostic(query: str, topic: str) -> None:
    assert topic in build_search_query_plan(query).candidates


@pytest.mark.asyncio
async def test_empty_precise_query_falls_back_to_topic() -> None:
    calls: list[str] = []

    async def fake_search(query: str, limit: int) -> list[dict[str, Any]]:
        calls.append(query)
        if query.casefold() == "agent":
            return [
                {"id": "post-1", "title": "从零开始学 Agent"},
                {"id": "post-2", "title": "Agent Harness 实战"},
            ][:limit]
        return []

    result = await search_with_fallback("如何学好agent", 10, fake_search)

    assert calls == ["如何学好agent", "agent"]
    assert result.matched_query == "agent"
    assert [item["id"] for item in result.results] == ["post-1", "post-2"]


def test_search_posts_capability_budget_comes_from_definition() -> None:
    """Regression: capability budget must not be hard-coded in Worker."""

    from app.tools import tool_registry
    from app.worker import AgentWorker
    import inspect

    definition = tool_registry.get("community.search_posts")
    assert definition.capability_budget.max_internal_calls == 5
    source = inspect.getsource(AgentWorker._dispatch_builtin_tool)
    assert "community.search_posts" not in source
    assert "max_uses=5" not in source.split("analyze_engagement", 1)[0]
    plan = build_search_query_plan("Agent 设计")
    assert len(plan.candidates) >= 2
    assert len(plan.candidates) <= 5


@pytest.mark.asyncio
async def test_precise_results_do_not_trigger_broader_search() -> None:
    calls: list[str] = []

    async def fake_search(query: str, limit: int) -> list[dict[str, Any]]:
        calls.append(query)
        return [{"id": "post-1", "title": "Redis 缓存三剑客"}]

    result = await search_with_fallback("Redis 缓存三剑客", 5, fake_search)

    assert calls == ["Redis 缓存三剑客"]
    assert result.matched_query == "Redis 缓存三剑客"


@pytest.mark.asyncio
async def test_fallback_deduplicates_and_honors_limit() -> None:
    async def fake_search(query: str, limit: int) -> list[dict[str, Any]]:
        if query == "运维":
            return [
                {"id": "post-1", "title": "运维入门"},
                {"id": "post-1", "title": "运维入门"},
                {"id": "post-2", "title": "自动化运维"},
            ]
        return []

    result = await search_with_fallback("如何学好运维", 1, fake_search)

    assert [item["id"] for item in result.results] == ["post-1"]
