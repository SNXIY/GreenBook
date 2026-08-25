"""Phase 4 canonical TaskManager lifecycle tests."""

from __future__ import annotations

import pytest
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task import (
    InMemoryTaskRepository,
    TaskManager,
    TaskStatus,
)
from greenbook_agent_core.task.models import Objective, TaskExecutionRef


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
    assert {objective.objective_id for objective in task.objectives} == {
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
async def test_running_task_without_active_execution_continues_pending_objective() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="完成两个帖子",
    )
    task.objectives = [
        Objective(
            task_id=task.task_id,
            intent="第二个帖子",
            required_capabilities=["GENERATE_CONTENT"],
        )
    ]
    task.execution_refs = [
        TaskExecutionRef(
            execution_id="execution-1",
            task_id=task.task_id,
            status="COMPLETED",
        )
    ]
    task.status = TaskStatus.RUNNING
    task.active_execution_id = None
    await repository.update(task, expected_version=task.version)

    resumed = await manager.resume_task(task.task_id)

    assert resumed.status == TaskStatus.READY
    assert resumed.active_execution_id is None
    assert resumed.last_action == "RESUME"


@pytest.mark.asyncio
async def test_running_task_with_active_execution_stays_running() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="执行一个任务",
    )
    task = await manager.bind_execution(task.task_id, "execution-active", status="RUNNING")

    resumed = await manager.resume_task(task.task_id)

    assert resumed.status == TaskStatus.RUNNING
    assert resumed.active_execution_id == "execution-active"


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
async def test_completed_task_remains_resolvable_for_a_later_turn() -> None:
    """Completion ends an execution; it must not erase the task anchor."""
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="write Java article",
    )
    task = await manager.complete_task(task.task_id)

    candidates = await manager.get_resolvable_tasks(
        "c1",
        user_id="u1",
        tenant_id="t1",
    )

    assert [candidate.task_id for candidate in candidates] == [task.task_id]
    assert candidates[0].status == TaskStatus.COMPLETED


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


@pytest.mark.asyncio
async def test_failed_sibling_does_not_block_explicit_continuation() -> None:
    """A failed objective must not freeze an independent sibling mutation."""
    manager = TaskManager(InMemoryTaskRepository())
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two independent objectives",
            children=[
                Goal(goal_id="failed-sibling", description="write Java"),
                Goal(goal_id="surviving-sibling", description="write Agent"),
            ],
        )
    )
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=tree,
        root_goal=tree.root,
    )
    task = await manager.bind_execution(
        task.task_id,
        "execution-failed",
        goal_id="failed-sibling",
    )
    task = await manager.fail_task(task.task_id, error="provider failure")

    rebound = await manager.bind_execution(
        task.task_id,
        "execution-surviving",
        goal_id="surviving-sibling",
        status="RUNNING",
    )

    assert rebound.status == TaskStatus.RUNNING
    assert rebound.active_execution_id == "execution-surviving"


# ── wait_for_human pauses only the involved Goal (design goal 0813) ────────


@pytest.mark.asyncio
async def test_wait_for_human_pauses_only_target_goal() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two independent goals",
            children=[
                Goal(goal_id="g-a", description="write A"),
                Goal(goal_id="g-b", description="write B"),
            ],
        )
    )
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=tree,
        root_goal=tree.root_goal,
    )
    task = await manager.wait_for_human(
        task.task_id,
        reason="clarify A",
        goal_id="g-a",
    )
    statuses = {objective.objective_id: objective.status for objective in task.objectives}
    assert statuses["g-a"] == "WAITING"
    # The sibling Objective must not be reported as waiting.
    assert statuses["g-b"] != "WAITING"
    assert task.status == TaskStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_wait_for_human_without_goal_id_marks_all_goals() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two goals",
            children=[
                Goal(goal_id="g-a", description="write A"),
                Goal(goal_id="g-b", description="write B"),
            ],
        )
    )
    task = await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal_tree=tree,
        root_goal=tree.root_goal,
    )
    task = await manager.wait_for_human(task.task_id, reason="clarify")
    statuses = {objective.objective_id: objective.status for objective in task.objectives}
    assert statuses["g-a"] == "WAITING"
    assert statuses["g-b"] == "WAITING"
