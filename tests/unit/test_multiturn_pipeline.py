"""Phase 5.3: Multi-turn pipeline tests — create → modify → improve."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.task.models import TaskIntent
from greenbook_assistant_core.task.understanding import TaskUnderstanding


def _mock_mcp(responses: dict[str, dict[str, Any]]) -> AsyncMock:
    mcp = AsyncMock()

    async def execute_tool(tool_name: str, **kw: Any) -> dict[str, Any]:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL",
                "message": f"Unexpected: {tool_name}"}

    mcp.execute_tool = execute_tool
    return mcp


def _ctx(**kw: Any) -> RuntimeContext:
    intent = type("_Intent", (), {
        "goal_category": kw.pop("goal_category", "CREATE_CONTENT"),
        "relation": kw.pop("relation", "NEW_TASK"),
        "requirements": kw.pop("requirements", [{"type": "CREATE"}]),
        "target_task_id": kw.pop("target_task_id", None),
        "target_task_hint": kw.pop("target_task_hint", None),
    })()
    return RuntimeContext(
        run_id=kw.pop("run_id", "run-1"),
        trace_id=kw.pop("trace_id", "trace-1"),
        task_id=kw.pop("task_id", "task-a"),
        user_id="u1",
        task_intent=intent,
        user_message=kw.pop("user_message", "写一篇Java文章"),
        mcp=kw.pop("mcp", _mock_mcp({
            "content.create_draft": {
                "ok": True, "code": "",
                "data": {"draft_id": "draft-a", "title": "Java Guide"},
            },
        })),
        session=None,
        **kw,
    )


# ── Round 1: CREATE_CONTENT → Task A + DRAFT ──────────────────────

@pytest.mark.asyncio
async def test_round1_create_content_creates_task_and_draft() -> None:
    service = RuntimeAgentService()
    result = await service.execute(_ctx(
        user_message="帮我写一篇Java学习文章",
        goal_category="CREATE_CONTENT",
        relation="NEW_TASK",
        requirements=[{"type": "CREATE"}],
    ))
    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.draft_id == "draft-a"
    assert len(result.artifact_ids) >= 1


# ── Round 2: MODIFY_TASK → find Task A → revises existing draft ───

@pytest.mark.asyncio
async def test_round2_modify_existing_draft() -> None:
    """修改已有文章 → IMPROVE_CONTENT → content.revise_draft."""

    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-a", "title": "Java Guide Revised",
                     "status": "DRAFT"},
        },
    })
    service = RuntimeAgentService()
    result = await service.execute(_ctx(
        run_id="run-2", trace_id="trace-2",
        user_message="修改刚才那篇文章，增加几个代码示例",
        goal_category="IMPROVE_CONTENT",
        relation="MODIFY_TASK",
        task_id="task-a",
        target_task_id="task-a",
        requirements=[{"type": "IMPROVE"}],
        mcp=mcp,
    ))
    assert result.success is True
    assert result.draft_id == "draft-a"  # same draft, revised


# ── Round 3: IMPROVE_WITH_RESEARCH → SEARCH → ANALYZE → REVISE ────

@pytest.mark.asyncio
async def test_round3_improve_with_research() -> None:
    """参考社区帖子→优化文章 → SEARCH→ANALYZE→IMPROVE multi-step."""

    draft_id = "draft-a"
    mcp = _mock_mcp({
        "community.search_public_posts": {
            "ok": True, "code": "",
            "data": {"items": [{"post_id": "p1", "title": "Java Hot"}], "total": 1},
        },
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": draft_id, "title": "Java Guide Optimized",
                     "status": "DRAFT"},
        },
    })
    service = RuntimeAgentService()
    result = await service.execute(_ctx(
        run_id="run-3", trace_id="trace-3",
        user_message="参考社区热门Java帖子优化刚才文章",
        goal_category="IMPROVE_CONTENT",
        relation="MODIFY_TASK",
        task_id="task-a",
        target_task_id="task-a",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "IMPROVE"},
        ],
        mcp=mcp,
    ))
    assert result.success is True
    assert result.draft_id == draft_id
    # Should have SEARCH_RESULT + ANALYSIS_REPORT + DRAFT artifacts
    assert len(result.artifact_ids) >= 2

    event_types = {e["event"] for e in result.events}
    assert "TOOL_INVOKED" in event_types


# ── Semantic equivalence: different expressions → same intent ─────

@pytest.mark.asyncio
async def test_semantic_equivalence_all_improve() -> None:
    """'完善/打磨/优化/提升' all → IMPROVE_CONTENT."""
    tu = TaskUnderstanding()

    cases = [
        "完善一下这篇文章",
        "重新打磨这篇文章",
        "优化一下这篇文章",
        "提升文章质量",
        "丰富一下内容",
        "修正一下标题",
        "充实文章内容",
        "改进这个帖子",
    ]
    for msg in cases:
        intent = await tu.understand(msg)
        assert intent.goal_category == "IMPROVE_CONTENT", (
            f"'{msg}' → {intent.goal_category}, expected IMPROVE_CONTENT"
        )
        assert intent.relation == "MODIFY_TASK", (
            f"'{msg}' → {intent.relation}, expected MODIFY_TASK"
        )


@pytest.mark.asyncio
async def test_create_variants_all_create() -> None:
    """'写一篇/创建一篇/生成一篇' → CREATE_CONTENT."""
    tu = TaskUnderstanding()

    cases = [
        "写一篇Java文章",
        "创建一篇Java文章",
        "生成一篇Java文章",
        "帮我创作一篇Java文章",
    ]
    for msg in cases:
        intent = await tu.understand(msg)
        assert intent.goal_category == "CREATE_CONTENT", (
            f"'{msg}' → {intent.goal_category}"
        )
        assert intent.relation == "NEW_TASK", (
            f"'{msg}' → {intent.relation}"
        )


# ── No new Task for modify ────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_does_not_create_new_task_id() -> None:
    """MODIFY_TASK with target_task_id should use that task_id."""
    service = RuntimeAgentService()
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-a", "title": "Revised", "status": "DRAFT"},
        },
    })
    result = await service.execute(_ctx(
        run_id="run-mod", trace_id="trace-mod",
        user_message="修改刚才那篇文章",
        goal_category="IMPROVE_CONTENT",
        relation="MODIFY_TASK",
        task_id="task-a-original",  # Same task_id
        target_task_id="task-a-original",
        requirements=[{"type": "IMPROVE"}],
        mcp=mcp,
    ))
    assert result.success is True
    # Task ID should be preserved (not a new one)
    assert result.task_id == "task-a-original"


# ── SINGLE_IMPROVE template used for simple modify ─────────────────

@pytest.mark.asyncio
async def test_simple_modify_uses_single_improve_template() -> None:
    """IMPROVE only → SINGLE_IMPROVE template → 1 step."""
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-a", "title": "Fixed", "status": "DRAFT"},
        },
    })
    service = RuntimeAgentService()
    result = await service.execute(_ctx(
        run_id="run-simple", trace_id="trace-simple",
        user_message="修改标题",
        goal_category="IMPROVE_CONTENT",
        relation="MODIFY_TASK",
        task_id="task-a",
        target_task_id="task-a",
        requirements=[{"type": "IMPROVE"}],
        mcp=mcp,
    ))
    assert result.success is True
    step_started = [e for e in result.events if e["event"] == "STEP_STARTED"]
    assert len(step_started) == 1  # SINGLE_IMPROVE → 1 step
