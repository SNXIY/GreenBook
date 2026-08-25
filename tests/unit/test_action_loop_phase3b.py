"""Focused Phase 3B tests for the ActionLoop.

The decision maker, read handler, write submitter, and task store are explicit
test stubs.  No stub fabricates a real Java/DB/LLM verification result: writes
that return SUBMITTED produce a WAIT (they are not treated as done), and FINISH
is only honored when the Task's objectives are backed by real resources.
"""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.task.models import Task, TaskExecutionRef, TaskGoal, TaskStatus

# ── stubs ────────────────────────────────────────────────────────────────


class RecordingStore:
    """Test task store that records events and resources against the Task."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.resources: list[tuple[str, str, str]] = []

    def _record(self, task: Any, event: str, detail: Any) -> None:
        self.events.append((event, detail))
        task.last_action = event

    def _record_resource(self, task: Any, resource_id: str, resource_kind: str, title: str = "", content: str = "", objective_id: str = "") -> None:
        self.resources.append((resource_id, resource_kind, title))
        task.resource_index.append(
            {"resource_id": resource_id, "resource_kind": resource_kind, "title": title}
        )


class QueueDecisions:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self._queue = list(decisions)
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self._queue.pop(0)


def _task(*, task_id: str = "task-java", goals: list[TaskGoal] | None = None) -> Task:
    resolved_goals = goals if goals is not None else [
        TaskGoal(task_id=task_id, kind="DRAFT", status="PENDING")
    ]
    return Task(
        task_id=task_id,
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="搜 Java 面试内容，写文章，明天九点发",
        status=TaskStatus.RUNNING,
        goals=resolved_goals,
        resource_index=[],
        execution_refs=[],
        artifacts=[],
    )


def _decision(dtype: ActionDecisionType, **kw: Any) -> ActionDecision:
    return ActionDecision(decision=dtype, **kw)


def _loop(
    *,
    decisions: list[ActionDecision],
    read=None,
    write=None,
    store: RecordingStore | None = None,
) -> ActionLoop:
    return ActionLoop(
        decision_maker=QueueDecisions(decisions),
        read_handler=read,
        write_submitter=write,
        task_store=store or RecordingStore(),
        max_iterations=12,
    )


async def _ok_read(tool_name=None, arguments=None, **kwargs):
    return {"ok": True, "resource_id": "search-1", "content": "找到结果"}


async def _complete_write(tool_name=None, arguments=None, **kwargs):
    if tool_name == "publication.schedule":
        return {"ok": True, "status": "COMPLETED", "resource_id": "sched-1", "schedule_id": "sched-1"}
    if tool_name == "content.create_draft":
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-1", "draft_id": "draft-1"}
    return {"ok": True, "status": "COMPLETED", "resource_id": "r-1"}


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complex_search_create_schedule_action_loop() -> None:
    store = RecordingStore()
    task = _task(goals=[
        TaskGoal(task_id="t", kind="DRAFT", status="PENDING"),
        TaskGoal(task_id="t", kind="SCHEDULE", status="PENDING"),
    ])
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Java 面试"}),
            _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"title": "Java 面试", "instruction": "写一篇"}),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="CREATE_SCHEDULE", arguments={"run_at": "2026-08-16T09:00:00Z"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=_ok_read,
        write=_complete_write,
        store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.success
    assert result.decisions[-1] == "4:FINISH"
    kinds = {r["resource_kind"] for r in task.resource_index}
    assert {"DRAFT", "SCHEDULE"} <= kinds
    # Every objective was satisfied by a real resource before finishing.
    assert "finish" in [e for e, _ in store.events]


@pytest.mark.asyncio
async def test_action_loop_artifact_driven_next_action() -> None:
    # Given a search artifact, the model freely chooses to create directly —
    # no Summarize step is forced.
    store = RecordingStore()
    task = _task(goals=[TaskGoal(task_id="t", kind="DRAFT", status="PENDING")])
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "x"}),
            _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"title": "t", "instruction": "i"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=_ok_read,
        write=_complete_write,
        store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    # The loop passed the search artifact into the create decision context;
    # there is no fixed workflow constant in the loop source.
    import greenbook_agent_core.actionloop.loop as loop_mod

    assert "summarize" not in loop_mod._SEMANTIC_TOOL
    assert result.decisions == ["1:CALL_TOOL", "2:GENERATE_CONTENT", "3:FINISH"]


@pytest.mark.asyncio
async def test_action_loop_no_fixed_workflow() -> None:
    # The loop must not hardcode any search->summarize->create->schedule chain.
    from pathlib import Path

    import greenbook_agent_core.actionloop.loop as loop_mod

    source = Path(loop_mod.__file__).read_text(encoding="utf-8").lower()
    assert "search" in source and "schedule" in source
    # No sequential fixed pipeline constant.
    assert "search->summarize->create->schedule" not in source
    assert "_workflow" not in source or "no fixed workflow" in source


class _Boundary:
    """Minimal execution boundary stub for _submit_write tests."""

    def record_operation_submitted(self, **kw: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_submit_write_materialized_execution_reports_submitted() -> None:
    # C1: a CANCEL write that durably submits materializes an execution and the
    # loop reports SUBMITTED with that real execution id (never a fabricated
    # empty one).
    async def ok_write(**kwargs):
        return {"ok": True, "status": "SUBMITTED", "execution_id": "exec-cancel-1"}

    loop = _loop(decisions=[], write=ok_write)
    obs = await loop._submit_write(
        _task(), _command(), _request(), "CANCEL_SCHEDULE", "CANCEL_SCHEDULE",
        "publication.cancel_schedule", {}, _Boundary(), objective_id="",
    )
    assert obs.outcome == "SUBMITTED"
    assert obs.execution_id == "exec-cancel-1"


@pytest.mark.asyncio
async def test_submit_write_none_does_not_fabricate_submitted() -> None:
    # C3: a write submit that returns None means no PlanExecution was
    # materialized; the loop must NOT report SUBMITTED (which would fabricate
    # WAITING_EXTERNAL and a falsely COMPLETED run).
    async def none_write(**kwargs):
        return None

    loop = _loop(decisions=[], write=none_write)
    obs = await loop._submit_write(
        _task(), _command(), _request(), "CANCEL_SCHEDULE", "CANCEL_SCHEDULE",
        "publication.cancel_schedule", {}, _Boundary(), objective_id="",
    )
    assert obs.outcome != "SUBMITTED"
    assert obs.ok is False


@pytest.mark.asyncio
async def test_submit_write_ok_without_execution_id_does_not_fabricate_submitted() -> None:
    # C3: ok=True but no execution_id means no durable execution was created; the
    # loop must not wait (WAITING_EXTERNAL) on a write that never submitted.
    async def ok_no_exec(**kwargs):
        return {"ok": True, "status": "SUBMITTED"}

    loop = _loop(decisions=[], write=ok_no_exec)
    obs = await loop._submit_write(
        _task(), _command(), _request(), "CANCEL_SCHEDULE", "CANCEL_SCHEDULE",
        "publication.cancel_schedule", {}, _Boundary(), objective_id="",
    )
    assert obs.outcome != "SUBMITTED"
    assert obs.ok is False


@pytest.mark.asyncio
async def test_action_loop_write_waits_for_verified_result() -> None:
    async def pending_write(**kwargs):
        return {"ok": True, "status": "SUBMITTED", "execution_id": "exec-9"}

    decision_maker = QueueDecisions([
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="UPDATE_DRAFT", arguments={"title": "x"}),
    ])
    loop = ActionLoop(
        decision_maker=decision_maker,
        write_submitter=pending_write,
        task_store=RecordingStore(),
        max_iterations=8,
    )
    task = _task()
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "WAITING_EXTERNAL"
    assert result.execution_id == "exec-9"
    # The model is NOT called again after a write is submitted.
    assert decision_maker.calls == 1


@pytest.mark.asyncio
async def test_action_loop_result_unknown_does_not_finish() -> None:
    task = _task()
    task.execution_refs = [TaskExecutionRef(execution_id="e1", task_id="t", status="RESULT_UNKNOWN")]
    decision_maker = QueueDecisions([])
    loop = ActionLoop(decision_maker=decision_maker, task_store=RecordingStore())
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "WAITING_EXTERNAL"
    assert decision_maker.calls == 0, "a RESULT_UNKNOWN execution must not keep reasoning"


@pytest.mark.asyncio
async def test_action_loop_multi_task_isolation() -> None:
    store_a = RecordingStore()
    store_b = RecordingStore()
    task_a = _task(task_id="task-java", goals=[TaskGoal(task_id="task-java", kind="SEARCH_RESULT", status="PENDING")])
    task_b = _task(task_id="task-agent", goals=[])
    loop_a = _loop(decisions=[
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "java"}),
        _decision(ActionDecisionType.FINISH),
    ], read=_ok_read, write=_complete_write, store=store_a)
    loop_b = _loop(decisions=[
        _decision(ActionDecisionType.FINISH),
    ], read=_ok_read, write=_complete_write, store=store_b)
    await loop_a.run(task_a, _command(), request=_request())
    await loop_b.run(task_b, _command(), request=_request())
    # Task A's search artifact must never leak into Task B.
    assert any(r["resource_kind"] == "SEARCH_RESULT" for r in task_a.resource_index)
    assert not any(r["resource_kind"] == "SEARCH_RESULT" for r in task_b.resource_index)
    assert task_b.resource_index == []


@pytest.mark.asyncio
async def test_action_loop_task_wait_does_not_block_other_task() -> None:
    async def pending_write(**kwargs):
        return {"ok": True, "status": "SUBMITTED", "execution_id": "e-wait"}

    task_a = _task(task_id="task-a")
    task_b = _task(task_id="task-b", goals=[])
    loop_a = ActionLoop(
        decision_maker=QueueDecisions([_decision(ActionDecisionType.CALL_TOOL, semantic_action="UPDATE_DRAFT", arguments={"t": "x"})]),
        write_submitter=pending_write, task_store=RecordingStore(),
    )
    loop_b = ActionLoop(
        decision_maker=QueueDecisions([_decision(ActionDecisionType.FINISH)]),
        write_submitter=pending_write, task_store=RecordingStore(),
    )
    result_a = await loop_a.run(task_a, _command(), request=_request())
    result_b = await loop_b.run(task_b, _command(), request=_request())
    assert result_a.status == "WAITING_EXTERNAL"
    assert result_b.status == "COMPLETED", "an independent Task must not be blocked by a waiting Task"


@pytest.mark.asyncio
async def test_action_loop_clarification() -> None:
    store = RecordingStore()
    loop = _loop(decisions=[
        _decision(ActionDecisionType.CLARIFY, reason="不确定要改哪一篇"),
    ], read=_ok_read, write=_complete_write, store=store)
    task = _task()
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "ACTION_LOOP_CLARIFY"
    assert "wait_human" in [e for e, _ in store.events]
    assert task.resource_index == [], "clarification must not create any execution/resource"


@pytest.mark.asyncio
async def test_action_loop_replan_after_read_failure() -> None:
    async def failing_read(tool_name=None, arguments=None, **kwargs):
        return {"ok": False, "message": "搜索失败"}

    async def good_read(tool_name=None, arguments=None, **kwargs):
        return {"ok": True, "resource_id": "search-2", "content": "重试成功"}

    calls = {"n": 0}

    async def flaky_read(**kwargs):
        calls["n"] += 1
        return await failing_read(**kwargs) if calls["n"] == 1 else await good_read(**kwargs)

    store = RecordingStore()
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "x"}),
            _decision(ActionDecisionType.REPLAN, reason="搜索失败，调整检索词", plan_steps=[
                {"step_id": "s1", "semantic_action": "SEARCH_POSTS"}
            ]),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "y"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=flaky_read,
        write=_complete_write,
        store=store,
    )
    task = _task(goals=[TaskGoal(task_id="t", kind="SEARCH_RESULT", status="PENDING")])
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert "replan" in [e for e, _ in store.events]
    assert any(o.outcome == "SUCCESS" for o in result.observations)
    assert any(r["resource_id"] == "search-2" for r in task.resource_index)


@pytest.mark.asyncio
async def test_action_loop_no_duplicate_tool_selection_llm() -> None:
    # Tool selection is deterministic from the semantic action; there is no
    # second tool-selection LLM call.  The decision maker is the only LLM and
    # is called exactly once per iteration.
    decision_maker = QueueDecisions([
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="GET_DRAFT", arguments={"draft_id": "d1"}),
        _decision(ActionDecisionType.FINISH),
    ])
    loop = ActionLoop(
        decision_maker=decision_maker,
        read_handler=_ok_read,
        task_store=RecordingStore(),
        max_iterations=8,
    )
    task = _task(goals=[])
    result = await loop.run(task, _command(), request=_request())
    assert decision_maker.calls == 2, "one decision call per iteration, no separate selector"
    assert result.decisions == ["1:CALL_TOOL", "2:FINISH"]


@pytest.mark.asyncio
async def test_action_loop_completed_task_reopen() -> None:
    store = RecordingStore()
    task = _task(goals=[])
    task.status = TaskStatus.COMPLETED
    loop = _loop(decisions=[
        _decision(ActionDecisionType.CALL_TOOL, semantic_action="UPDATE_DRAFT", arguments={"title": "改一下"}),
        _decision(ActionDecisionType.FINISH),
    ], read=_ok_read, write=_complete_write, store=store)
    # A COMPLETED Task is still driven by the loop (reopen for modification).
    result = await loop.run(task, _command(), request=_request())
    assert result.decisions == ["1:CALL_TOOL", "2:FINISH"]
    assert task.last_action == "finish"


@pytest.mark.asyncio
async def test_search_with_resource_id_satisfies_objective_and_finishes() -> None:
    """When a search read DOES surface a real resource_id, the SEARCH_RESULT
    objective is satisfied and the loop finishes (this is the fix contract)."""
    store = RecordingStore()

    async def search_read(tool_name=None, arguments=None, **kwargs):
        return {"ok": True, "status": "COMPLETED", "resource_id": "search-Agent",
                "content": "找到 31 篇", "message": "找到 31 篇"}

    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=search_read,
        write=_complete_write,
        store=store,
    )
    task = _task(goals=[TaskGoal(task_id="t", kind="SEARCH_RESULT", status="PENDING")])
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.success
    assert any(r["resource_kind"] == "SEARCH_RESULT" for r in task.resource_index)


@pytest.mark.asyncio
async def test_search_then_detail_then_finish_completes() -> None:
    """The core SEARCH -> GET_POST -> FINISH flow completes in the ActionLoop
    with real-like read results (resource_id surfaced for search)."""
    store = RecordingStore()

    async def read(tool_name=None, arguments=None, **kwargs):
        if tool_name == "community.get_post":
            return {"ok": True, "status": "COMPLETED", "resource_id": "3469-p1",
                    "content": "正文：Agent 落地工程实践", "message": "读取成功"}
        return {"ok": True, "status": "COMPLETED", "resource_id": "search-Agent",
                "content": "找到 31 篇", "message": "找到 31 篇"}

    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="GET_POST", arguments={"post_id": "3469-p1"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=read,
        write=_complete_write,
        store=store,
    )
    task = _task(goals=[TaskGoal(task_id="t", kind="SEARCH_RESULT", status="PENDING")])
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.success
    assert result.decisions == ["1:CALL_TOOL", "2:CALL_TOOL", "3:FINISH"]
    kinds = {r["resource_kind"] for r in task.resource_index}
    assert "SEARCH_RESULT" in kinds


@pytest.mark.asyncio
async def test_repeated_successful_search_converges_after_same_observation() -> None:
    """The second read is observed so a backend can return a new resource;
    equivalent results then trip the no-progress guard."""
    calls: list[tuple[str, dict]] = []

    async def read(tool_name=None, arguments=None, **kw):
        calls.append((tool_name, dict(arguments or {})))
        return {"ok": True, "resource_id": "s1", "resource_kind": "SEARCH_RESULT", "content": "ok"}

    store = RecordingStore()
    task = _task(goals=[TaskGoal(task_id="t", kind="SEARCH_RESULT", status="PENDING")])
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=read, store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert len(calls) == 2
    assert result.error_code == "ACTION_LOOP_NO_PROGRESS"
    assert not any(o.outcome == "ALREADY_DONE" for o in result.observations)


@pytest.mark.asyncio
async def test_repeated_search_different_query_is_allowed() -> None:
    """A genuinely different read (new query) is a new action, not a duplicate."""
    calls: list[str] = []

    async def read(tool_name=None, arguments=None, **kw):
        calls.append(str((arguments or {}).get("query")))
        return {"ok": True, "resource_id": f"s-{len(calls)}", "resource_kind": "SEARCH_RESULT", "content": "ok"}

    store = RecordingStore()
    task = _task(goals=[TaskGoal(task_id="t", kind="SEARCH_RESULT", status="PENDING")])
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Memory"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=read, store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert calls == ["Agent", "Memory"], "different query = different action"


@pytest.mark.asyncio
async def test_same_target_does_not_create_second_draft() -> None:
    """W9: when a DRAFT resource already exists for the target, a repeated
    CREATE_DRAFT must not create a second draft (the resume/dup guard)."""
    store = RecordingStore()
    task = _task(goals=[TaskGoal(task_id="t", kind="DRAFT", status="PENDING")])
    task.resource_index.append({"resource_id": "draft-1", "resource_kind": "DRAFT", "title": "已有草稿"})
    create_calls: list[str] = []

    async def write(tool_name=None, arguments=None, **kw):
        if tool_name == "content.create_draft":
            create_calls.append(tool_name)
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-2", "draft_id": "draft-2"}

    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT",
                      arguments={"title": "t", "instruction": "i"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=_ok_read, write=write, store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    # Already-satisfied guard: no second create_draft call for an existing DRAFT.
    assert create_calls == []
    assert any(o.outcome == "ALREADY_SATISFIED" for o in result.observations)


def test_objective_canonical_run_at_overrides_model() -> None:
    """The Objective's canonical run_at (time authority) overrides the model's
    local-time guess for CREATE_SCHEDULE."""
    from greenbook_agent_core.actionloop.loop import _normalize_arguments
    from greenbook_agent_core.task.models import Objective

    obj = Objective(task_id="t", intent="发布", constraints={"run_at": "2026-08-17T02:00:00Z", "timezone": "Asia/Shanghai"})
    args = _normalize_arguments("CREATE_SCHEDULE", {"run_at": "2026-08-17T10:00:00Z", "draft_id": "d1"},
                                command=None, objective=obj)
    assert args["run_at"] == "2026-08-17T02:00:00Z"


def test_update_schedule_uses_objective_canonical() -> None:
    from greenbook_agent_core.actionloop.loop import _normalize_arguments
    from greenbook_agent_core.task.models import Objective

    obj = Objective(task_id="t", intent="改时间", constraints={"run_at": "2026-08-17T08:00:00Z"})
    args = _normalize_arguments("UPDATE_SCHEDULE", {"run_at": "2026-08-17T16:00:00Z", "schedule_id": "s1"},
                                command=None, objective=obj)
    assert args["run_at"] == "2026-08-17T08:00:00Z"


def test_objective_without_run_at_keeps_model_proposal() -> None:
    """An Objective with NO canonical run_at must not invent/force a schedule
    time; a pure UPDATE_DRAFT stays time-free."""
    from greenbook_agent_core.actionloop.loop import _normalize_arguments
    from greenbook_agent_core.task.models import Objective

    obj = Objective(task_id="t", intent="改标题", constraints={})
    args = _normalize_arguments("UPDATE_DRAFT", {"draft_id": "d1", "title": "新标题"},
                                command=None, objective=obj)
    assert "run_at" not in args


# ── helpers ──────────────────────────────────────────────────────────────


def _command() -> Command:
    return Command(type=CommandType.CREATE, goal="复杂任务", raw_input="复杂任务")


def _request() -> Any:
    return type("Req", (), {
        "run_id": "run1", "trace_id": "trace1", "conversation_id": "c1",
        "user_id": "u1", "tenant_id": "t1", "session": None, "auth": None, "mcp": None,
        "llm": None, "model": "", "timezone": "Asia/Shanghai",
        "activity_callback": None, "completion_callback": None,
    })()


@pytest.mark.asyncio
async def test_created_resources_bound_to_current_objective() -> None:
    """Ownership production: resources created for a business Objective are
    bound to that Objective's related_resource_ids (not task-global only)."""
    from greenbook_agent_core.task.models import Objective

    store = RecordingStore()
    obj = Objective(task_id="t", intent="Java学习",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    constraints={"run_at": "2026-08-18T02:00:00Z"})
    task = Task(
        task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="写Java学习帖并发布", status=TaskStatus.RUNNING,
        objectives=[obj], resource_index=[], execution_refs=[], artifacts=[], goals=[],
    )
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"title": "t", "instruction": "i"}),
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="CREATE_SCHEDULE", arguments={"run_at": "2026-08-18T02:00:00Z"}),
            _decision(ActionDecisionType.FINISH),
        ],
        read=_ok_read, write=_complete_write, store=store,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert "draft-1" in obj.related_resource_ids
    assert "sched-1" in obj.related_resource_ids
    assert len(obj.related_resource_ids) == 2, "dedupe: draft + schedule only"


@pytest.mark.asyncio
async def test_business_write_missing_objective_rejected_before_submit() -> None:
    """F5: a new Business Objective WRITE with no objective_id must fail BEFORE
    any side effect (no Operation, no Java call), not silently bind later."""
    from greenbook_agent_core.task.models import Objective

    obj = Objective(task_id="t", intent="Java学习",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"])
    task = Task(
        task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1",
        goal="写并发布", status=TaskStatus.RUNNING,
        objectives=[obj], resource_index=[], execution_refs=[], artifacts=[], goals=[],
    )
    write_calls: list[str] = []

    async def write(tool_name=None, arguments=None, **kw):
        write_calls.append(tool_name)
        return {"ok": True, "status": "COMPLETED", "resource_id": "d1"}

    # Objective has no objective_id (empty) -> reject the write entirely.
    obj.objective_id = ""
    loop = ActionLoop(
        decision_maker=QueueDecisions([
            _decision(ActionDecisionType.GENERATE_CONTENT, semantic_action="CREATE_DRAFT", arguments={"t": "x"}),
            _decision(ActionDecisionType.FINISH),
        ]),
        read_handler=_ok_read, write_submitter=write,
        task_store=RecordingStore(), max_iterations=2,
    )
    result = await loop.run(task, _command(), request=_request())
    assert write_calls == [], "write must not be submitted without objective_id"
    assert any(o.outcome == "FAILED" for o in result.observations)


def test_ready_plan_step_is_scoped_to_current_objective() -> None:
    """A resumed multi-objective loop must not resurrect Objective A's step."""

    plan = TaskPlan(
        task_id="task-multi",
        steps=[
            PlanStep(
                step_id="A:0",
                goal_id="A",
                capability="GENERATE_CONTENT",
                status="READY",
            ),
            PlanStep(
                step_id="B:0",
                goal_id="B",
                capability="GENERATE_CONTENT",
                status="PENDING",
            ),
        ],
    )

    ready = ActionLoop._next_ready_plan_step(plan, objective_id="B")

    assert ready is not None
    assert ready.goal_id == "B"
