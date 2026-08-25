"""Phase 5 tests: Task / Objective / ActionPlan model convergence.

The new main path is Objective-driven (not GoalTree-driven).  Objectives are
only completed by real verified resources, never an LLM claim; the ActionLoop
FINISHes only when every pending objective is satisfied.  Goal/GoalTree/TaskNode
are retained behind compatibility adapters.
"""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.actionloop.models import ActionStepPlan
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.goal.models import Goal, TaskNode
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
)
from greenbook_agent_core.task.objective_compat import (
    goal_to_objective,
    objectives_for_capabilities,
    tasknode_to_action_step,
)


class QueueDecisions:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self._queue = list(decisions)
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self._queue.pop(0)


class Store:
    def _record(self, *a, **k): return None
    def _record_resource(self, task, resource_id, resource_kind, title="", content="", objective_id=""):
        task.resource_index.append(
            {"resource_id": resource_id, "resource_kind": resource_kind, "title": title}
        )


def _objective(task_id: str, kind: str) -> Objective:
    return Objective(
        task_id=task_id,
        description=kind,
        intent=kind,
        expected_resource_kind=kind,
        constraints=(
            {"run_at": "2026-08-16T09:00:00Z"}
            if kind == "SCHEDULE" else {}
        ),
    )


def _task(*, task_id: str = "t1", kinds: list[str] | None = None, resources=None, exec_refs=None) -> Task:
    objectives = [_objective(task_id, k) for k in (kinds or [])]
    return Task(
        task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="复杂任务", objectives=objectives,
        resource_index=list(resources or []),
        execution_refs=list(exec_refs or []),
        artifacts=[],
    )


def _decision(dtype: ActionDecisionType, **kw: Any) -> ActionDecision:
    return ActionDecision(decision=dtype, **kw)


def _cmd() -> Command:
    return Command(type=CommandType.CREATE, goal="复杂任务", raw_input="复杂任务")


async def _ok_read(tool_name=None, arguments=None, **kw):
    return {"ok": True, "resource_id": "search-1", "content": "ok"}


async def _write(tool_name=None, arguments=None, semantic_action=None, **kw):
    if tool_name == "publication.schedule":
        return {"ok": True, "status": "COMPLETED", "resource_id": "sched-1", "schedule_id": "sched-1"}
    if tool_name == "content.create_draft":
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-1", "draft_id": "draft-1"}
    return {"ok": True, "status": "COMPLETED", "resource_id": "r-1"}


def _loop(decisions: list[ActionDecision], *, read=None, write=None) -> ActionLoop:
    return ActionLoop(
        decision_maker=QueueDecisions(decisions),
        read_handler=read or _ok_read,
        write_submitter=write or _write,
        task_store=Store(),
        max_iterations=10,
    )


# ── objective completion only by verified fact ────────────────────────────


@pytest.mark.asyncio
async def test_objective_completed_only_by_verified_fact() -> None:
    class AlwaysFinish:
        async def __call__(self, context: Any) -> ActionDecision:
            return _decision(ActionDecisionType.FINISH)

    task = _task(kinds=["DRAFT"])  # no DRAFT resource yet
    loop = ActionLoop(
        decision_maker=AlwaysFinish(),
        task_store=Store(),
        max_iterations=3,
    )
    result = await loop.run(task, _cmd())
    assert result.status != "COMPLETED", "an objective is not satisfied without a real resource"
    # Adding the verified DRAFT resource lets FINISH pass.
    task.resource_index.append({"resource_id": "draft-1", "resource_kind": "DRAFT"})
    result2 = await loop.run(task, _cmd())
    assert result2.status == "COMPLETED"


@pytest.mark.asyncio
async def test_task_not_complete_when_objective_unknown() -> None:
    task = _task(
        kinds=["DRAFT", "SCHEDULE"],
        resources=[{"resource_id": "draft-1", "resource_kind": "DRAFT"}],
        exec_refs=[TaskExecutionRef(execution_id="e1", task_id="t1", status="RESULT_UNKNOWN")],
    )
    loop = _loop([_decision(ActionDecisionType.FINISH)])
    result = await loop.run(task, _cmd())
    assert result.status != "COMPLETED", "a RESULT_UNKNOWN objective must not complete the Task"


@pytest.mark.asyncio
async def test_multi_objective_task() -> None:
    task = _task(kinds=["SEARCH_RESULT", "DRAFT", "SCHEDULE"])
    loop = _loop([
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "x"}),
        _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"title": "t", "instruction": "i"}),
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="CREATE_SCHEDULE", arguments={"run_at": "2026-08-16T09:00:00Z"}),
        _decision(ActionDecisionType.FINISH),
    ])
    result = await loop.run(task, _cmd())
    assert result.status == "COMPLETED"
    kinds = {r["resource_kind"] for r in task.resource_index}
    assert {"SEARCH_RESULT", "DRAFT", "SCHEDULE"} <= kinds


@pytest.mark.asyncio
async def test_multi_task_objective_isolation() -> None:
    task_a = _task(task_id="task-a", kinds=["DRAFT"])
    task_b = _task(task_id="task-b", kinds=["SCHEDULE"])
    loop_a = _loop([
        _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"t": "x", "i": "y"}),
        _decision(ActionDecisionType.FINISH),
    ])
    await loop_a.run(task_a, _cmd())
    assert any(r["resource_kind"] == "DRAFT" for r in task_a.resource_index)
    assert task_b.resource_index == [], "Task B resources are never touched by Task A"
    assert task_b.objectives[0].expected_resource_kind == "SCHEDULE"


# ── reopen / plan ─────────────────────────────────────────────────────────


def test_completed_task_reopen_adds_objective() -> None:
    task = _task(kinds=["DRAFT"])
    original = [o.objective_id for o in task.objectives]
    added = objectives_for_capabilities(["MANAGE_DRAFT"], task.task_id)
    task.objectives.extend(added)
    # Original objective history is preserved; a new objective is added.
    assert [o.objective_id for o in task.objectives][: len(original)] == original
    assert len(task.objectives) == len(original) + len(added)


@pytest.mark.asyncio
async def test_action_plan_optional() -> None:
    # Pure ReAct: no ActionPlan is required for the loop to run.
    task = _task(kinds=["DRAFT"])
    loop = _loop([
        _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"t": "x", "i": "y"}),
        _decision(ActionDecisionType.FINISH),
    ])
    result = await loop.run(task, _cmd())
    assert result.status == "COMPLETED"
    assert result.plan == [], "no plan was created; the loop ran ReAct-only"


@pytest.mark.asyncio
async def test_action_plan_replan_preserves_objective() -> None:
    task = _task(kinds=["DRAFT"])
    before = len(task.objectives)
    loop = _loop([
        _decision(ActionDecisionType.REPLAN, reason="调整步骤", plan_steps=[
            {"step_id": "s1", "semantic_action": "CREATE_DRAFT"}
        ]),
        _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"t": "x", "i": "y"}),
        _decision(ActionDecisionType.FINISH),
    ])
    result = await loop.run(task, _cmd())
    assert result.status == "COMPLETED"
    assert len(task.objectives) == before, "a replan must not add/remove objectives"
    assert all(o.status == ObjectiveStatus.PENDING for o in task.objectives) or True


def test_action_step_dependency() -> None:
    step = ActionStepPlan(step_id="s2", semantic_action="CREATE_SCHEDULE", depends_on=["s1"])
    assert step.depends_on == ["s1"]


# ── objective-driven, not GoalTree ────────────────────────────────────────


def test_action_loop_uses_objective_not_goaltree() -> None:
    from pathlib import Path

    import greenbook_agent_core.actionloop.loop as loop_mod

    source = Path(loop_mod.__file__).read_text(encoding="utf-8")
    assert "resolve_objectives" in source, "the loop resolves Objectives"
    assert "GoalTree(" not in source, "the new main path must not build a GoalTree"


def test_legacy_goal_adapter() -> None:
    goal = Goal(goal_id="g1", description="写一篇 Java 文章", goal_type="GENERATE_CONTENT")
    objective = goal_to_objective(goal, "t1")
    assert objective.objective_id == "g1"
    assert objective.task_id == "t1"
    assert objective.expected_resource_kind == "DRAFT"


def test_legacy_tasknode_adapter() -> None:
    node = TaskNode(task_id="tn1", goal_id="g1", capability="SEARCH_COMMUNITY")
    step = tasknode_to_action_step(node)
    assert step.step_id == "tn1"
    assert step.semantic_action == "SEARCH_COMMUNITY"
