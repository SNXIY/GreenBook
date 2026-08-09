"""Phase 6.2.2-B E2E tests — ReferenceResolver integrated into Runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus
from greenbook_assistant_core.task.reference_resolver import TaskReferenceResolver


def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    mcp = AsyncMock()

    async def h(tool_name: str, **kw: Any) -> dict:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    mcp.execute_tool = h
    return mcp


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _hours_ago_sec(hours: float) -> int:
    return int(hours * 3600)


# ── Case 1: "修改昨天那个文章标题" → resolves to yesterday's task ─

@pytest.mark.asyncio
async def test_modify_yesterday_article_resolves_target() -> None:
    """ReferenceResolver finds yesterday's task and sets ctx.task_id."""
    yesterday_task = Task(
        task_id="task-yesterday", conversation_id="c1", user_id="u1",
        tenant_id="t1", goal="创建Java文章", goal_category="CREATE_CONTENT",
        status=TaskStatus.COMPLETED,
        created_at=_ago(_hours_ago_sec(30)),  # 30 hours ago = yesterday
    )

    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-old", "title": "Revised", "status": "DRAFT"},
        },
    })
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改昨天那篇文章标题",
        target_task_hint="昨天那个文章",
        requirements=[{"type": "IMPROVE"}],
    )
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="u1",
        task_intent=intent, user_message="修改昨天那篇文章标题",
        mcp=mcp, session=None,
        recent_tasks=[yesterday_task],
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    # Should succeed — ReferenceResolver found yesterday's task
    assert result.success is True
    assert result.draft_id == "draft-old"


# ── Case 2: "修改刚才那个" with 2 recent tasks → clarification ──

@pytest.mark.asyncio
async def test_ambiguous_recent_returns_clarification() -> None:
    """Two recent tasks + '修改刚才那个' → needs_clarification."""
    tasks = [
        Task(task_id="t1", conversation_id="c1", user_id="u1", tenant_id="t1",
             goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED, created_at=_ago(10)),
        Task(task_id="t2", conversation_id="c1", user_id="u1", tenant_id="t1",
             goal="创建Python文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED, created_at=_ago(20)),
    ]
    mcp = _mock_mcp({})
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改刚才那个", target_task_hint="刚才那个",
        requirements=[{"type": "IMPROVE"}],
    )
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", user_id="u1",
        task_intent=intent, user_message="修改刚才那个",
        mcp=mcp, session=None, recent_tasks=tasks,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    # Should return clarification — ambiguous reference
    assert result.success is False
    assert result.status == "WAITING_HUMAN"
    assert "WAITING_HUMAN" in result.error_code


# ── Case 3: ReferenceResolver correctly resolves to single recent task ─

@pytest.mark.asyncio
async def test_single_recent_task_resolves_cleanly() -> None:
    """One recent task + '修改刚才的文章' → resolves successfully."""
    tasks = [
        Task(task_id="t1", conversation_id="c1", user_id="u1", tenant_id="t1",
             goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED, created_at=_ago(10)),
    ]
    mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d1", "title": "Revised", "status": "DRAFT"},
        },
    })
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改刚才的文章", target_task_hint="刚才的文章",
        requirements=[{"type": "IMPROVE"}],
    )
    ctx = RuntimeContext(
        run_id="r3", trace_id="t3", user_id="u1",
        task_intent=intent, user_message="修改刚才的文章",
        mcp=mcp, session=None, recent_tasks=tasks,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    assert result.success is True
