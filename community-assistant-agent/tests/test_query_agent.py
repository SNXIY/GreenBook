"""Phase-4 QueryAgent: inventory/status queries without Goal/Task mutation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.query_agent import QueryAgent, QueryCatalog
from app.router import ControlPlaneRouter


ROOT = Path(__file__).resolve().parents[1]
QUERY_AGENT_PATH = ROOT / "app" / "query_agent.py"


def test_query_agent_module_has_no_action_control_plane_imports() -> None:
    tree = ast.parse(QUERY_AGENT_PATH.read_text(encoding="utf-8"))
    banned = {
        "goal_resolver",
        "target_resolver",
        "task_manager",
        "intent_delta",
        "turn_pipeline",
        "plan_compiler",
    }
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    assert banned.isdisjoint(imported)


@pytest.mark.asyncio
async def test_how_many_posts_returns_count_without_creating_goal() -> None:
    agent = QueryAgent()
    calls: list[tuple[str, dict]] = []

    async def execute_tool(name: str, args: dict) -> dict:
        calls.append((name, args))
        return {
            "posts": [
                {"id": "1", "title": "Java"},
                {"id": "2", "title": "Python"},
                {"id": "3", "title": "Go"},
            ],
            "count": 3,
            "truncated": False,
        }

    result = await agent.handle(
        message="我发布多少帖子",
        execute_tool=execute_tool,
    )
    assert result.kind == "OWN_POST_COUNT"
    assert result.data["count"] == 3
    assert result.created_goal is False
    assert result.touched_task is False
    assert result.used_goal_resolver is False
    assert result.used_target_resolver is False
    assert result.created_intent_delta is False
    assert calls == [("community.list_own_posts", {"max_items": 1000})]
    assert "3" in result.answer


@pytest.mark.asyncio
async def test_recent_posts_uses_query_list_path() -> None:
    router = ControlPlaneRouter()
    route = router.classify("最近发布了哪些帖子")
    assert route.mode == "QUERY"

    agent = QueryAgent()

    async def execute_tool(name: str, args: dict) -> dict:
        assert name == "community.list_own_posts"
        assert args["max_items"] == 20
        return {
            "posts": [
                {"id": "11", "title": "Agent 设计", "status": "PUBLISHED"},
                {"id": "12", "title": "Java 并发", "status": "PUBLISHED"},
            ],
            "count": 2,
            "truncated": False,
        }

    result = await agent.handle(
        message="最近发布了哪些帖子",
        route=route,
        execute_tool=execute_tool,
    )
    assert result.kind == "OWN_POST_LIST"
    assert result.data["count"] == 2
    assert len(result.data["posts"]) == 2
    assert "Agent 设计" in result.answer
    assert result.created_goal is False


def test_schedule_status_stays_on_query_path_not_action() -> None:
    router = ControlPlaneRouter()
    route = router.classify("查询定时状态")
    assert route.mode == "QUERY"

    catalog = QueryCatalog()
    spec = catalog.resolve("查询定时状态", route)
    assert spec.kind == "SCHEDULE_STATUS"
    assert spec.tool is None


def test_public_search_does_not_fall_back_to_own_posts() -> None:
    catalog = QueryCatalog()
    spec = catalog.resolve("帮我检索出几篇关于如何学习agent的帖子")
    assert spec.kind == "PUBLIC_POST_SEARCH"
    assert spec.tool == "community.search_posts"
    assert spec.arguments == {"query": "如何学习agent", "limit": 10}


@pytest.mark.asyncio
async def test_public_search_renders_search_results() -> None:
    agent = QueryAgent()

    async def execute_tool(name: str, args: dict) -> dict:
        assert name == "community.search_posts"
        assert args == {"query": "如何学习agent", "limit": 10}
        return {"query": "如何学习agent", "results": [{"id": "p-1", "title": "Agent 学习路线"}]}

    result = await agent.handle(
        message="帮我检索出几篇关于如何学习agent的帖子",
        execute_tool=execute_tool,
    )
    assert result.kind == "PUBLIC_POST_SEARCH"
    assert result.tool_name == "community.search_posts"
    assert "Agent 学习路线" in result.answer


@pytest.mark.asyncio
async def test_schedule_status_answers_without_tools_or_goals() -> None:
    agent = QueryAgent()
    result = await agent.handle(
        message="查询定时状态",
        schedules=[
            {
                "id": "sched-1",
                "status": "SCHEDULED",
                "run_at": "2026-08-04T10:00:00+08:00",
                "draft_id": "draft-1",
            }
        ],
    )
    assert result.kind == "SCHEDULE_STATUS"
    assert result.data["active_count"] == 1
    assert result.tool_name is None
    assert result.created_goal is False
    assert result.touched_task is False
    assert "sched-1" in result.answer
