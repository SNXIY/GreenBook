"""Phase 6.5 Stage 2 tests — HumanInteraction integrated into Runtime."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus


def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    mcp = AsyncMock()

    async def h(tool_name: str, **kw: Any) -> dict:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    mcp.execute_tool = h
    return mcp


# ── Case 1: two candidates → clarification → SELECT → success ──

@pytest.mark.asyncio
async def test_clarification_two_candidates_resume() -> None:
    """Two matching tasks → pause → user selects → execute."""
    tasks = [
        Task(task_id="task-a", conversation_id="c1", user_id="u1",
             tenant_id="t1", goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
        Task(task_id="task-b", conversation_id="c1", user_id="u1",
             tenant_id="t1", goal="创建Python文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
    ]
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d1", "title": "Revised", "status": "DRAFT"},
        },
    })
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改刚才那个", target_task_hint="刚才那个",
        requirements=[{"type": "IMPROVE"}],
        resource_requests=[{"operation": "UPDATE", "resource_type": "CONTENT_DRAFT"}],
    )
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="u1",
        task_intent=intent, user_message="修改刚才那个",
        mcp=mcp, session=None, recent_tasks=tasks,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    # Should pause for clarification
    assert result.status == "WAITING_HUMAN"
    interaction_id = result.partial_results.get("interaction_id")
    assert interaction_id is not None

    # User selects task-a
    resumed = await service.resume_human_interaction(interaction_id, "task-a")
    assert resumed.success is True
    assert resumed.status == "COMPLETED"


# ── Case 2: single candidate → no pause → executes normally ─────

@pytest.mark.asyncio
async def test_single_candidate_no_pause() -> None:
    """One match → no clarification needed."""
    tasks = [
        Task(task_id="task-a", conversation_id="c1", user_id="u1",
             tenant_id="t1", goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
    ]
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d1", "title": "Revised", "status": "DRAFT"},
        },
    })
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改Java文章", target_task_hint="Java文章",
        requirements=[{"type": "IMPROVE"}],
        resource_requests=[{"operation": "UPDATE", "resource_type": "CONTENT_DRAFT"}],
    )
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", user_id="u1",
        task_intent=intent, user_message="修改Java文章",
        mcp=mcp, session=None, recent_tasks=tasks,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)
    assert result.success is True
    assert result.status == "COMPLETED"


# ── Case 3: expired interaction → cannot resume ─────────────────

@pytest.mark.asyncio
async def test_expired_interaction_cannot_resume() -> None:
    """Resume with expired/nonexistent interaction_id → FAILED."""
    service = RuntimeAgentService()
    resumed = await service.resume_human_interaction("nonexistent-id", "any")
    assert resumed.success is False
    assert resumed.error_code in ("INTERACTION_EXPIRED", "NO_PAUSED_CONTEXT")


# ── Case 4: reference_resolution clarification → resume ─────────

@pytest.mark.asyncio
async def test_reference_clarification_resume() -> None:
    """Two recent tasks + temporal hint → pause → select → execute."""
    tasks = [
        Task(task_id="task-a", conversation_id="c1", user_id="u1",
             tenant_id="t1", goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
        Task(task_id="task-b", conversation_id="c1", user_id="u1",
             tenant_id="t1", goal="创建Python文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
    ]
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d1", "title": "Revised", "status": "DRAFT"},
        },
    })
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改刚才那个", target_task_hint="刚才那个",
        requirements=[{"type": "IMPROVE"}],
    )
    ctx = RuntimeContext(
        run_id="r3", trace_id="t3", user_id="u1",
        task_intent=intent, user_message="修改刚才那个",
        mcp=mcp, session=None, recent_tasks=tasks,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    assert result.status == "WAITING_HUMAN"
    iid = result.partial_results.get("interaction_id")

    resumed = await service.resume_human_interaction(iid, "task-b")
    assert resumed.success is True
