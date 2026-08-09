"""Phase 6.5 Stage 4 tests — INPUT type via HumanInteraction."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.human.models import (
    HumanInteractionResponse,
    InteractionType,
)
from greenbook_assistant_core.task.models import TaskIntent


# ── Case 1: INPUT pause creates interaction ──────────────────────

@pytest.mark.asyncio
async def test_input_pause_creates_request() -> None:
    """_pause_for_input → WAITING_HUMAN + interaction_id."""
    intent = TaskIntent(goal_category="PUBLISH_CONTENT", relation="NEW_TASK")
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="u1",
        task_intent=intent, user_message="发布文章",
        mcp=AsyncMock(), session=None,
    )
    service = RuntimeAgentService()
    result = service._pause_for_input(
        ctx, question="请选择发布平台",
        options=[{"value": "greenbook", "label": "GreenBook"},
                 {"value": "wechat", "label": "微信"}],
    )
    assert result.status == "WAITING_HUMAN"
    iid = result.partial_results.get("interaction_id")
    assert iid is not None


# ── Case 2: INPUT resume — content injected into ctx ─────────────

@pytest.mark.asyncio
async def test_input_resume_injects_content() -> None:
    """User provides input → injected into ctx.user_message + constraints."""
    intent = TaskIntent(goal_category="CREATE_CONTENT", relation="NEW_TASK",
                        requirements=[{"type": "CREATE"}])
    mcp = AsyncMock()
    mcp.execute_tool = AsyncMock(return_value={
        "ok": True, "code": "",
        "data": {"draft_id": "d1", "title": "Test"},
    })
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", user_id="u1",
        task_intent=intent, user_message="写一篇文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = service._pause_for_input(
        ctx, question="请描述文章主题和风格",
    )
    iid = result.partial_results.get("interaction_id")

    # Resume via service method (handles content injection)
    await service.resume_human_interaction(
        iid, "", content="Java并发编程，面向中级开发者，包含实战代码",
        decision="INPUT",
    )

    # Verify content was injected into constraints
    constraints = getattr(ctx.task_intent, "constraints", [])
    assert any(c.get("type") == "USER_INPUT" for c in constraints)
    assert any("Java并发编程" in str(c.get("value", "")) for c in constraints)


# ── Case 3: expired INPUT cannot resume ──────────────────────────

def test_expired_input_cannot_resume() -> None:
    from datetime import UTC, datetime, timedelta
    from greenbook_assistant_core.human.manager import HumanInteractionManager
    mgr = HumanInteractionManager()
    req = mgr.pause(execution_id="e1", type=InteractionType.INPUT)
    stored = mgr.store.find_by_id(req.interaction_id)
    stored.expires_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    mgr.store.save(stored)

    resp = HumanInteractionResponse(interaction_id=req.interaction_id)
    resumed = mgr.resume(req.interaction_id, resp)
    assert resumed is None


# ── Case 4: INPUT resume with full execute_single ────────────────

@pytest.mark.asyncio
async def test_input_resume_full_execution() -> None:
    """Resume INPUT → _execute_single completes."""
    mcp = AsyncMock()
    mcp.execute_tool = AsyncMock(return_value={
        "ok": True, "code": "",
        "data": {"draft_id": "d-input", "title": "Input Test"},
    })
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="写一篇文章", requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r4", trace_id="t4", user_id="u1",
        task_intent=intent, user_message="写一篇文章",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    # Pause for input
    result = service._pause_for_input(ctx, question="请补充文章主题")
    iid = result.partial_results["interaction_id"]

    # Resume with INPUT
    resumed = await service.resume_human_interaction(
        iid, "",  # selected_value empty, content goes to user_message
    )
    # Note: the empty response content means the original message stays
    assert resumed is not None
