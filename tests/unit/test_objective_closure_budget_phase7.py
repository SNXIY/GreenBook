"""Phase 7 tests: Objective state closure (Resource/Artifact/Operation binding)
and ActionLoop runtime budgets."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
)
from greenbook_agent_core.task.objective_reducer import (
    ObjectiveStateReducer,
    bind_related,
    is_objective_satisfied,
    objective_for_resource,
)


def _objective(task_id: str, kind: str, status: ObjectiveStatus = ObjectiveStatus.PENDING) -> Objective:
    return Objective(task_id=task_id, description=kind, intent=kind,
                     expected_resource_kind=kind, status=status)


def _task(*, task_id: str = "t1", kinds=None, resources=None, exec_refs=None) -> Task:
    return Task(
        task_id=task_id, conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="复杂任务", objectives=[_objective(task_id, k) for k in (kinds or [])],
        resource_index=list(resources or []),
        execution_refs=list(exec_refs or []),
        artifacts=[], goals=[],
    )


# ── binding ───────────────────────────────────────────────────────────────


def test_resource_bound_to_correct_objective() -> None:
    task = _task(kinds=["SEARCH_RESULT", "DRAFT", "SCHEDULE"])
    target = objective_for_resource(task, "DRAFT")
    assert target is not None
    bind_related(task, objective_id=target.objective_id, resource_id="draft-1",
                 resource_kind="DRAFT", operation_id="op-1")
    draft_obj = next(o for o in task.objectives if o.expected_resource_kind == "DRAFT")
    assert "draft-1" in draft_obj.related_resource_ids
    assert "op-1" in draft_obj.related_operations
    others = [o for o in task.objectives if o is not draft_obj]
    assert all(not o.related_resource_ids for o in others), "resource binds only to its objective"


def test_artifact_bound_to_correct_objective() -> None:
    task = _task(kinds=["DRAFT"])
    target = task.objectives[0]
    bind_related(task, objective_id=target.objective_id, resource_id="d1",
                 resource_kind="DRAFT", artifact_id="art-1")
    assert "art-1" in target.related_artifact_ids


def test_cross_task_binding_forbidden() -> None:
    task_a = _task(task_id="task-a", kinds=["DRAFT"])
    task_b = _task(task_id="task-b", kinds=["DRAFT"])
    bind_related(task_a, objective_id=task_a.objectives[0].objective_id,
                 resource_id="d1", resource_kind="DRAFT")
    assert task_a.objectives[0].related_resource_ids == ["d1"]
    assert task_b.objectives[0].related_resource_ids == [], "cross-task binding is forbidden"


def test_verified_binding_completes_objective() -> None:
    task = _task(kinds=["DRAFT"])
    target = task.objectives[0]
    bind_related(task, objective_id=target.objective_id, resource_id="d1", resource_kind="DRAFT")
    task.resource_index.append({"resource_id": "d1", "resource_kind": "DRAFT"})
    assert is_objective_satisfied(task, target) is True
    ObjectiveStateReducer().reduce(task)
    assert target.status == ObjectiveStatus.COMPLETED


def test_result_unknown_does_not_complete() -> None:
    task = _task(
        kinds=["DRAFT"],
        exec_refs=[TaskExecutionRef(execution_id="e1", task_id="t1", status="RESULT_UNKNOWN")],
    )
    ObjectiveStateReducer().reduce(task)
    assert task.objectives[0].status != ObjectiveStatus.COMPLETED


def test_objective_waiting_resume_completed() -> None:
    task = _task(kinds=["DRAFT"], exec_refs=[TaskExecutionRef(execution_id="e1", task_id="t1", status="SUBMITTED")])
    ObjectiveStateReducer().reduce(task)
    assert task.objectives[0].status == ObjectiveStatus.WAITING
    # Resume: the write verified and the resource appears.
    task.execution_refs = [TaskExecutionRef(execution_id="e1", task_id="t1", status="COMPLETED")]
    task.resource_index.append({"resource_id": "d1", "resource_kind": "DRAFT"})
    bind_related(task, objective_id=task.objectives[0].objective_id, resource_id="d1", resource_kind="DRAFT")
    ObjectiveStateReducer().reduce(task)
    assert task.objectives[0].status == ObjectiveStatus.COMPLETED


# ── budgets ──────────────────────────────────────────────────────────────


class RepeatDecision:
    def __init__(self, decision: ActionDecision) -> None:
        self._decision = decision
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self._decision


def _decision(dtype: ActionDecisionType, **kw: Any) -> ActionDecision:
    return ActionDecision(decision=dtype, **kw)


def _cmd() -> Command:
    return Command(type=CommandType.CREATE, goal="复杂任务", raw_input="复杂任务")


class Store:
    def _record(self, *a, **k): return None
    def _record_resource(self, *a, **k): return None


async def _ok_write(tool_name=None, arguments=None, semantic_action=None, **kw):
    return {"ok": True, "status": "SUBMITTED", "execution_id": "e-1"}


async def _ok_read(tool_name=None, arguments=None, **kw):
    return {"ok": True}  # read with no resource: loop continues, no objective satisfied


_READ_DECISION = _decision(ActionDecisionType.CALL_TOOL, semantic_action="LIST_DRAFTS")


@pytest.mark.asyncio
async def test_loop_iteration_budget() -> None:
    task = _task(kinds=["DRAFT"])
    loop = ActionLoop(
        decision_maker=RepeatDecision(_READ_DECISION),
        read_handler=_ok_read,
        task_store=Store(),
        max_iterations=2,
    )
    result = await loop.run(task, _cmd())
    assert result.status == "FAILED"
    assert result.error_code == "ACTION_LOOP_NO_PROGRESS"


@pytest.mark.asyncio
async def test_llm_call_budget() -> None:
    task = _task(kinds=["DRAFT"])
    loop = ActionLoop(
        decision_maker=RepeatDecision(_READ_DECISION),
        read_handler=_ok_read,
        task_store=Store(),
        max_iterations=10,
        max_llm_calls=1,
    )
    result = await loop.run(task, _cmd())
    assert result.error_code == "ACTION_LOOP_LLM_BUDGET"


@pytest.mark.asyncio
async def test_tool_call_budget() -> None:
    # Distinct reads (different query) each count as a real tool call; the
    # read-dedup guard only collapses identical re-reads, so distinct reads
    # still exceed the tool budget.
    class DistinctReads:
        def __init__(self) -> None:
            self.i = 0

        async def __call__(self, context: Any) -> ActionDecision:
            self.i += 1
            return _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS",
                             arguments={"query": f"q{self.i}"})

    task = _task(kinds=["DRAFT"])
    loop = ActionLoop(
        decision_maker=DistinctReads(),
        read_handler=_ok_read,
        task_store=Store(),
        max_iterations=10,
        max_tool_calls=1,
    )
    result = await loop.run(task, _cmd())
    assert result.error_code == "ACTION_LOOP_TOOL_BUDGET"


@pytest.mark.asyncio
async def test_replan_budget() -> None:
    task = _task(kinds=["DRAFT"])
    loop = ActionLoop(
        decision_maker=RepeatDecision(_decision(ActionDecisionType.REPLAN, reason="调整", plan_steps=[
            {"step_id": "s1", "semantic_action": "CREATE_DRAFT"}
        ])),
        task_store=Store(),
        max_iterations=10,
        max_replans=1,
    )
    result = await loop.run(task, _cmd())
    assert result.error_code == "ACTION_LOOP_REPLAN_BUDGET"


@pytest.mark.asyncio
async def test_waiting_external_does_not_consume_budget() -> None:
    task = _task(kinds=["DRAFT"], exec_refs=[TaskExecutionRef(execution_id="e1", task_id="t1", status="SUBMITTED")])
    decision_maker = RepeatDecision(_decision(ActionDecisionType.CALL_TOOL, semantic_action="UPDATE_DRAFT"))
    loop = ActionLoop(
        decision_maker=decision_maker,
        task_store=Store(),
        max_iterations=5,
        max_llm_calls=1,
    )
    result = await loop.run(task, _cmd())
    assert result.status == "WAITING_EXTERNAL"
    assert decision_maker.calls == 0, "WAITING_EXTERNAL must not consume the reasoning budget"


# ── reopen ───────────────────────────────────────────────────────────────


def test_completed_task_reopen_preserves_old_objectives() -> None:
    from greenbook_agent_core.task.objective_compat import objectives_for_capabilities

    task = _task(kinds=["DRAFT"])
    task.objectives[0].status = ObjectiveStatus.COMPLETED
    task.objectives[0].completed_at = "2026-08-15T10:00:00Z"
    old = [o.objective_id for o in task.objectives]
    task.objectives.extend(objectives_for_capabilities(["SCHEDULE_PUBLISH"], task.task_id))
    assert [o.objective_id for o in task.objectives][: len(old)] == old
    assert task.objectives[0].status == ObjectiveStatus.COMPLETED, "reopen must not tamper old history"
    assert task.objectives[-1].status == ObjectiveStatus.PENDING
