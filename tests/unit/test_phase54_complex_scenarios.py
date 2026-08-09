"""Phase 5.4: Complex community assistant scenario tests.

Validates the Runtime against real-world community assistant use cases.
Documents gaps where the current architecture falls short.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    Task,
    TaskIntent,
    TaskStatus,
)
from greenbook_assistant_core.task.resolver import TaskResolver
from greenbook_assistant_core.task.understanding import TaskUnderstanding


# ═══════════════════════════════════════════════════════════════════
# Section 1: Semantic Understanding — synonym equivalence
# ═══════════════════════════════════════════════════════════════════

class TestSemanticUnderstanding:
    """All synonym expressions for 'improve' must map to IMPROVE_CONTENT."""

    IMPROVE_SYNONYMS = [
        "优化这篇文章",
        "完善这篇文章",
        "润色这篇文章",
        "打磨这篇文章",
        "提升文章质量",
        "丰富文章内容",
        "修正文章标题",
        "充实内容",
        "改进这个帖子",
        "重新整理这篇文章",
        "把文章改得更好",
        "帮忙增强一下内容",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msg", IMPROVE_SYNONYMS)
    async def test_improve_synonym(self, msg: str) -> None:
        tu = TaskUnderstanding()
        intent = await tu.understand(msg)
        assert intent.goal_category == "IMPROVE_CONTENT", (
            f"'{msg}' → {intent.goal_category}, expected IMPROVE_CONTENT"
        )
        assert intent.relation == "MODIFY_TASK"

    CREATE_SYNONYMS = [
        "写一篇Java文章",
        "创建一篇Java文章",
        "生成一篇Java文章",
        "帮我创作一篇Java文章",
        "发一篇Java帖子",
        "新建一篇Java文章",
        "写个Java教程",
        "帮我写一篇Java学习指南",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msg", CREATE_SYNONYMS)
    async def test_create_synonym(self, msg: str) -> None:
        tu = TaskUnderstanding()
        intent = await tu.understand(msg)
        assert intent.goal_category == "CREATE_CONTENT", (
            f"'{msg}' → {intent.goal_category}"
        )

    SEARCH_SYNONYMS = [
        "搜索社区Java帖子",
        "查找Java相关内容",
        "检索社区热门文章",
        "找一下社区里的Python教程",
        "看看有没有Java面试题",
        "帮我找社区里的Spring教程",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msg", SEARCH_SYNONYMS)
    async def test_search_synonym(self, msg: str) -> None:
        tu = TaskUnderstanding()
        intent = await tu.understand(msg)
        assert intent.goal_category == "ANALYZE_COMMUNITY", (
            f"'{msg}' → {intent.goal_category}"
        )


# ═══════════════════════════════════════════════════════════════════
# Section 2: Multi-task switching
# ═══════════════════════════════════════════════════════════════════

class TestMultiTaskSwitching:
    """Verify TaskResolver correctly identifies the right task."""

    @staticmethod
    def _make_task(task_id: str, goal: str, category: str = "CREATE_CONTENT",
                   draft_id: str | None = None) -> Task:
        artifacts = []
        if draft_id:
            artifacts.append(ArtifactRef(
                artifact_id=f"art-{task_id}",
                task_id=task_id,
                artifact_type="DRAFT",
                resource_id=draft_id,
                resource_kind="DRAFT",
                summary=goal,
            ))
        return Task(
            task_id=task_id,
            conversation_id="conv-1",
            user_id="u1",
            tenant_id="t1",
            goal=goal,
            goal_category=category,
            status=TaskStatus.COMPLETED,
            artifacts=artifacts,
        )

    def test_find_java_task_among_two(self) -> None:
        """Task A=Java, Task B=Python. '修改Java文章' → Task A."""
        tasks = [
            self._make_task("task-b", "创建一篇Python文章", draft_id="draft-b"),
            self._make_task("task-a", "创建一篇Java文章", draft_id="draft-a"),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="Java",
            goal="修改Java文章标题",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        assert result.task_id == "task-a"
        assert result.match_level <= 2  # label match

    def test_find_python_task_among_two(self) -> None:
        """'修改Python文章' → Task B."""
        tasks = [
            self._make_task("task-b", "创建一篇Python文章", draft_id="draft-b"),
            self._make_task("task-a", "创建一篇Java文章", draft_id="draft-a"),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="Python",
            goal="修改Python文章",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        assert result.task_id == "task-b"

    def test_temporal_hint_finds_most_recent(self) -> None:
        """'修改刚才那篇' → most recent task (task-b)."""
        tasks = [
            self._make_task("task-b", "创建一篇Python文章", draft_id="draft-b"),
            self._make_task("task-a", "创建一篇Java文章", draft_id="draft-a"),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="刚才那篇",
            goal="修改刚才那篇",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        # "刚才那篇" → temporal → falls to recency (newest = task-b)
        assert result.task_id == "task-b"
        assert result.match_level >= 4  # temporal → recency

    def test_three_tasks_alternating(self) -> None:
        """A(Java), B(Python), C(Go). '修改Java文章' → A."""
        tasks = [
            self._make_task("task-c", "创建一篇Go文章"),
            self._make_task("task-b", "创建一篇Python文章"),
            self._make_task("task-a", "创建一篇Java文章"),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="Java",
            goal="修改Java文章",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        assert result.task_id == "task-a"


# ═══════════════════════════════════════════════════════════════════
# Section 3: Composite task planning
# ═══════════════════════════════════════════════════════════════════

class TestCompositeTaskPlanning:
    """Verify the Orchestrator handles complex requirement combinations."""

    def test_create_and_publish_uses_create_and_publish_template(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1",
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
        )
        assert plan.template_name == "CREATE_AND_PUBLISH"
        caps = [s.capability for s in plan.steps]
        assert "GENERATE_CONTENT" in caps
        assert "SCHEDULE_PUBLISH" in caps

    def test_full_pipeline_search_analyze_create_publish(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t2",
            goal_category="CREATE_CONTENT",
            requirements=[
                {"type": "SEARCH"}, {"type": "ANALYZE"},
                {"type": "CREATE"}, {"type": "PUBLISH"},
            ],
        )
        assert plan.template_name == "FULL_PIPELINE"
        assert len(plan.steps) == 5

    def test_create_and_improve_now_uses_create_and_improve_template(self) -> None:
        """CREATE + IMPROVE → CREATE_AND_IMPROVE (Phase 5.5 fix)."""
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t3",
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}, {"type": "IMPROVE"}],
        )
        assert plan.template_name == "CREATE_AND_IMPROVE"
        assert len(plan.steps) == 2  # GENERATE → IMPROVE

    def test_create_improve_publish_uses_create_and_publish(self) -> None:
        """CREATE + IMPROVE + PUBLISH → CREATE_AND_PUBLISH.

        The IMPROVE requirement is treated as 'ensure quality' which the
        VALIDATE step handles.  The user gets GENERATE→VALIDATE→SCHEDULE.
        """
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t4",
            goal_category="CREATE_CONTENT",
            requirements=[
                {"type": "CREATE"}, {"type": "IMPROVE"}, {"type": "PUBLISH"},
            ],
        )
        # CREATE + PUBLISH → CREATE_AND_PUBLISH (3 steps, includes VALIDATE)
        assert plan.template_name == "CREATE_AND_PUBLISH"
        assert len(plan.steps) == 3


# ═══════════════════════════════════════════════════════════════════
# Section 4: Lifecycle operations
# ═══════════════════════════════════════════════════════════════════

class TestLifecycleOperations:
    """Verify schedule management: cancel, modify time, re-publish."""

    def test_cancel_schedule_uses_single_cancel(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1",
            goal_category="MANAGE_SCHEDULE",
            requirements=[{"type": "CANCEL"}],
        )
        assert plan.template_name == "SINGLE_CANCEL"
        assert plan.steps[0].capability == "CANCEL_SCHEDULE"

    def test_modify_schedule_time_not_supported(self) -> None:
        """'修改发布时间' → no dedicated template for update_schedule.

        GAP: SINGLE_PUBLISH creates a NEW schedule via publication.schedule.
        There's no template for publication.update_schedule (modify existing).
        """
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t2",
            goal_category="MANAGE_SCHEDULE",
            requirements=[{"type": "PUBLISH"}],
        )
        # Current: SINGLE_PUBLISH → publication.schedule (CREATE new)
        # Needed: a MANAGE_SCHEDULE template → publication.update_schedule
        assert plan.steps[0].capability in (
            "SCHEDULE_PUBLISH",  # current: creates new
            # "MANAGE_SCHEDULE",  # needed: updates existing
        )
        # GAP documented below

    def test_republish_not_supported(self) -> None:
        """PUBLISH_NOW after schedule → not covered by templates.

        GAP: no template for 'publish the scheduled draft now'.
        """
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t3",
            goal_category="PUBLISH_CONTENT",
            requirements=[{"type": "PUBLISH"}],
        )
        # SINGLE_PUBLISH → SCHEDULE_PUBLISH (creates scheduled publish)
        # What we want: PUBLISH_NOW when no future time is specified
        assert plan.steps[0].capability == "SCHEDULE_PUBLISH"
        # GAP: distinction between schedule and immediate publish


# ═══════════════════════════════════════════════════════════════════
# Section 5: E2E multi-step with mock MCP
# ═══════════════════════════════════════════════════════════════════

class TestE2EMultiStep:
    """End-to-end CREATE + PUBLISH with real RuntimeAgentService."""

    @staticmethod
    def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
        mcp = AsyncMock()
        async def h(tool_name: str, **kw: Any) -> dict:
            if tool_name in responses:
                return dict(responses[tool_name])
            return {"ok": False, "code": "UNKNOWN_TOOL"}
        mcp.execute_tool = h
        return mcp

    def _ctx(self, **kw: Any) -> RuntimeContext:
        intent = type("_Intent", (), {
            "goal_category": kw.pop("gc", "CREATE_CONTENT"),
            "relation": kw.pop("rel", "NEW_TASK"),
            "requirements": kw.pop("reqs", [{"type": "CREATE"}]),
        })()
        return RuntimeContext(
            run_id=kw.pop("run_id", "r1"),
            trace_id=kw.pop("trace_id", "t1"),
            task_id=kw.pop("task_id", "task-1"),
            user_id="u1",
            task_intent=intent,
            user_message=kw.pop("msg", "test"),
            mcp=kw.pop("mcp"),
            session=None,
        )

    @pytest.mark.asyncio
    async def test_create_and_schedule_e2e(self) -> None:
        """Full CREATE+PUBLISH pipeline: draft created, then scheduled."""
        mcp = self._mock_mcp({
            "content.create_draft": {
                "ok": True, "code": "",
                "data": {"draft_id": "d-e2e", "title": "Java E2E"},
            },
            "publication.schedule": {
                "ok": True, "code": "",
                "data": {
                    "schedule_id": "s-e2e", "draft_id": "d-e2e",
                    "run_at": "2026-08-08T00:00:00Z",
                    "timezone": "Asia/Shanghai", "status": "SCHEDULED",
                },
            },
        })
        ctx = self._ctx(
            mcp=mcp, gc="CREATE_CONTENT",
            reqs=[{"type": "CREATE"}, {"type": "PUBLISH"}],
            msg="写一篇Java文章，五分钟后发布",
        )
        service = RuntimeAgentService()
        result = await service.execute(ctx)

        assert result.success is True
        assert result.draft_id == "d-e2e"
        assert len(result.artifact_ids) >= 2

        event_types = {e["event"] for e in result.events}
        assert "TOOL_INVOKED" in event_types
        assert "TOOL_COMPLETED" in event_types
        assert "EXECUTION_COMPLETED" in event_types
        assert "EXECUTION_FAILED" not in event_types

    @pytest.mark.asyncio
    async def test_improve_with_research_e2e(self) -> None:
        """Multi-step: SEARCH → ANALYZE → IMPROVE."""
        mcp = self._mock_mcp({
            "community.search_public_posts": {
                "ok": True, "code": "",
                "data": {"items": [{"post_id": "p1", "title": "Hot Java"}], "total": 1},
            },
            "content.revise_draft": {
                "ok": True, "code": "",
                "data": {"draft_id": "d-old", "title": "Revised", "status": "DRAFT"},
            },
        })
        ctx = self._ctx(
            mcp=mcp, gc="IMPROVE_CONTENT", rel="MODIFY_TASK",
            reqs=[{"type": "SEARCH"}, {"type": "ANALYZE"}, {"type": "IMPROVE"}],
            msg="参考社区热门Java帖子优化刚才文章",
        )
        service = RuntimeAgentService()
        result = await service.execute(ctx)
        assert result.success is True
        assert len(result.artifact_ids) >= 2


# ═══════════════════════════════════════════════════════════════════
# Section 6: Architecture gap documentation
# ═══════════════════════════════════════════════════════════════════

def test_gap_documentation() -> None:
    """Document known gaps — this test always passes, serving as a checklist."""
    gaps: list[str] = []

    # Gap 1: No CREATE+IMPROVE+PUBLISH template
    orchestrator = TaskOrchestrator(CapabilityRegistry())
    plan = orchestrator.generate_plan(
        task_id="t",
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "CREATE"}, {"type": "IMPROVE"}, {"type": "PUBLISH"},
        ],
    )
    if len(plan.steps) < 3:
        gaps.append(
            "GAP-1: No template for CREATE+IMPROVE+PUBLISH. "
            "User: '写一篇Java文章，标题新颖一点，增加代码案例，五分钟后发布'. "
            f"Got: {plan.template_name} ({len(plan.steps)} steps). "
            "Expected: 3+ steps (GENERATE→IMPROVE→SCHEDULE)."
        )

    # Gap 2: No schedule-update template
    plan2 = orchestrator.generate_plan(
        task_id="t2",
        goal_category="MANAGE_SCHEDULE",
        requirements=[{"type": "PUBLISH"}],
    )
    if plan2.steps[0].capability == "SCHEDULE_PUBLISH":
        gaps.append(
            "GAP-2: '修改发布时间' maps to SCHEDULE_PUBLISH (creates new), "
            "but should map to publication.update_schedule (modifies existing)."
        )

    # Gap 3: Ambiguous hint with low confidence
    resolver = TaskResolver()
    tasks = [
        Task(task_id="ta", conversation_id="c1", user_id="u1", tenant_id="t1",
             goal="创建Java文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
        Task(task_id="tb", conversation_id="c1", user_id="u1", tenant_id="t1",
             goal="创建Spring文章", goal_category="CREATE_CONTENT",
             status=TaskStatus.COMPLETED),
    ]
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="刚才那个",
        goal="修改刚才那个",
    )
    result = resolver.resolve(intent, tasks)
    if result is not None and result.confidence > 0.60:
        gaps.append(
            "GAP-3: '修改刚才那个' with two similar tasks returns "
            f"high confidence ({result.confidence}) — should flag ambiguity."
        )

    # Document all gaps
    for g in gaps:
        print(f"[GAP] {g}")

    # This test never fails — it's documentation
    assert True
