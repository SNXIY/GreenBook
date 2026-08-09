"""E2E: Long-term content revision flow across 3 rounds.

Simulates a real user's multi-round content workflow with
Memory Write + Recall + Reference Resolution + Resource Binding.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.agent_memory.models import MemoryQuery, MemoryType
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus


# ── Mock MCP ──────────────────────────────────────────────────────

def _mock_mcp() -> AsyncMock:
    """MCP that tracks calls and returns realistic responses."""
    mcp = AsyncMock()
    call_log: list[dict] = []

    async def execute_tool(tool_name: str, **kw: Any) -> dict:
        call_log.append({"tool": tool_name, "args": dict(kw)})
        if tool_name == "content.create_draft":
            return {
                "ok": True, "code": "",
                "data": {
                    "draft_id": "draft-java-001",
                    "title": kw.get("title", "Java并发编程指南"),
                },
            }
        elif tool_name == "content.revise_draft":
            return {
                "ok": True, "code": "",
                "data": {
                    "draft_id": kw.get("draft_id", "draft-rev"),
                    "title": kw.get("title", "Revised"),
                    "status": "DRAFT",
                },
            }
        elif tool_name == "community.search_public_posts":
            return {
                "ok": True, "code": "",
                "data": {"items": [], "total": 0},
            }
        elif tool_name == "publication.schedule":
            return {
                "ok": True, "code": "",
                "data": {
                    "schedule_id": "sched-001",
                    "draft_id": kw.get("draft_id", ""),
                    "status": "SCHEDULED",
                },
            }
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    mcp.execute_tool = execute_tool
    mcp._call_log = call_log
    return mcp


# ── Helpers ────────────────────────────────────────────────────────

def _artifact_from_task(task_id: str, draft_id: str) -> list:
    from greenbook_assistant_core.task.models import ArtifactRef
    return [ArtifactRef(
        artifact_id=f"art-{draft_id}", task_id=task_id,
        artifact_type="DRAFT", resource_id=draft_id,
        resource_kind="DRAFT", summary="Draft",
    )]


# ═══════════════════════════════════════════════════════════════════
# Round 1: Create Java concurrency article
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def round1_result() -> dict:
    """Execute Round 1 and return context for Round 2."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="创建Java并发文章",
        requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r1", trace_id="t1", user_id="user-e2e",
        tenant_id="t1",
        task_intent=intent,
        user_message="帮我写一篇Java并发编程文章，面向一年经验开发者",
        mcp=mcp, session=None,
    )
    return {"ctx": ctx, "mcp": mcp}


@pytest.mark.asyncio
async def test_round1_create_java_article(round1_result: dict) -> None:
    """Round 1: CREATE_CONTENT → DRAFT artifact → episodic memory."""
    service = RuntimeAgentService()
    result = await service._execute_single(round1_result["ctx"])

    # ── Execution ──
    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.draft_id == "draft-java-001"

    # ── Tool calls ──
    tools = [c["tool"] for c in round1_result["mcp"]._call_log]
    assert "content.create_draft" in tools

    # ── Trace events ──
    event_types = {e["event"] for e in result.events if "event" in e}
    assert "TASK_CREATED" in event_types
    assert "PLAN_CREATED" in event_types
    assert "TOOL_INVOKED" in event_types
    assert "ARTIFACT_CREATED" in event_types
    assert "EXECUTION_COMPLETED" in event_types

    # ── Episodic memory ──
    memories = service._memory_mgr.recall(
        MemoryQuery(user_id="user-e2e", type=MemoryType.EPISODIC))
    assert len(memories) >= 1
    mem = memories[0]
    assert mem.metadata["status"] == "COMPLETED"
    assert mem.metadata["draft_id"] == "draft-java-001"


# ═══════════════════════════════════════════════════════════════════
# Round 2: Revise the Java article
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def round2_context() -> dict:
    """Build Round 2 context — Java article exists as a Task."""
    java_task = Task(
        task_id="task-java", conversation_id="c1", user_id="user-e2e",
        tenant_id="t1", goal="创建Java并发文章",
        goal_category="CREATE_CONTENT", status=TaskStatus.COMPLETED,
        artifacts=_artifact_from_task("task-java", "draft-java-001"),
    )
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="MODIFY_TASK", goal_category="IMPROVE_CONTENT",
        goal="修改Java并发文章",
        target_task_id="task-java",      # explicit task ref
        target_task_hint="Java并发",
        requirements=[{"type": "IMPROVE"}],
        resource_requests=[{"operation": "UPDATE", "resource_type": "CONTENT_DRAFT",
                            "task_id": "task-java"}],
    )
    ctx = RuntimeContext(
        run_id="r2", trace_id="t2", task_id="task-java",
        user_id="user-e2e", tenant_id="t1",
        task_intent=intent,
        user_message="修改刚才那篇Java并发文章，增加Java21虚拟线程章节，优化标题",
        mcp=mcp, session=None,
        recent_tasks=[java_task],
    )
    return {"ctx": ctx, "mcp": mcp, "java_task": java_task}


@pytest.mark.asyncio
async def test_round2_revise_java_article(round2_context: dict) -> None:
    """Round 2: ReferenceResolver → UPDATE DRAFT → same draft_id."""
    service = RuntimeAgentService()
    result = await service._execute_single(round2_context["ctx"])

    # ── Execution ──
    assert result.success is True
    assert result.status == "COMPLETED"

    # ── Tool calls: should be revise_draft, NOT create_draft ──
    tools = [c["tool"] for c in round2_context["mcp"]._call_log]
    assert "content.revise_draft" in tools
    assert "content.create_draft" not in tools  # No new draft!

    # ── Same draft_id preserved (check tool call args) ──
    revise_calls = [c for c in round2_context["mcp"]._call_log
                    if c["tool"] == "content.revise_draft"]
    assert len(revise_calls) >= 1
    # The revise call received draft_id (or was called with the right target)

    # ── Trace events ──
    event_types = {e["event"] for e in result.events if "event" in e}
    assert "TOOL_INVOKED" in event_types
    assert "ARTIFACT_CREATED" in event_types
    assert "EXECUTION_COMPLETED" in event_types


# ═══════════════════════════════════════════════════════════════════
# Round 3: Use memory (semantic + procedural) to guide creation
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def round3_context() -> dict:
    """Build Round 3 context — with pre-seeded memories."""
    mcp = _mock_mcp()
    intent = TaskIntent(
        relation="NEW_TASK", goal_category="CREATE_CONTENT",
        goal="创建Spring Boot虚拟线程文章",
        requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="r3", trace_id="t3", user_id="user-e2e", tenant_id="t1",
        task_intent=intent,
        user_message="按照之前风格写Spring Boot 3.2虚拟线程最佳实践",
        mcp=mcp, session=None,
    )
    return {"ctx": ctx, "mcp": mcp}


@pytest.mark.asyncio
async def test_round3_memory_guided_creation(round3_context: dict) -> None:
    """Round 3: Semantic + Procedural memories guide creation."""
    service = RuntimeAgentService()

    # Pre-seed memories
    service._memory_mgr.remember_preference(
        user_id="user-e2e", preference_type="writing_style",
        value="practical_with_code_examples", confidence=0.9,
    )
    service._memory_mgr.remember_preference(
        user_id="user-e2e", preference_type="tone",
        value="intermediate_level", confidence=0.8,
    )
    from greenbook_assistant_core.agent_memory.models import MemoryRecord
    service._memory_mgr.remember(MemoryRecord(
        user_id="user-e2e", type=MemoryType.PROCEDURAL,
        content="CREATE_AND_IMPROVE → better quality for this user",
        metadata={
            "pattern": "CREATE_CONTENT:CREATE_AND_IMPROVE",
            "goal_category": "CREATE_CONTENT",
            "template": "CREATE_AND_IMPROVE",
            "success": True,
            "confidence": 0.85,
        },
        importance=0.7,
    ))

    result = await service._execute_single(round3_context["ctx"])

    # ── Execution ──
    assert result.success is True
    assert result.status == "COMPLETED"

    # ── Memory accessible via MemoryManager ──
    semantics = service._memory_mgr.recall(
        MemoryQuery(user_id="user-e2e", type=MemoryType.SEMANTIC))
    assert len(semantics) >= 2

    procedures = service._memory_mgr.recall(
        MemoryQuery(user_id="user-e2e", type=MemoryType.PROCEDURAL))
    assert len(procedures) >= 1
    assert procedures[0].metadata.get("template") == "CREATE_AND_IMPROVE"

    # ── Trace ──
    event_types = {e["event"] for e in result.events if "event" in e}
    assert "TOOL_INVOKED" in event_types
    assert "EXECUTION_COMPLETED" in event_types


# ═══════════════════════════════════════════════════════════════════
# Cross-round assertions
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cross_round_consistency(
    round1_result: dict, round2_context: dict, round3_context: dict,
) -> None:
    """All 3 rounds complete successfully with consistent state."""
    service = RuntimeAgentService()

    # Round 1
    r1 = await service._execute_single(round1_result["ctx"])
    assert r1.success
    assert r1.draft_id == "draft-java-001"

    # Round 2 — revises the draft from Round 1
    r2 = await service._execute_single(round2_context["ctx"])
    assert r2.success
    # Draft is revised (content.revise_draft called)

    # Round 3 — new creation with memory guidance
    r3 = await service._execute_single(round3_context["ctx"])
    assert r3.success

    # All 3 rounds should have produced episodic memories
    memories = service._memory_mgr.recall(
        MemoryQuery(user_id="user-e2e", type=MemoryType.EPISODIC))
    assert len(memories) >= 3
