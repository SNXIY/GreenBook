"""Phase 5.5: GAP fix tests — CREATE_AND_IMPROVE, Schedule Mutation, Ambiguity."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.task.models import (
    Task,
    TaskIntent,
    TaskStatus,
)
from greenbook_assistant_core.task.resolver import TaskResolver


# ── helpers ──────────────────────────────────────────────────────

def _mock_mcp(responses: dict[str, dict]) -> AsyncMock:
    mcp = AsyncMock()
    async def h(tool_name: str, **kw: Any) -> dict:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL"}
    mcp.execute_tool = h
    return mcp


def _make_task(task_id: str, goal: str, category: str = "CREATE_CONTENT") -> Task:
    return Task(task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
                goal=goal, goal_category=category, status=TaskStatus.COMPLETED)


# ═══════════════════════════════════════════════════════════════════
# GAP-1: CREATE_AND_IMPROVE
# ═══════════════════════════════════════════════════════════════════

class TestCreateAndImprove:
    def test_template_selection(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1", goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}, {"type": "IMPROVE"}],
        )
        assert plan.template_name == "CREATE_AND_IMPROVE"
        assert len(plan.steps) == 2
        caps = [s.capability for s in plan.steps]
        assert caps == ["GENERATE_CONTENT", "IMPROVE_CONTENT"]

    def test_dag_dependencies(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1", goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}, {"type": "IMPROVE"}],
        )
        s1, s2 = plan.steps
        assert s2.depends_on == [s1.step_id]
        assert "DRAFT" in s2.input_artifact_types
        assert s1.output_artifact_type == "DRAFT"

    @pytest.mark.asyncio
    async def test_e2e_create_and_improve(self) -> None:
        mcp = _mock_mcp({
            "content.create_draft": {
                "ok": True, "code": "",
                "data": {"draft_id": "d1", "title": "Java Draft"},
            },
            "content.revise_draft": {
                "ok": True, "code": "",
                "data": {"draft_id": "d1", "title": "Java Improved"},
            },
        })
        intent = type("_Intent", (), {
            "goal_category": "CREATE_CONTENT", "relation": "NEW_TASK",
            "requirements": [{"type": "CREATE"}, {"type": "IMPROVE"}],
        })()
        ctx = RuntimeContext(
            run_id="r1", trace_id="t1", task_id="task-1", user_id="u1",
            task_intent=intent, user_message="写一篇Java文章，标题新颖一点",
            mcp=mcp, session=None,
        )
        service = RuntimeAgentService()
        result = await service.execute(ctx)
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════
# GAP-2: Schedule Mutation
# ═══════════════════════════════════════════════════════════════════

class TestScheduleMutation:
    def test_update_requirement_selects_manage_template(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1", goal_category="MANAGE_SCHEDULE",
            requirements=[{"type": "UPDATE"}],
        )
        assert plan.template_name == "SINGLE_MANAGE_SCHEDULE"
        assert plan.steps[0].capability == "MANAGE_SCHEDULE"

    def test_update_maps_to_manage_schedule_capability(self) -> None:
        registry = CapabilityRegistry()
        match = registry.resolve_requirement({"type": "UPDATE"})
        assert match.capability is not None
        assert match.capability.name == "MANAGE_SCHEDULE"
        assert "publication.update_schedule" in match.capability.tools

    def test_manage_schedule_needs_schedule_artifact(self) -> None:
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        plan = orchestrator.generate_plan(
            task_id="t1", goal_category="MANAGE_SCHEDULE",
            requirements=[{"type": "UPDATE"}],
        )
        step = plan.steps[0]
        assert "SCHEDULE" in step.input_artifact_types
        assert step.output_artifact_type == "SCHEDULE"

    def test_publish_still_creates_new_schedule(self) -> None:
        """PUBLISH requirement → creates new, UPDATE → modifies existing."""
        orchestrator = TaskOrchestrator(CapabilityRegistry())
        # PUBLISH → creates new
        plan_create = orchestrator.generate_plan(
            task_id="t1", goal_category="CREATE_CONTENT",
            requirements=[{"type": "PUBLISH"}],
        )
        assert plan_create.steps[0].capability == "SCHEDULE_PUBLISH"

        # UPDATE → modifies existing
        plan_update = orchestrator.generate_plan(
            task_id="t1", goal_category="MANAGE_SCHEDULE",
            requirements=[{"type": "UPDATE"}],
        )
        assert plan_update.steps[0].capability == "MANAGE_SCHEDULE"

    @pytest.mark.asyncio
    async def test_e2e_update_schedule(self) -> None:
        mcp = _mock_mcp({
            "publication.update_schedule": {
                "ok": True, "code": "",
                "data": {"schedule_id": "s1", "status": "SCHEDULED",
                         "run_at": "2026-08-07T21:00:00Z", "draft_id": "d1"},
            },
        })
        intent = type("_Intent", (), {
            "goal_category": "MANAGE_SCHEDULE", "relation": "MODIFY_TASK",
            "requirements": [{"type": "UPDATE"}],
            "target_task_id": None, "target_task_hint": None,
        })()
        ctx = RuntimeContext(
            run_id="r2", trace_id="t2", task_id="task-1", user_id="u1",
            task_intent=intent, user_message="把发布时间改成晚上9点",
            mcp=mcp, session=None,
        )
        service = RuntimeAgentService()
        result = await service.execute(ctx)
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════
# GAP-3: Ambiguous Reference
# ═══════════════════════════════════════════════════════════════════

class TestAmbiguousReference:
    def test_temporal_hint_with_multiple_same_category_is_ambiguous(self) -> None:
        tasks = [
            _make_task("task-b", "创建Spring文章", "CREATE_CONTENT"),
            _make_task("task-a", "创建Java文章", "CREATE_CONTENT"),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="刚才那个",
            goal="修改刚才那个",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)

        assert result is not None
        assert result.is_ambiguous is True
        assert result.confidence <= 0.40
        assert len(result.candidates) >= 1

    def test_temporal_single_task_is_not_ambiguous(self) -> None:
        tasks = [_make_task("task-a", "创建Java文章", "CREATE_CONTENT")]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="刚才那个",
            goal="修改刚才那个",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        assert result.is_ambiguous is False

    def test_label_hint_with_multiple_is_still_deterministic(self) -> None:
        """'Java文章' is a content hint, not temporal → no ambiguity flag."""
        tasks = [
            _make_task("task-b", "创建Spring文章", "CREATE_CONTENT"),
            _make_task("task-a", "创建Java文章", "CREATE_CONTENT"),
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
        assert result.is_ambiguous is False  # content hint resolves clearly

    def test_different_categories_not_ambiguous(self) -> None:
        """Different categories → no ambiguity even for temporal hints."""
        tasks = [
            _make_task("task-a", "创建Java文章", "CREATE_CONTENT"),
            Task(task_id="task-b", conversation_id="c1", user_id="u1",
                 tenant_id="t1", goal="搜索Java帖子",
                 goal_category="ANALYZE_COMMUNITY", status=TaskStatus.COMPLETED),
        ]
        intent = TaskIntent(
            relation="MODIFY_TASK",
            goal_category="IMPROVE_CONTENT",
            target_task_hint="刚才那个",
            goal="修改刚才那个",
        )
        resolver = TaskResolver()
        result = resolver.resolve(intent, tasks)
        assert result is not None
        # Different categories → only 1 task per category → not ambiguous
        assert result.is_ambiguous is False
