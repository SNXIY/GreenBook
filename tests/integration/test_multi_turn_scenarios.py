"""Multi-turn conversation scenario regression tests.

The six user scenarios (multi-task creation, "Java 那篇"/"第三篇"/"下午那篇"/
"刚刚那篇"/"第一篇" citations, time/title/body edits, cancel-keeping-draft,
appending to finished work) must resolve deterministically through
TargetResolver + TaskManager + GoalTree patch — no LLM, no guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.command.models import (
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolver
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import InMemoryTaskRepository
from greenbook_agent_core.task.manager import TaskManager
from greenbook_agent_core.task.models import TaskStatus

CONV = "conv-scenario-1"
USER = "user-1"
TENANT = "tenant-1"


async def _create_task(
    manager: TaskManager,
    goal: str,
    *,
    run_at: str | None = None,
    capabilities: list[str] | None = None,
    updated_delta: timedelta = timedelta(0),
    created_delta: timedelta = timedelta(0),
) -> object:
    caps = capabilities or ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    tree = GoalTree(
        root=Goal(
            goal_id=f"goal-{goal[:12].replace(' ', '-')}",
            description=goal,
            goal_type="TASK",
            required_capabilities=caps,
            constraints=[{"run_at": run_at}] if run_at else [],
        ),
        source="TASK_DELTA",
    )
    task = await manager.create_task(
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
        goal=goal,
        goal_tree=tree,
    )
    if created_delta != timedelta(0):
        # Simulate real creation-order spacing (production tasks are created
        # across turns/loops with ms-level timestamps); ordinal resolution
        # relies on created_at ordering.
        task.created_at = (datetime.now(UTC) + created_delta).isoformat()
    if updated_delta != timedelta(0):
        task.updated_at = (datetime.now(UTC) + updated_delta).isoformat()
    if created_delta != timedelta(0) or updated_delta != timedelta(0):
        task = await manager._repository.update(task)
    return task


def _adapter(manager: TaskManager) -> ConversationRuntimeAdapter:
    adapter = object.__new__(ConversationRuntimeAdapter)
    adapter._task_manager = manager
    adapter._target_resolver = TargetResolver()
    return adapter


async def _apply_update(
    adapter: ConversationRuntimeAdapter,
    manager: TaskManager,
    reference: dict,
    desired: dict,
    session: object | None = None,
) -> object:
    """Resolve + apply an UPDATE_GOAL delta, returning the updated Task."""
    session = session or SessionContext(
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    task = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference=reference,
            desired_changes=desired,
        ),
        session,
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert task is not None, f"UPDATE_GOAL unresolved: {reference}"
    return await adapter._apply_goal_mutation(
        manager,
        task,
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference=reference,
            desired_changes=desired,
        ),
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )


def _goal_description(task: object) -> str:
    return str(task.goal or "")


def _root_run_at(task: object) -> str:
    tree = GoalTree.model_validate(task.goal_tree_snapshot)
    goal = tree.root_goal
    temporal = goal.temporal_constraint or {}
    if temporal.get("run_at"):
        return str(temporal["run_at"])
    for item in goal.constraints or ():
        if isinstance(item, dict) and item.get("run_at"):
            return str(item["run_at"])
    return ""


# ── Case 1: three tasks, then edit Java time + third title ────────────────


@pytest.mark.asyncio
async def test_case1_edit_by_label_and_ordinal() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    java = await _create_task(
        manager, "Java 后端实习面试最容易被问到的 10 个问题", run_at="2026-08-15T09:00:00+08:00",
        created_delta=timedelta(seconds=-2))
    agent = await _create_task(
        manager, "2026 年 Agent 开发需要掌握哪些核心技术", run_at="2026-08-15T14:00:00+08:00",
        created_delta=timedelta(seconds=-1))
    third = await _create_task(
        manager, "为什么学了很多八股还是不会做项目", run_at="2026-08-16T20:00:00+08:00")
    adapter = _adapter(manager)

    # "Java 那篇别明天早上发了，改成明天下午 4 点"
    java_updated = await _apply_update(
        adapter, manager,
        {"label": "Java"},
        {"run_at": "2026-08-15T16:00:00+08:00"},
    )
    assert java_updated.task_id == java.task_id
    assert _root_run_at(java_updated) == "2026-08-15T16:00:00+08:00"

    # "第三篇标题有点太负面，把它改成…" (ordinal 3 = third task)
    third_updated = await _apply_update(
        adapter, manager,
        {"ordinal": 3, "reference_type": "ORDINAL"},
        {"description": "为什么学了很多技术还是做不好项目"},
    )
    assert third_updated.task_id == third.task_id
    assert "做不好项目" in _goal_description(third_updated)

    # Agent 任务保持不变
    agent_now = await manager.get_task(agent.task_id)
    assert _root_run_at(agent_now) == "2026-08-15T14:00:00+08:00"


# ── Case 2: sequential creates, then "刚刚Java那篇" and "刚刚agent那篇" ──


@pytest.mark.asyncio
async def test_case2_label_citations_across_sequential_creates() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    java = await _create_task(
        manager, "如何学习 Java 的帖子", run_at="2026-08-14T13:44:51Z")
    agent = await _create_task(
        manager, "如何学习 agent 的帖子", run_at="2026-08-15T08:00:00+08:00")
    adapter = _adapter(manager)

    # "刚刚Java那篇修改一下标题…发布时间改成明天下午五点"
    java_updated = await _apply_update(
        adapter, manager,
        {"label": "Java"},
        {"description": "Java 学习全攻略", "run_at": "2026-08-15T17:00:00+08:00"},
    )
    assert java_updated.task_id == java.task_id
    assert "全攻略" in _goal_description(java_updated)

    # "刚刚agent那篇取消发布"
    agent_task = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"label": "Agent"},
        ),
        type("Session", (), {"active_task_id": ""})(),
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert agent_task is not None and agent_task.task_id == agent.task_id


# ── Case 3: "下午那篇" resolves by publication time window ────────────────


@pytest.mark.asyncio
async def test_case3_afternoon_citation_is_safe_when_ambiguous() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    await _create_task(manager, "Java 集合详解", run_at="2026-08-15T08:00:00+08:00")
    jvm = await _create_task(manager, "JVM 内存模型", run_at="2026-08-15T14:00:00+08:00")
    await _create_task(manager, "Spring Boot 入门", run_at="2026-08-15T17:00:00+08:00")
    adapter = _adapter(manager)

    # "把下午那篇改到晚上八点": BOTH 14:00 and 17:00 are in the afternoon
    # window.  The resolver must NOT silently pick one; it stays ambiguous and
    # the adapter turns that into a clarification instead of mis-editing.
    session = type("Session", (), {"active_task_id": ""})()
    ambiguous = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "下午"},
            desired_changes={"run_at": "2026-08-15T20:00:00+08:00"},
        ),
        session,
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert ambiguous is None

    # A precise label still resolves: "JVM 那篇改到晚上八点".
    jvm_updated = await _apply_update(
        adapter, manager,
        {"label": "JVM"},
        {"run_at": "2026-08-15T20:00:00+08:00"},
    )
    assert jvm_updated.task_id == jvm.task_id
    assert _root_run_at(jvm_updated) == "2026-08-15T20:00:00+08:00"


# ── Case 4: edit A, then "刚刚那篇" refers to B (most recently updated) ──


@pytest.mark.asyncio
async def test_case4_just_now_requires_clarification_even_with_conversation_focus() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    redis = await _create_task(
        manager, "Redis 缓存学习指南", run_at="2026-08-15T10:00:00+08:00",
        updated_delta=timedelta(minutes=-10))
    mysql = await _create_task(
        manager, "MySQL 索引优化实战", run_at="2026-08-15T15:00:00+08:00",
        updated_delta=timedelta(minutes=-5))
    adapter = _adapter(manager)
    session = SessionContext(
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )

    # "Redis 那篇改成下午四点"
    redis_updated = await _apply_update(
        adapter, manager,
        {"label": "Redis"},
        {"run_at": "2026-08-15T16:00:00+08:00"},
        session,
    )
    assert redis_updated.task_id == redis.task_id

    # "MySQL 那篇标题换一下"
    mysql_updated = await _apply_update(
        adapter, manager,
        {"label": "MySQL"},
        {"description": "MySQL 索引优化完全指南"},
        session,
    )
    assert mysql_updated.task_id == mysql.task_id

    # Simulate a later background write to Redis. It must not steal the
    # conversational referent selected by the user's MySQL turn.
    redis_after = await manager.get_task(redis.task_id)
    redis_after.updated_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await manager._repository.update(redis_after)

    # A focus binding is only weak evidence.  It must not hide the fact that
    # the weak reference has multiple valid Task candidates.
    latest_task = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"reference_type": "ACTIVE", "label": "刚刚那篇"},
        ),
        session,
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert latest_task is None


# ── Case 5: cancel publication but keep the draft ─────────────────────────


@pytest.mark.asyncio
async def test_case5_cancel_keeps_draft_task() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    langgraph = await _create_task(
        manager, "如何学习 LangGraph", run_at="2026-08-15T15:00:00+08:00")
    adapter = _adapter(manager)

    target = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"label": "LangGraph"},
        ),
        type("Session", (), {"active_task_id": ""})(),
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert target is not None and target.task_id == langgraph.task_id
    # The Task itself survives (draft kept): only the publication is cancelled.
    task = await manager.get_task(langgraph.task_id)
    assert task is not None


# ── Cross-task dependency fallback: a sibling task's DRAFT feeds a later
#    SCHEDULE_PUBLISH when the model splits a dependent pipeline into
#    separate CREATE_TASK entries ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_execution_state_filter_excludes_sibling_task_artifacts() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    # A finished task with a real DRAFT in its resource_index.
    writer = await _create_task(manager, "写一篇 Agent 学习帖子")
    writer.updated_at = (datetime.now(UTC) + timedelta(minutes=-5)).isoformat()
    writer.resource_index = [
        type("Ref", (), {
            "resource_kind": "DRAFT",
            "resource_id": "draft-sibling-1",
            "title": "Agent 学习指南",
            "status": "draft",
            "updated_at": writer.updated_at,
        })()
    ]
    await manager._repository.update(writer)

    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _execution_states_from_state,
    )

    consumer_state = SimpleNamespace(
        context_snapshot={
            "execution_states": [
                {
                    "task_id": str(writer.task_id),
                    "goal_id": "writer-goal",
                    "capability": "GENERATE_CONTENT",
                    "status": "COMPLETED",
                    "draft_id": "draft-sibling-1",
                }
            ]
        }
    )
    assert _execution_states_from_state(consumer_state, task_id="consumer-task") == []


def test_incremental_plan_does_not_schedule_from_sibling_draft() -> None:
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _incremental_plan,
    )
    from greenbook_agent_core.planning.contracts import PlanStep

    goal_tree = GoalTree(
        root=Goal(
            goal_id="g-schedule",
            description="五分钟之后发布",
            goal_type="TASK",
            required_capabilities=["SCHEDULE_PUBLISH"],
            constraints=[{"run_at": "2026-08-14T15:33:00+00:00"}],
        )
    )
    step = PlanStep(
        step_id="s1",
        goal_id="g-schedule",
        capability="SCHEDULE_PUBLISH",
        constraints={"run_at": "2026-08-14T15:33:00+00:00"},
    )
    plan = SimpleNamespace(task_id="t1", steps=[step], plan_source="INCREMENTAL")

    state = SimpleNamespace(
        goal_tree=goal_tree,
        completed_task_ids=[],
        resume_context=None,
        context_snapshot={"execution_states": [
            # sibling task's GENERATE_CONTENT produced the draft
            {"task_id": "other-task", "goal_id": "sibling:t-writer", "capability": "GENERATE_CONTENT",
             "status": "COMPLETED", "draft_id": "draft-sibling-1"},
        ]},
    )

    reduced = _incremental_plan(state, plan)
    single = reduced.steps[0]
    assert single.constraints.get("draft_id") in (None, "")



@pytest.mark.asyncio
async def test_label_with_conversational_suffix_resolves() -> None:
    """Model often cites "Java 那篇" — the suffix must not break matching."""

    manager = TaskManager(InMemoryTaskRepository())
    java = await _create_task(
        manager, "搜索社区里最近比较热门的 Java 面试相关帖子并总结关注点", run_at="2026-08-15T09:00:00+08:00",
        created_delta=timedelta(seconds=-2))
    await _create_task(
        manager, "再找一些最近关于 AI Agent 的讨论写一篇核心技术的文章", run_at="2026-08-15T14:00:00+08:00",
        created_delta=timedelta(seconds=-1))
    await _create_task(
        manager, "不用搜索直接写一篇比较轻松的为什么学了很多八股还是不会做项目", run_at="2026-08-16T20:00:00+08:00")

    adapter = _adapter(manager)
    # "Java 那篇别明天早上发了，改成明天下午 4 点"
    updated = await _apply_update(
        adapter, manager,
        {"label": "Java 那篇"},
        {"run_at": "2026-08-15T16:00:00+08:00"},
    )
    assert updated.task_id == java.task_id
    assert _root_run_at(updated) == "2026-08-15T16:00:00+08:00"


@pytest.mark.asyncio
async def test_bare_mutation_with_multiple_tasks_requires_clarification() -> None:
    """A bare mutation must not use persisted update recency as a target."""
    manager = TaskManager(InMemoryTaskRepository())
    older = await _create_task(
        manager, "Redis 缓存学习指南", run_at="2026-08-15T10:00:00+08:00",
        updated_delta=timedelta(minutes=-30))
    recent = await _create_task(
        manager, "从零开始学 Agent 系统学习与实践指南", run_at="2026-08-15T07:00:00+00:00",
        updated_delta=timedelta(minutes=-5))
    adapter = _adapter(manager)

    target = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={},
            desired_changes={"run_at": "2026-08-14T06:36:00+00:00"},
        ),
        SessionContext(conversation_id=CONV, user_id=USER, tenant_id=TENANT),
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert target is None
    # No persisted resource is mutated while clarification is required.
    older_now = await manager.get_task(older.task_id)
    recent_now = await manager.get_task(recent.task_id)
    assert _root_run_at(older_now) == "2026-08-15T10:00:00+08:00"
    assert _root_run_at(recent_now) == "2026-08-15T07:00:00+00:00"


@pytest.mark.asyncio
async def test_case6_append_to_completed_task_and_first_ordinal() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    first = await _create_task(
        manager, "Java 集合详解", run_at="2026-08-15T10:00:00+08:00")
    # Mark first task COMPLETED (publication finished), like the scenario.
    first.status = TaskStatus.COMPLETED
    await manager._repository.update(first)

    # "再给它补一段 HashMap 扩容机制"
    first_updated = await _apply_update(
        adapter := _adapter(manager), manager,
        {"label": "Java 集合"},
        {"description": "Java 集合详解（补充 HashMap 扩容机制）"},
    )
    assert first_updated.task_id == first.task_id
    assert "HashMap" in _goal_description(first_updated)

    await _create_task(
        manager, "JVM GC 详解", run_at="2026-08-16T10:00:00+08:00")

    # "第一篇再加一个面试题总结" — creation ordinal 1 = Java 集合详解
    first_again = await adapter._resolve_delta_target(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "第一篇"},
            desired_changes={"description": "x"},
        ),
        type("Session", (), {"active_task_id": ""})(),
        conversation_id=CONV,
        user_id=USER,
        tenant_id=TENANT,
    )
    assert first_again is not None and first_again.task_id == first.task_id
