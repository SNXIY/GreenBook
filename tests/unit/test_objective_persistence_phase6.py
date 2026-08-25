"""Phase 6 tests: Objective persistence, resume, reducer, and context budget."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
)
from greenbook_agent_core.task.objective_compat import resolve_objectives
from greenbook_agent_core.task.objective_reducer import ObjectiveStateReducer
from greenbook_agent_core.task.repository import InMemoryTaskRepository
from greenbook_agent_core.turn import ContextAssembler, TurnBudget


def _objective(task_id: str, kind: str, status: ObjectiveStatus = ObjectiveStatus.PENDING) -> Objective:
    return Objective(task_id=task_id, description=kind, intent=kind,
                     expected_resource_kind=kind, status=status,
                     constraints=(
                         {"run_at": "2026-08-16T09:00:00Z"}
                         if kind == "SCHEDULE" else {}
                     ))


def _task(*, task_id: str = "t1", kinds=None, statuses=None, resources=None, exec_refs=None) -> Task:
    objectives = [_objective(task_id, k, statuses[i] if statuses else ObjectiveStatus.PENDING)
                  for i, k in enumerate(kinds or [])]
    return Task(
        task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="复杂任务", objectives=objectives,
        resource_index=list(resources or []),
        execution_refs=list(exec_refs or []),
        artifacts=[], goals=[],
    )


# ── persistence ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_objective_persists_across_reload() -> None:
    repo = InMemoryTaskRepository()
    task = _task(kinds=["DRAFT", "SCHEDULE"])
    await repo.create(task)
    loaded = await repo.get(task.task_id)
    assert loaded is not None
    assert [o.objective_id for o in loaded.objectives] == [o.objective_id for o in task.objectives]
    assert loaded.objectives[0].expected_resource_kind == "DRAFT"


@pytest.mark.asyncio
async def test_objective_status_persists() -> None:
    repo = InMemoryTaskRepository()
    task = _task(kinds=["DRAFT"])
    await repo.create(task)
    task.objectives[0].status = ObjectiveStatus.COMPLETED
    task.objectives[0].completed_at = "2026-08-15T10:00:00Z"
    await repo.update(task)
    loaded = await repo.get(task.task_id)
    assert loaded.objectives[0].status == ObjectiveStatus.COMPLETED


# ── reducer ───────────────────────────────────────────────────────────────


def test_verified_resource_completes_objective() -> None:
    task = _task(kinds=["DRAFT"], resources=[{"resource_id": "d1", "resource_kind": "DRAFT"}])
    ObjectiveStateReducer().reduce(task)
    assert task.objectives[0].status == ObjectiveStatus.COMPLETED


def test_result_unknown_does_not_complete_objective() -> None:
    task = _task(
        kinds=["DRAFT"],
        exec_refs=[TaskExecutionRef(execution_id="e1", task_id="t1", status="RESULT_UNKNOWN")],
    )
    ObjectiveStateReducer().reduce(task)
    assert task.objectives[0].status != ObjectiveStatus.COMPLETED


# ── resume ────────────────────────────────────────────────────────────────


class Store:
    def _record(self, *a, **k): return None
    def _record_resource(self, task, resource_id, resource_kind, title="", content="", objective_id=""):
        task.resource_index.append(
            {"resource_id": resource_id, "resource_kind": resource_kind, "title": title}
        )


def _decision(dtype: ActionDecisionType, **kw: Any) -> ActionDecision:
    return ActionDecision(decision=dtype, **kw)


def _cmd() -> Command:
    return Command(type=CommandType.CREATE, goal="复杂任务", raw_input="复杂任务")


async def _write(tool_name=None, arguments=None, semantic_action=None, **kw):
    if tool_name == "publication.schedule":
        return {"ok": True, "status": "COMPLETED", "resource_id": "sched-1", "schedule_id": "sched-1"}
    return {"ok": True, "status": "COMPLETED", "resource_id": "r-1"}


def _schedule_then_finish():
    """Decision maker: schedule once, then FINISH (resume skips the done step)."""
    calls = {"n": 0}

    async def decide(context):
        calls["n"] += 1
        if calls["n"] == 1:
            return _decision(ActionDecisionType.CALL_TOOL, semantic_action="CREATE_SCHEDULE",
                             arguments={"run_at": "2026-08-16T09:00:00Z"})
        return _decision(ActionDecisionType.FINISH)

    return decide


@pytest.mark.asyncio
async def test_resume_skips_completed_objective() -> None:
    # DRAFT objective already satisfied by a verified resource; the loop must
    # not re-create the draft, only finish the remaining SCHEDULE objective.
    task = _task(
        kinds=["DRAFT", "SCHEDULE"],
        resources=[{"resource_id": "d1", "resource_kind": "DRAFT"}],
    )
    writes: list[str] = []
    writes_capture = writes

    async def write(**kw):
        writes_capture.append(kw.get("tool_name"))
        return await _write(**kw)

    loop = ActionLoop(
        decision_maker=_schedule_then_finish(),
        write_submitter=write,
        task_store=Store(),
        max_iterations=5,
    )
    result = await loop.run(task, _cmd())
    assert result.status == "COMPLETED"
    assert writes_capture == ["publication.schedule"], "the satisfied DRAFT objective is not re-run"
    assert writes_capture.count("content.create_draft") == 0


@pytest.mark.asyncio
async def test_resume_continues_next_objective() -> None:
    task = _task(
        kinds=["DRAFT", "SCHEDULE"],
        resources=[{"resource_id": "d1", "resource_kind": "DRAFT"}],
    )
    loop = ActionLoop(
        decision_maker=_schedule_then_finish(),
        write_submitter=_write,
        task_store=Store(),
        max_iterations=5,
    )
    result = await loop.run(task, _cmd())
    assert result.status == "COMPLETED"
    assert any(
        str(getattr(r, "resource_kind", None) or r.get("resource_kind", "")) == "SCHEDULE"
        for r in task.resource_index
    )


def test_restart_does_not_repeat_write() -> None:
    # A resumed task whose DRAFT resource already exists must not re-submit the
    # create (guarded by _already_satisfied).
    task = _task(kinds=["DRAFT"], resources=[{"resource_id": "d1", "resource_kind": "DRAFT"}])
    from greenbook_agent_core.actionloop.loop import ActionLoop as LoopImpl

    assert LoopImpl._already_satisfied(task, "CREATE_DRAFT") is True
    assert LoopImpl._already_satisfied(task, "CREATE_SCHEDULE") is False


def test_completed_task_reopen_preserves_history() -> None:
    from greenbook_agent_core.task.objective_compat import objectives_for_capabilities

    task = _task(kinds=["DRAFT"], statuses=[ObjectiveStatus.COMPLETED])
    original = [o.objective_id for o in task.objectives]
    task.objectives.extend(objectives_for_capabilities(["MANAGE_DRAFT"], task.task_id))
    assert [o.objective_id for o in task.objectives][: len(original)] == original
    assert task.objectives[0].status == ObjectiveStatus.COMPLETED, "history is preserved on reopen"


# ── context ───────────────────────────────────────────────────────────────


class _FakeBuilder:
    def __init__(self, snapshot: ContextSnapshot) -> None:
        self._snapshot = snapshot

    def build(self, **kwargs):
        return self._snapshot


def _snapshot_with_objectives() -> ContextSnapshot:
    return ContextSnapshot(
        conversation_id="c1", user_id="u1", tenant_id="t1", timezone="Asia/Shanghai",
        active_tasks=[{
            "task_id": "task-java", "goal": "Java", "status": "RUNNING",
            "objectives": [
                {"objective_id": "o-completed", "task_id": "task-java",
                 "description": "旧的已完成", "status": "COMPLETED", "expected_resource_kind": "DRAFT"},
                {"objective_id": "o-pending", "task_id": "task-java",
                 "description": "待完成", "status": "PENDING", "expected_resource_kind": "SCHEDULE"},
                {"objective_id": "o-pending2", "task_id": "task-java",
                 "description": "再一个待完成", "status": "PENDING", "expected_resource_kind": "POST"},
            ],
        }],
        artifacts=[],
        available_resources=[],
        execution_states=[],
    )


@pytest.mark.asyncio
async def test_context_prioritizes_pending_objectives() -> None:
    assembler = ContextAssembler(_FakeBuilder(_snapshot_with_objectives()), budget=TurnBudget())
    assembled = await assembler.assemble(conversation_id="c1", user_id="u1", tenant_id="t1")
    statuses = [o["status"] for o in assembled.selected_objectives]
    assert statuses[0] == "PENDING", "pending objectives come first"
    # Completed history is summarized, not dropped entirely but capped.
    assert len(assembled.selected_objectives) <= 3


@pytest.mark.asyncio
async def test_context_budget_enforced() -> None:
    budget = TurnBudget(max_objectives=2, max_completed_objective_summary=1)
    assembler = ContextAssembler(_FakeBuilder(_snapshot_with_objectives()), budget=budget)
    assembled = await assembler.assemble(conversation_id="c1", user_id="u1", tenant_id="t1")
    assert len(assembled.selected_objectives) == 2


# ── legacy ────────────────────────────────────────────────────────────────


def test_legacy_goal_restores_objective() -> None:
    task = Task(
        task_id="t1", conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="旧任务", goals=[],
        objectives=[],
    )
    # Legacy task carrying goals (no objectives) is restored via the adapter.
    from greenbook_agent_core.task.models import TaskGoal

    task.goals = [TaskGoal(task_id="t1", description="写文章", kind="GENERATE_CONTENT", status="PENDING")]
    restored = resolve_objectives(task)
    assert len(restored) == 1
    assert restored[0].expected_resource_kind == "DRAFT"
    assert restored[0].task_id == "t1"
