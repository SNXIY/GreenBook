"""Phase 5.6 tests — ResourceResolver and resource_requests in TaskIntent."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.resource.models import (
    ResourceOperation,
    ResourceRequest,
    ResourceTarget,
    ResourceType,
)
from greenbook_assistant_core.resource.resolver import ResourceResolver
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    Task,
    TaskIntent,
    TaskStatus,
)
from greenbook_assistant_core.task.understanding import TaskUnderstanding


# ── helpers ──────────────────────────────────────────────────────

def _task(task_id: str, goal: str, category: str = "CREATE_CONTENT",
          artifacts: list[ArtifactRef] | None = None) -> Task:
    return Task(
        task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
        goal=goal, goal_category=category, status=TaskStatus.COMPLETED,
        artifacts=artifacts or [],
    )


def _java_task_with_schedule(schedule_id: str = "sched-a") -> Task:
    return _task("task-a", "创建Java文章", "CREATE_CONTENT", artifacts=[
        ArtifactRef(artifact_id="art-d1", task_id="task-a",
                    artifact_type="DRAFT", resource_id="draft-a",
                    resource_kind="DRAFT", summary="Java文章草稿"),
        ArtifactRef(artifact_id="art-s1", task_id="task-a",
                    artifact_type="SCHEDULE", resource_id=schedule_id,
                    resource_kind="SCHEDULE", summary="Java文章定时发布"),
    ])


def _python_task_with_schedule(schedule_id: str = "sched-b") -> Task:
    return _task("task-b", "创建Python文章", "CREATE_CONTENT", artifacts=[
        ArtifactRef(artifact_id="art-d2", task_id="task-b",
                    artifact_type="DRAFT", resource_id="draft-b",
                    resource_kind="DRAFT", summary="Python文章草稿"),
        ArtifactRef(artifact_id="art-s2", task_id="task-b",
                    artifact_type="SCHEDULE", resource_id=schedule_id,
                    resource_kind="SCHEDULE", summary="Python文章定时发布"),
    ])


# ═══════════════════════════════════════════════════════════════════
# Case 1: CREATE new resource does NOT pollute historical resources
# ═══════════════════════════════════════════════════════════════════

class TestCreateNewResource:
    def test_create_schedule_when_old_exists(self) -> None:
        """已有 schedule-a, 用户创建新文章 → 创建新 schedule, 不修改旧的."""
        tasks = [_java_task_with_schedule("sched-a")]
        requests = [
            ResourceRequest(operation=ResourceOperation.CREATE,
                            resource_type=ResourceType.CONTENT_DRAFT),
            ResourceRequest(operation=ResourceOperation.CREATE,
                            resource_type=ResourceType.SCHEDULE),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        assert len(result.targets) == 2
        draft_target = result.targets[0]
        assert draft_target.operation == ResourceOperation.CREATE
        assert draft_target.resource_id is None  # will be created

        schedule_target = result.targets[1]
        assert schedule_target.operation == ResourceOperation.CREATE
        assert schedule_target.resource_id is None  # will be created, NOT old one
        assert result.needs_clarification is False

    @pytest.mark.asyncio
    async def test_l1_produces_create_for_new_task(self) -> None:
        """'帮我写一篇Python文章，晚上8点发布' → CREATE for both DRAFT and SCHEDULE."""
        tu = TaskUnderstanding()
        intent = await tu.understand("帮我写一篇Python文章，晚上8点发布")
        assert intent.relation == "NEW_TASK"
        reqs = intent.resource_requests
        ops = {(r["operation"], r["resource_type"]) for r in reqs}
        assert ("CREATE", "CONTENT_DRAFT") in ops
        assert ("CREATE", "SCHEDULE") in ops
        # No UPDATE operations
        assert not any(r["operation"] == "UPDATE" for r in reqs)


# ═══════════════════════════════════════════════════════════════════
# Case 2: Explicit UPDATE finds the correct target
# ═══════════════════════════════════════════════════════════════════

class TestUpdateExplicitTarget:
    def test_update_schedule_by_label(self) -> None:
        """'Java文章' hint → resolve to task-a → find schedule-a."""
        tasks = [_java_task_with_schedule("sched-a"),
                 _python_task_with_schedule("sched-b")]
        requests = [
            ResourceRequest(operation=ResourceOperation.UPDATE,
                            resource_type=ResourceType.SCHEDULE,
                            hint="Java文章"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        assert len(result.targets) == 1
        target = result.targets[0]
        assert target.operation == ResourceOperation.UPDATE
        assert target.resource_id == "sched-a"
        assert target.task_id == "task-a"
        assert target.is_ambiguous is False

    def test_update_schedule_by_task_id(self) -> None:
        """Explicit task_id → finds the schedule on that task."""
        tasks = [_java_task_with_schedule("sched-a"),
                 _python_task_with_schedule("sched-b")]
        requests = [
            ResourceRequest(operation=ResourceOperation.UPDATE,
                            resource_type=ResourceType.SCHEDULE,
                            task_id="task-b"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        assert result.targets[0].resource_id == "sched-b"
        assert result.targets[0].task_id == "task-b"


# ═══════════════════════════════════════════════════════════════════
# Case 3: Ambiguous reference → needs_clarification
# ═══════════════════════════════════════════════════════════════════

class TestAmbiguousReference:
    def test_temporal_hint_with_multiple_schedules_is_ambiguous(self) -> None:
        """'修改刚才那个发布时间' with two schedules → ambiguous."""
        tasks = [_java_task_with_schedule("sched-a"),
                 _python_task_with_schedule("sched-b")]
        requests = [
            ResourceRequest(operation=ResourceOperation.UPDATE,
                            resource_type=ResourceType.SCHEDULE,
                            hint="刚才那个"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        assert result.needs_clarification is True
        target = result.targets[0]
        assert target.is_ambiguous is True
        assert len(target.candidates) >= 1

    def test_no_schedule_found(self) -> None:
        """UPDATE schedule on task without schedule → no_artifact_in_task."""
        tasks = [_task("task-a", "创建Java文章", artifacts=[
            ArtifactRef(artifact_id="a1", task_id="task-a",
                        artifact_type="DRAFT", resource_id="draft-a",
                        resource_kind="DRAFT"),
        ])]
        requests = [
            ResourceRequest(operation=ResourceOperation.UPDATE,
                            resource_type=ResourceType.SCHEDULE,
                            hint="Java文章"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        target = result.targets[0]
        assert target.match_reason == "no_artifact_in_task"
        assert target.resource_id is None


# ═══════════════════════════════════════════════════════════════════
# Case 4: NEW_TASK always creates, never updates
# ═══════════════════════════════════════════════════════════════════

class TestNewTaskAlwaysCreates:
    def test_create_schedule_even_with_existing(self) -> None:
        """NEW_TASK with schedule requirement → CREATE, not UPDATE."""
        tasks = [_java_task_with_schedule("sched-a")]  # old schedule exists
        requests = [
            ResourceRequest(operation=ResourceOperation.CREATE,
                            resource_type=ResourceType.SCHEDULE),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)

        assert result.targets[0].operation == ResourceOperation.CREATE
        assert result.targets[0].resource_id is None  # new, not sched-a

    @pytest.mark.asyncio
    async def test_l1_create_with_schedule_is_new_task(self) -> None:
        """'创建一篇新的Go文章，明天发布' → NEW_TASK + CREATE schedule."""
        tu = TaskUnderstanding()
        intent = await tu.understand("创建一篇新的Go文章，明天发布")
        assert intent.relation == "NEW_TASK"
        reqs = intent.resource_requests
        schedule_reqs = [r for r in reqs if r["resource_type"] == "SCHEDULE"]
        assert len(schedule_reqs) >= 1
        assert schedule_reqs[0]["operation"] == "CREATE"


# ═══════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_delete_schedule(self) -> None:
        """DELETE operation with hint → finds target."""
        tasks = [_java_task_with_schedule("sched-a")]
        requests = [
            ResourceRequest(operation=ResourceOperation.DELETE,
                            resource_type=ResourceType.SCHEDULE,
                            hint="Java文章"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)
        assert result.targets[0].resource_id == "sched-a"
        assert result.targets[0].operation == ResourceOperation.DELETE

    def test_update_draft_by_label(self) -> None:
        """'修改Java文章' → UPDATE CONTENT_DRAFT, finds draft-a."""
        tasks = [_java_task_with_schedule("sched-a")]
        requests = [
            ResourceRequest(operation=ResourceOperation.UPDATE,
                            resource_type=ResourceType.CONTENT_DRAFT,
                            hint="Java文章"),
        ]
        resolver = ResourceResolver()
        result = resolver.resolve(requests, tasks)
        assert result.targets[0].resource_id == "draft-a"

    def test_empty_requests_returns_empty(self) -> None:
        resolver = ResourceResolver()
        result = resolver.resolve([], [])
        assert len(result.targets) == 0
        assert result.needs_clarification is False

    @pytest.mark.asyncio
    async def test_l1_modify_produces_update_requests(self) -> None:
        """'修改Java文章标题' → UPDATE on CONTENT_DRAFT."""
        tu = TaskUnderstanding()
        intent = await tu.understand("修改Java文章标题",
                                      existing_tasks=[
                                          {"task_id": "t1", "goal": "创建Java文章",
                                           "goal_category": "CREATE_CONTENT"},
                                      ])
        assert intent.relation == "MODIFY_TASK"
        reqs = intent.resource_requests
        draft_reqs = [r for r in reqs if r["resource_type"] == "CONTENT_DRAFT"]
        assert len(draft_reqs) >= 1
        assert draft_reqs[0]["operation"] == "UPDATE"
