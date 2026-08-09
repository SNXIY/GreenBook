"""Phase 6.6 Stage 2 tests — Memory write integration with Runtime."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.agent_memory.models import MemoryQuery, MemoryType
from greenbook_assistant_core.task.models import TaskIntent


def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    mcp = AsyncMock()

    async def h(tool_name: str, **kw: Any) -> dict:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    mcp.execute_tool = h
    return mcp


# ── Case 1: successful execution → episodic memory ──────────────

@pytest.mark.asyncio
async def test_success_creates_episodic_memory() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-mem", "title": "Memory Test"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写一篇Java文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="u-mem-1",
        task_intent=intent, user_message="写一篇Java文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    assert result.success is True

    # Check memory was recorded
    memories = service._memory_mgr.recall(
        MemoryQuery(user_id="u-mem-1", type=MemoryType.EPISODIC))
    assert len(memories) >= 1
    mem = memories[0]
    assert mem.type == MemoryType.EPISODIC
    assert mem.metadata["status"] == "COMPLETED"
    assert mem.metadata["draft_id"] == "d-mem"
    assert mem.metadata["goal_category"] == "CREATE_CONTENT"


# ── Case 2: memory contains artifact/resource metadata ─────────

@pytest.mark.asyncio
async def test_memory_contains_resource_metadata() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-meta", "title": "Meta"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", user_id="u-mem-2",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    await service._execute_single(ctx)

    memories = service._memory_mgr.recall(
        MemoryQuery(user_id="u-mem-2", type=MemoryType.EPISODIC))
    assert len(memories) >= 1
    mem = memories[0]
    assert mem.metadata.get("draft_id") == "d-meta"
    assert mem.metadata.get("goal_category") == "CREATE_CONTENT"
    assert mem.importance >= 0.7  # COMPLETED + has draft


# ── Case 3: failed execution → lower importance memory ─────────

@pytest.mark.asyncio
async def test_failed_execution_lower_importance() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": False, "code": "TOOL_ERROR",
            "message": "Creation failed", "retryable": False,
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r3", trace_id="t3", user_id="u-mem-3",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    await service._execute_single(ctx)

    memories = service._memory_mgr.recall(
        MemoryQuery(user_id="u-mem-3", type=MemoryType.EPISODIC))
    assert len(memories) >= 1
    mem = memories[0]
    assert mem.metadata["status"] == "FAILED"
    assert mem.importance <= 0.6  # FAILED + no draft → lower


# ── Case 4: user isolation ─────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_user_isolation() -> None:
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d-iso", "title": "Isolated"},
        },
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写文章", requirements=[{"type": "CREATE"}],
    )

    # User A executes
    ctx_a = RuntimeContext(
        run_id="ra", trace_id="ta", user_id="user-a",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )
    service_a = RuntimeAgentService()
    await service_a._execute_single(ctx_a)

    # User B executes
    ctx_b = RuntimeContext(
        run_id="rb", trace_id="tb", user_id="user-b",
        task_intent=intent, user_message="写文章",
        mcp=mcp, session=None,
    )
    service_b = RuntimeAgentService()
    await service_b._execute_single(ctx_b)

    # User A can't see User B's memories
    a_memories = service_a._memory_mgr.recall(
        MemoryQuery(user_id="user-a", type=MemoryType.EPISODIC))
    assert len(a_memories) == 1
    assert a_memories[0].user_id == "user-a"
