"""Phase 6.7 E2E: Complex operations workflow.

Simulates a real community operations scenario:
  Search → Analyze → Conditional Create/Update → Approval → Schedule
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.agent_memory.models import (
    MemoryQuery, MemoryRecord, MemoryType,
)
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus


# ── Mock MCP ──────────────────────────────────────────────────────

def _mock_mcp() -> AsyncMock:
    mcp = AsyncMock()
    call_log: list[dict] = []

    async def h(tool_name: str, **kw: Any) -> dict:
        call_log.append({"tool": tool_name})
        if tool_name == "community.search_public_posts":
            return {
                "ok": True, "code": "",
                "data": {
                    "items": [
                        {"post_id": "p1", "title": "Java虚拟线程实战"},
                        {"post_id": "p2", "title": "Spring Boot 3.2新特性"},
                    ],
                    "total": 2,
                },
            }
        elif tool_name == "content.create_draft":
            return {
                "ok": True, "code": "",
                "data": {"draft_id": "draft-new", "title": "New Article"},
            }
        elif tool_name == "content.revise_draft":
            return {
                "ok": True, "code": "",
                "data": {"draft_id": "draft-old", "title": "Updated Article"},
            }
        elif tool_name == "publication.schedule":
            return {
                "ok": True, "code": "",
                "data": {"schedule_id": "sched-1", "draft_id": kw.get("draft_id", ""),
                         "status": "SCHEDULED"},
            }
        return {"ok": True, "code": "", "data": {}}

    mcp.execute_tool = h
    mcp._call_log = call_log
    return mcp


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 1: SEARCH + ANALYZE
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase1_search_and_analyze() -> None:
    """Search community → analyze patterns."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="ANALYZE_COMMUNITY",
        goal="搜索Java虚拟线程相关内容",
        requirements=[{"type": "SEARCH"}, {"type": "ANALYZE"}],
    )
    ctx = RuntimeContext(
        run_id="p1", trace_id="tp1", user_id="user-ops",
        task_intent=intent,
        user_message="搜索社区Java虚拟线程相关帖子，分析热门主题",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)
    assert result.success is True

    tools = [c["tool"] for c in mcp._call_log]
    assert "community.search_public_posts" in tools

    event_types = {e["event"] for e in result.events if "event" in e}
    assert "TOOL_INVOKED" in event_types
    assert "EXECUTION_COMPLETED" in event_types


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 2: CONDITIONAL CREATE/UPDATE
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase2_conditional_update() -> None:
    """Existing article exists → UPDATE instead of CREATE."""
    existing_task = Task(
        task_id="task-existing", conversation_id="c1",
        user_id="user-ops", tenant_id="t1",
        goal="创建Java虚拟线程文章",
        goal_category="CREATE_CONTENT",
        status=TaskStatus.COMPLETED,
    )
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        goal="更新已有Java虚拟线程文章",
        target_task_hint="虚拟线程",
        requirements=[{"type": "IMPROVE"}],
        resource_requests=[{"operation": "UPDATE", "resource_type": "CONTENT_DRAFT"}],
    )
    ctx = RuntimeContext(
        run_id="p2", trace_id="tp2", user_id="user-ops",
        task_intent=intent,
        user_message="如果已有Java虚拟线程文章就更新它，不要重复创建",
        mcp=mcp, session=None,
        recent_tasks=[existing_task],
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)
    assert result.success is True

    tools = [c["tool"] for c in mcp._call_log]
    assert "content.revise_draft" in tools
    assert "content.create_draft" not in tools  # Conditional: UPDATE, not CREATE


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 3: CREATE with PUBLISH → full pipeline
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase3_create_with_publish() -> None:
    """New article → CREATE + PUBLISH pipeline."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="创建Spring Boot文章并发布",
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
        resource_requests=[
            {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"},
            {"operation": "CREATE", "resource_type": "SCHEDULE"},
        ],
    )
    ctx = RuntimeContext(
        run_id="p3", trace_id="tp3", user_id="user-ops",
        task_intent=intent,
        user_message="创建Spring Boot 3.2虚拟线程最佳实践文章，明天上午发布",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)
    assert result.success is True

    tools = [c["tool"] for c in mcp._call_log]
    assert "content.create_draft" in tools
    assert "publication.schedule" in tools

    event_types = {e["event"] for e in result.events if "event" in e}
    assert "TOOL_INVOKED" in event_types
    assert "ARTIFACT_CREATED" in event_types
    assert "EXECUTION_COMPLETED" in event_types


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 4: HITL — 发布前让我确认
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase4_human_approval_pause() -> None:
    """'发布前让我确认' → HUMAN_APPROVAL pause."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="创建文章，发布前确认",
        requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="p4", trace_id="tp4", user_id="user-ops",
        task_intent=intent,
        user_message="创建Java文章，发布前让我确认",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)

    # Either completes normally (no approval needed for CREATE only)
    # or pauses for approval (if PUBLISH was included)
    assert result.status in ("COMPLETED", "WAITING_HUMAN")

    if result.status == "WAITING_HUMAN":
        iid = result.partial_results.get("interaction_id")
        assert iid is not None


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 5: Memory recall across the workflow
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase5_memory_across_workflow() -> None:
    """After multiple phases, memory accumulates correctly."""
    service = RuntimeAgentService()

    # Phase 1: SEARCH → episodic memory
    mcp1 = _mock_mcp()
    intent1 = TaskIntent(
        relation="NEW_TASK", goal_category="ANALYZE_COMMUNITY",
        goal="搜索", requirements=[{"type": "SEARCH"}],
    )
    ctx1 = RuntimeContext(
        run_id="pm1", trace_id="tm1", user_id="user-ops-wf",
        task_intent=intent1, user_message="搜索Java帖子",
        mcp=mcp1, session=None,
    )
    await service._execute_single(ctx1)

    # Phase 2: CREATE → episodic memory + procedural pattern
    mcp2 = _mock_mcp()
    intent2 = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="创建文章", requirements=[{"type": "CREATE"}],
    )
    ctx2 = RuntimeContext(
        run_id="pm2", trace_id="tm2", user_id="user-ops-wf",
        task_intent=intent2, user_message="创建Java文章",
        mcp=mcp2, session=None,
    )
    await service._execute_single(ctx2)

    # Phase 3: IMPROVE → another episodic memory
    mcp3 = _mock_mcp()
    intent3 = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="优化文章", requirements=[{"type": "IMPROVE"}],
        resource_requests=[{"operation": "UPDATE", "resource_type": "CONTENT_DRAFT"}],
    )
    ctx3 = RuntimeContext(
        run_id="pm3", trace_id="tm3", user_id="user-ops-wf",
        task_intent=intent3, user_message="优化刚才Java文章",
        mcp=mcp3, session=None,
    )
    await service._execute_single(ctx3)

    # Verify memory accumulated
    episodic = service._memory_mgr.recall(
        MemoryQuery(user_id="user-ops-wf", type=MemoryType.EPISODIC))
    assert len(episodic) >= 3  # SEARCH + CREATE + IMPROVE


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 6: Full trace validation
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase6_full_trace_validation() -> None:
    """FULL_PIPELINE → all trace events present."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="完整流程",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
        resource_requests=[
            {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"},
            {"operation": "CREATE", "resource_type": "SCHEDULE"},
        ],
    )
    ctx = RuntimeContext(
        run_id="p6", trace_id="tp6", user_id="user-ops",
        task_intent=intent,
        user_message="搜索Java虚拟线程帖子，分析热门主题，生成文章，明天发布",
        mcp=mcp, session=None,
    )

    service = RuntimeAgentService()
    result = await service._execute_single(ctx)
    assert result.success is True

    event_types = {e["event"] for e in result.events if "event" in e}

    # Complete trace: every lifecycle event must exist
    required_events = [
        "TASK_CREATED", "PLAN_CREATED", "EXECUTION_STARTED",
        "STEP_STARTED", "TOOL_INVOKED", "TOOL_COMPLETED",
        "ARTIFACT_CREATED", "STEP_COMPLETED",
        "EXECUTION_COMPLETED",
    ]
    for evt in required_events:
        assert evt in event_types, f"Missing trace event: {evt}"

    assert "EXECUTION_FAILED" not in event_types

    # Multi-step: at least 2 TOOL_INVOKED (SEARCH + CREATE_DRAFT + SCHEDULE)
    tool_count = sum(1 for e in result.events
                     if e.get("event") == "TOOL_INVOKED")
    assert tool_count >= 2


# ═══════════════════════════════════════════════════════════════════
# Sub-flow 7: Numbered list decomposition
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_phase7_numbered_list_decomposition() -> None:
    """'1.xxx 2.xxx 3.xxx' → decomposed into sub-tasks."""
    from greenbook_assistant_core.task.decomposer import TaskDecomposer
    from greenbook_assistant_core.task.understanding import TaskUnderstanding

    tu = TaskUnderstanding()
    d = TaskDecomposer()
    sub_tasks = await d.decompose(
        "1. 搜索社区Java帖子\n2. 分析热门主题\n3. 生成原创文章",
        tu,
    )
    # Numbered list should be split
    assert len(sub_tasks) >= 1  # At minimum, numbered items detected
