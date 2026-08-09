"""Phase 6.6 Stage 3 tests — Memory Recall integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.agent_memory.models import MemoryRecord, MemoryType
from greenbook_assistant_core.task.models import TaskIntent


def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    mcp = AsyncMock()
    async def h(tool_name: str, **kw: Any) -> dict:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL"}
    mcp.execute_tool = h
    return mcp


# ── Case 1: user preferences recalled ──────────────────────────

@pytest.mark.asyncio
async def test_semantic_preferences_recalled() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-pref", "title": "Pref Test"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="u-pref",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    # Pre-seed semantic memories
    service._memory_mgr.remember_preference(
        user_id="u-pref", preference_type="writing_style",
        value="practical_with_code", confidence=0.9,
    )
    service._memory_mgr.remember_preference(
        user_id="u-pref", preference_type="publish_time",
        value="morning", confidence=0.7,
    )

    await service._execute_single(ctx)

    # context should have preferences
    prefs = ctx.memory_context.get("preferences", [])
    assert len(prefs) >= 2
    assert any(p["type"] == "writing_style" for p in prefs)
    assert any(p["type"] == "publish_time" for p in prefs)


# ── Case 2: recent task history recalled ────────────────────────

@pytest.mark.asyncio
async def test_recent_task_history_recalled() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-hist", "title": "History"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", user_id="u-hist",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    # Pre-seed episodic memory (simulating a previous execution)
    service._memory_mgr.remember_execution(
        user_id="u-hist", goal="创建Java文章",
        category="CREATE_CONTENT", status="COMPLETED",
        draft_id="d-old",
    )

    await service._execute_single(ctx)

    recent = ctx.memory_context.get("recent_tasks", [])
    assert len(recent) >= 1
    assert recent[0]["goal"] == "创建Java文章"
    assert recent[0]["draft_id"] == "d-old"


# ── Case 3: user isolation ─────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_user_isolation() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-iso", "title": "Iso"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )

    # Seed preference for user-a only
    service = RuntimeAgentService()
    service._memory_mgr.remember_preference(
        user_id="user-a", preference_type="style", value="casual",
    )

    ctx_b = RuntimeContext(
        run_id="rb", trace_id="tb", user_id="user-b",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )
    await service._execute_single(ctx_b)
    # User-b should have empty preferences
    assert ctx_b.memory_context.get("preferences", []) == []


# ── Case 4: empty memory → normal execution ─────────────────────

@pytest.mark.asyncio
async def test_empty_memory_normal_execution() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-empty", "title": "Empty"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r4", trace_id="t4", user_id="u-new",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    # Should still succeed with empty memory
    assert result.success is True
    assert ctx.memory_context.get("preferences", []) == []
    assert ctx.memory_context.get("recent_tasks", []) == []


# ── Case 5: recall does not affect old path ────────────────────

@pytest.mark.asyncio
async def test_recall_no_side_effect_on_old_path() -> None:
    """Memory recall is additive — old execution path unchanged."""
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-noff", "title": "No Side Effect"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r5", trace_id="t5", user_id="u-noside",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    # Seed a memory
    service._memory_mgr.remember_preference(
        user_id="u-noside", preference_type="test", value="x",
    )
    service._memory_mgr.remember_execution(
        user_id="u-noside", goal="old task", status="COMPLETED",
    )

    result = await service._execute_single(ctx)

    # Execution still works
    assert result.success is True
    assert result.draft_id == "d-noff"
    # Memory context populated
    assert len(ctx.memory_context.get("preferences", [])) >= 1
    assert len(ctx.memory_context.get("recent_tasks", [])) >= 1
