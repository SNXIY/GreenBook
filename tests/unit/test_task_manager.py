"""Phase 4 canonical TaskManager lifecycle tests."""

from __future__ import annotations

import pytest
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import (
    InMemoryTaskRepository,
    TaskManager,
    TaskStatus,
)


def _tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="publish_article",
            description="研究、写作并发布文章",
            children=[
                Goal(goal_id="research", description="研究主题"),
                Goal(
                    goal_id="write",
                    description="生成文章",
                    dependencies=["research"],
                ),
            ],
        )
    )


@pytest.mark.asyncio
async def test_single_task_single_goal_and_lifecycle() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal=Goal(goal_id="g1", description="创建 Java 学习文章"),
    )

    assert task.status == TaskStatus.CREATED
    task = await manager.bind_goal_tree(
        task.task_id,
        GoalTree(root=Goal(goal_id="g1", description="创建 Java 学习文章")),
    )
    assert task.status == TaskStatus.READY
    task = await manager.bind_execution(task.task_id, "execution-1")
    assert task.status == TaskStatus.RUNNING
    task = await manager.pause_task(task.task_id)
    assert task.status == TaskStatus.PAUSED
    task = await manager.resume_task(task.task_id)
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_one_task_can_hold_multiple_goals() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=_tree(),
    )

    assert task.root_goal_id == "publish_article"
    assert {goal.goal_id for goal in task.goals} == {
        "publish_article",
        "research",
        "write",
    }
    assert task.goal_tree_version == 1
    assert task.plan_version == 1


@pytest.mark.asyncio
async def test_independent_task_preempts_and_resumes_active_task() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task_a = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="后台发布任务",
        priority=1,
    )
    task_a = await manager.bind_execution(task_a.task_id, "execution-a")
    task_b = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="用户前台查询",
        priority=10,
    )

    paused = await manager.preempt_for(task_a.task_id, task_b.priority)
    assert paused.status == TaskStatus.PAUSED
    resumed = await manager.resume_task(paused.task_id)
    assert resumed.status == TaskStatus.RUNNING
    assert resumed.active_execution_id == "execution-a"


@pytest.mark.asyncio
async def test_replan_increments_plan_version_without_overwriting_history() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="分析 AI 趋势",
    )
    task = await manager.bind_goal_tree(
        task.task_id,
        GoalTree(root=Goal(goal_id="g1", description="分析 AI 趋势")),
    )
    task = await manager.record_replan(
        task.task_id,
        decision="RETRY_WITH_NEW_ARGS",
        observation={"ok": False, "error_code": "NO_RESULTS"},
        reason="调整查询范围",
    )
    assert task.plan_version == 2
    assert len(task.plan_history) == 1
    assert task.plan_history[0].previous_plan_version == 1


@pytest.mark.asyncio
async def test_completed_task_can_be_revised_in_the_same_conversation() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=GoalTree(
            root=Goal(goal_id="write-java", description="写一篇 Java 文章")
        ),
    )
    task = await manager.bind_execution(task.task_id, "execution-1")
    task = await manager.complete_task(task.task_id)

    revised = await manager.bind_goal_tree(
        task.task_id,
        GoalTree(
            root=Goal(
                goal_id="revise-java",
                description="改成 Java 面试方向并安排发布",
            )
        ),
    )

    assert revised.task_id == task.task_id
    assert revised.status == TaskStatus.READY
    assert revised.goal_tree_version == 2
    assert revised.plan_version == 2
    assert revised.plan_history[-1].decision == "MODIFY_GOAL"
    assert revised.completed_at is None


@pytest.mark.asyncio
async def test_failed_task_can_be_revised_and_rebound_to_new_execution() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=GoalTree(
            root=Goal(goal_id="write-agent", description="鍐欎竴绡?Agent 鏂囩珷")
        ),
    )
    task = await manager.bind_execution(task.task_id, "execution-1")
    task = await manager.fail_task(task.task_id, error="revision input missing")

    revised = await manager.bind_goal_tree(
        task.task_id,
        GoalTree(
            root=Goal(
                goal_id="revise-agent",
                description="澧炲姞 LangGraph 鍜?MCP 瀹炴垬妗堜緥",
            )
        ),
    )
    rebound = await manager.bind_execution(revised.task_id, "execution-2")

    assert revised.status == TaskStatus.READY
    assert revised.plan_version == 2
    assert rebound.status == TaskStatus.RUNNING
    assert rebound.active_execution_id == "execution-2"
