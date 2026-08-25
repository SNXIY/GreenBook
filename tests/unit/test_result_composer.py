"""Focused tests: generic ActionLoop result production (ResultComposer).

Covers the three completion kinds (DIRECT / MUTATION / GROUNDED_SYNTHESIS),
evidence readiness, cross-task isolation, and partial evidence.  No real LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.actionloop import ActionDecision, ActionDecisionType, ActionLoop
from greenbook_agent_core.actionloop.models import ActionLoopResult, ActionObservation
from greenbook_agent_core.actionloop.loop import _extract_resource_refs
from greenbook_agent_core.actionloop.result import (
    FinalResult,
    ResultComposer,
    ResultRequirement,
    classify_result_requirement,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.task.models import (
    ArtifactRef,
    Objective,
    Task,
    TaskResourceRef,
    TaskStatus,
)


class _RecordingStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    def _record(self, task: Any, event: str, detail: Any) -> None:
        self.events.append(event)

    def _record_resource(self, task: Any, resource_id: str, resource_kind: str, title: str = "", content: str = "", objective_id: str = "") -> None:
        task.resource_index.append(
            TaskResourceRef(resource_id=resource_id, resource_kind=resource_kind, title=title)
        )


class _Decisions:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self._queue = list(decisions)

    async def __call__(self, context: Any) -> ActionDecision:
        return self._queue.pop(0)


def _decision(dtype: ActionDecisionType, **kw: Any) -> ActionDecision:
    return ActionDecision(decision=dtype, **kw)


def _task(*, objectives: list[Objective], resources: list[TaskResourceRef] | None = None,
          artifacts: list[Any] | None = None) -> Task:
    return Task(
        task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1",
        goals=[], status=TaskStatus.RUNNING, objectives=objectives,
        resource_index=list(resources or []), artifacts=list(artifacts or []),
        execution_refs=[], plan_history=[],
    )


def _synth_objective(intent: str = "总结共同方法") -> Objective:
    return Objective(task_id="t", intent=intent, description=intent,
                     result_requirement=ResultRequirement.GROUNDED_SYNTHESIS)


def _direct_objective(kind: str = "SEARCH_RESULT") -> Objective:
    return Objective(task_id="t", intent="SEARCH_COMMUNITY", expected_resource_kind=kind,
                     result_requirement=ResultRequirement.DIRECT_RESULT)


def _search_get_post_read(items: list[dict] | None = None):
    """Read handler for Search (SEARCH_RESULT candidates) + GET_POST (POST evidence)."""
    items = items or [{"post_id": "p1", "title": "Agent 工程"}, {"post_id": "p2", "title": "多智能体"}]

    async def read(tool_name=None, arguments=None, **kw):
        if tool_name == "community.search_public_posts":
            return {"ok": True, "resource_id": "search-1", "resource_kind": "SEARCH_RESULT",
                    "data": {"total": len(items), "items": items}}
        return {"ok": True, "resource_id": (arguments or {}).get("post_id"), "resource_kind": "POST",
                "data": {"post_id": (arguments or {}).get("post_id"), "content": "body"}}

    return read


def _loop(decisions: list[ActionDecision], task: Task, *, composer: ResultComposer | None = None,
          read=None, store: _RecordingStore | None = None) -> ActionLoop:
    return ActionLoop(
        decision_maker=_Decisions(decisions),
        read_handler=read or (lambda **kw: {"ok": True, "resource_id": "r-1", "content": "ok"}),
        task_store=store or _RecordingStore(),
        result_composer=composer,
        max_iterations=10,
    )


def _request() -> Any:
    return type("Req", (), {"run_id": "r", "trace_id": "t", "conversation_id": "c1",
                            "user_id": "u1", "tenant_id": "t1"})()


def _command() -> Command:
    return Command(type=CommandType.QUERY, goal="总结共同方法", raw_input="总结共同方法")


# ── classifier (metadata-driven, no keyword rules) ───────────────────────


def test_classify_llm_step_is_grounded_synthesis() -> None:
    class _Cap:
        is_llm_step = True
    assert classify_result_requirement(_Cap()) == ResultRequirement.GROUNDED_SYNTHESIS


def test_classify_tool_capability_is_direct_result() -> None:
    class _Cap:
        is_llm_step = False
    assert classify_result_requirement(_Cap()) == ResultRequirement.DIRECT_RESULT


# ── ResultComposer evidence & readiness ──────────────────────────────────


def test_grounded_synthesis_not_ready_without_evidence() -> None:
    composer = ResultComposer()
    task = _task(objectives=[_synth_objective()], resources=[])
    result = composer.evidence_from_task(task)
    assert result == []
    composed = asyncio_run(composer.compose(objective=_synth_objective(), intent="总结", task=task))
    assert composed.ready is False
    assert composed.coverage == 0.0
    assert composed.content == ""


def test_grounded_synthesis_ready_with_current_task_evidence() -> None:
    composer = ResultComposer(generator=lambda intent, ev: "社区共同方法总结")
    task = _task(objectives=[_synth_objective()],
                 resources=[TaskResourceRef(resource_id="p1", resource_kind="POST", title="Agent 工程")])
    composed = asyncio_run(composer.compose(objective=_synth_objective(), intent="总结", task=task))
    assert composed.ready is True
    assert composed.source_refs == ["p1"]
    assert composed.coverage == 1.0
    assert composed.content == "社区共同方法总结"


def test_evidence_is_current_task_only() -> None:
    """Task A evidence must not satisfy Task B's synthesis (cross-task isolation)."""
    composer = ResultComposer()
    task_b = _task(objectives=[_synth_objective()], resources=[])  # Task B: no evidence
    task_a = _task(objectives=[_synth_objective()],
                   resources=[TaskResourceRef(resource_id="a1", resource_kind="POST")])
    assert composer.evidence_from_task(task_a) != []
    assert composer.evidence_from_task(task_b) == []


def test_partial_evidence_below_threshold_not_ready() -> None:
    composer = ResultComposer()
    task = _task(objectives=[_synth_objective()],
                 resources=[TaskResourceRef(resource_id="p1", resource_kind="POST")])
    # 1/5 required sources -> coverage 0.2 < 0.8 -> not ready.
    composed = asyncio_run(composer.compose(
        objective=_synth_objective(), intent="总结", task=task,
        required_coverage=0.8, required_evidence_count=5))
    assert composed.ready is False
    assert composed.coverage == 0.2


def test_evidence_is_objective_owned_within_one_task() -> None:
    """Objective B cannot compose from Objective A's POST evidence."""
    composer = ResultComposer()
    first = _synth_objective("first")
    first.objective_id = "objective-a"
    first.related_resource_ids = ["post-a"]
    second = _synth_objective("second")
    second.objective_id = "objective-b"
    task = _task(
        objectives=[first, second],
        resources=[TaskResourceRef(
            resource_id="post-a", resource_kind="POST", objective_id="objective-a",
        )],
    )
    assert asyncio_run(composer.compose(objective=first, intent="first", task=task)).ready is True
    assert asyncio_run(composer.compose(objective=second, intent="second", task=task)).ready is False


# ── ActionLoop completion kinds ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_result_finishes_without_composer() -> None:
    """R1: a DIRECT_RESULT task completes on its resource; no synthesis needed."""
    store = _RecordingStore()
    task = _task(objectives=[_direct_objective()])
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ],
        task=task, store=store,
        read=_search_get_post_read(),
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.final_result is None
    assert any(r.resource_kind == "SEARCH_RESULT" for r in task.resource_index)


@pytest.mark.asyncio
async def test_grounded_synthesis_search_compose_finish() -> None:
    """R3: SEARCH -> COMPOSE_RESULT -> FINISH produces a composed answer."""
    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer(generator=lambda intent, ev: "共同方法：落地工程实践、多智能体协作。")
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.COMPOSE_RESULT),
            _decision(ActionDecisionType.FINISH),
        ],
        task=task, store=store, composer=composer,
        read=_search_get_post_read(),
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.success
    assert result.final_result is not None
    assert "共同方法" in result.content
    # SEARCH (model) -> GET_POST (deterministic) -> evidence ready -> deterministic compose+finish
    assert result.decisions[-1].startswith("2:CALL_TOOL")


@pytest.mark.asyncio
async def test_grounded_synthesis_no_evidence_fails_fast() -> None:
    """R7: GROUNDED_SYNTHESIS with empty evidence must NOT fabricate a result."""
    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer()  # no evidence -> not ready
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.COMPOSE_RESULT),
            _decision(ActionDecisionType.COMPOSE_RESULT),
            _decision(ActionDecisionType.COMPOSE_RESULT),
            _decision(ActionDecisionType.COMPOSE_RESULT),
        ],
        task=task, store=store, composer=composer,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "FAILED"
    assert result.error_code == "ACTION_LOOP_EVIDENCE_BUDGET"
    # No fabricated answer.
    assert "综合" not in result.content


def _sync_loop_task_with_existing_artifact() -> tuple[ActionLoop, Task]:
    task = _task(objectives=[_synth_objective()],
                 artifacts=[ArtifactRef(task_id="t", artifact_id="a1", artifact_type="ANALYSIS_REPORT",
                                        resource_id="a1", resource_kind="ANALYSIS_REPORT", summary="已有报告")])
    return task


@pytest.mark.asyncio
async def test_existing_artifact_synthesis_without_forced_search() -> None:
    """R4: existing current-Task artifact is valid evidence; no SEARCH forced."""
    store = _RecordingStore()
    task = _sync_loop_task_with_existing_artifact()
    composer = ResultComposer(generator=lambda intent, ev: "基于已有报告的综合")
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.COMPOSE_RESULT),
            _decision(ActionDecisionType.FINISH),
        ],
        task=task, store=store, composer=composer,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert "已有报告" in result.content or result.content


# ── deterministic COMPOSE_RESULT (state machine, not LLM) ─────────────────


@pytest.mark.asyncio
async def test_r11_evidence_ready_composes_deterministically_on_finish() -> None:
    """R11: with evidence ready, FINISH deterministically composes the result;
    the model does NOT have to emit COMPOSE_RESULT."""
    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer(generator=lambda intent, ev: "自动综合结论")
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),  # no explicit COMPOSE_RESULT
        ],
        task=task, store=store, composer=composer,
        read=_search_get_post_read(),
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert result.final_result is not None
    assert "自动综合结论" in result.content
    assert task.objectives[0].related_artifact_ids  # result artifact bound


def test_r12_r13_result_artifact_gates_synthesis_completion() -> None:
    """R12: no result artifact -> synthesis objective cannot complete.
    R13: once a result artifact is bound -> objective completes."""
    from greenbook_agent_core.actionloop.loop import _bind_composed_result
    from greenbook_agent_core.task.objective_reducer import ObjectiveStateReducer, all_objectives_satisfied

    reducer = ObjectiveStateReducer()
    task = _task(objectives=[_synth_objective()],
                 resources=[TaskResourceRef(resource_id="s1", resource_kind="POST", title="x")])
    reducer.reduce(task)
    assert all_objectives_satisfied(task) is False  # R12

    _bind_composed_result(task, task.objectives[0], "result-1")
    reducer.reduce(task)
    assert all_objectives_satisfied(task) is True  # R13


def test_r14_source_ref_must_be_real() -> None:
    """R14: composer only counts real current-Task facts; invented refs are absent."""
    composer = ResultComposer()
    task = _task(objectives=[_synth_objective()])  # no resources/artifacts
    facts = composer.evidence_from_task(task)
    assert facts == []
    # A fake source_ref is never derived from model text.
    assert "fake-artifact" not in [f["source_ref"] for f in facts]


@pytest.mark.asyncio
async def test_r16_read_structured_result_survives_to_composer() -> None:
    """R16: a structured read result (resource+kind) flows into the composer as
    evidence and drives a grounded result."""
    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer(generator=lambda intent, ev: "基于结构化结果")
    loop = _loop(
        decisions=[
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ],
        task=task, store=store, composer=composer,
        read=_search_get_post_read(),
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert "结构化结果" in result.content
    # Strong evidence comes from the GET_POST POST resource, not the search set.
    refs = composer.evidence_from_task(task)
    assert any(f["source_ref"] == "p1" for f in refs)


# ── deterministic evidence acquisition (candidates -> GET_POST, no LLM) ────


@pytest.mark.asyncio
async def test_r_deterministic_evidence_acquisition_no_llm_detail() -> None:
    """After a SEARCH produces candidates for a GROUNDED_SYNTHESIS Objective, the
    Runtime deterministically GET_POSTs the next PENDING candidate — the model
    never has to emit GET_POST."""
    calls: list[tuple[str, dict]] = []

    async def read(tool_name=None, arguments=None, **kw):
        calls.append((tool_name, dict(arguments or {})))
        if tool_name == "community.search_public_posts":
            return {"ok": True, "resource_id": "search-1", "resource_kind": "SEARCH_RESULT",
                    "data": {"total": 2, "items": [{"post_id": "A", "title": "a"}, {"post_id": "B", "title": "b"}]}}
        return {"ok": True, "resource_id": (arguments or {}).get("post_id"), "resource_kind": "POST",
                "data": {"post_id": (arguments or {}).get("post_id"), "content": "body"}}

    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer(generator=lambda intent, ev: "共同方法总结")
    loop = ActionLoop(
        decision_maker=_Decisions([
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ]),
        read_handler=read, task_store=store, result_composer=composer, max_iterations=10,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    detail_calls = [c for c in calls if c[0] == "community.get_post"]
    assert detail_calls, "Runtime must deterministically call GET_POST"
    # GET_POST targeted the first pending candidate in order.
    assert detail_calls[0][1].get("post_id") == "A"
    # The synthesis objective only completed after a POST resource (evidence) existed.
    assert task.objectives[0].related_artifact_ids
    assert "共同方法总结" in result.content


@pytest.mark.asyncio
async def test_r_detail_failure_moves_to_next_candidate() -> None:
    """A GET_POST failure on one candidate marks it FAILED and the loop tries the
    next PENDING candidate deterministically (no repeated failed candidate)."""
    calls: list[tuple[str, dict]] = []

    async def read(tool_name=None, arguments=None, **kw):
        calls.append((tool_name, dict(arguments or {})))
        if tool_name == "community.search_public_posts":
            return {"ok": True, "resource_id": "search-1", "resource_kind": "SEARCH_RESULT",
                    "data": {"total": 2, "items": [{"post_id": "A", "title": "a"}, {"post_id": "B", "title": "b"}]}}
        pid = (arguments or {}).get("post_id")
        if pid == "A":
            return {"ok": False, "code": "POST_NOT_FOUND", "message": "404"}
        return {"ok": True, "resource_id": pid, "resource_kind": "POST", "data": {"post_id": pid}}

    store = _RecordingStore()
    task = _task(objectives=[_synth_objective()])
    composer = ResultComposer(generator=lambda intent, ev: "共同方法总结")
    loop = ActionLoop(
        decision_maker=_Decisions([
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ]),
        read_handler=read, task_store=store, result_composer=composer, max_iterations=10,
    )
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    pids = [c[1].get("post_id") for c in calls if c[0] == "community.get_post"]
    assert pids == ["A", "B"], "A failed -> must move to B, never repeat A"
    assert "共同方法总结" in result.content


# ── min_sources (Compare requires >=2 distinct real sources) ──────────────


@pytest.mark.asyncio
async def test_r_detail_without_identity_is_not_success_and_moves_on() -> None:
    """A successful-but-anonymous GET_POST cannot become reusable evidence."""
    calls: list[str] = []

    async def read(tool_name=None, arguments=None, **kw):
        if tool_name == "community.search_public_posts":
            return {"ok": True, "data": {"items": [{"post_id": "A"}, {"post_id": "B"}]}}
        pid = str((arguments or {}).get("post_id") or "")
        calls.append(pid)
        if pid == "A":
            return {"ok": True, "data": {"content": "missing source id"}}
        return {"ok": True, "data": {"post_id": pid, "content": "verified"}}

    task = _task(objectives=[_synth_objective()])
    result = await ActionLoop(
        decision_maker=_Decisions([
            _decision(ActionDecisionType.CALL_TOOL, semantic_action="SEARCH_POSTS", arguments={"query": "Agent"}),
            _decision(ActionDecisionType.FINISH),
        ]),
        read_handler=read,
        task_store=_RecordingStore(),
        result_composer=ResultComposer(generator=lambda _intent, _evidence: "summary"),
        max_iterations=10,
    ).run(task, _command(), request=_request())

    assert result.status == "COMPLETED"
    assert calls == ["A", "B"]
    assert any(
        observation.action == "GET_POST" and observation.outcome == "FAILED"
        for observation in result.observations
    )


def test_compare_requires_two_sources() -> None:
    """A multi-source (Compare) Objective must not compose from a single source."""
    composer = ResultComposer(generator=lambda intent, ev: "比较结果")
    objective = _synth_objective("比较两篇")
    objective.min_sources = 2
    one = _task(objectives=[objective],
                resources=[TaskResourceRef(resource_id="p1", resource_kind="POST", title="A", content="x")])
    assert asyncio_run(composer.compose(objective=objective, intent="比较", task=one)).ready is False
    two = _task(objectives=[objective],
                resources=[TaskResourceRef(resource_id="p1", resource_kind="POST", title="A", content="x"),
                           TaskResourceRef(resource_id="p2", resource_kind="POST", title="B", content="y")])
    composed = asyncio_run(composer.compose(objective=objective, intent="比较", task=two))
    assert composed.ready is True
    assert len(composed.source_refs) == 2


# ── structured read observation (ToolResult data must not be stringified) ──


def test_search_structured_data_and_source_refs_survive() -> None:
    """A SEARCH read keeps its structured payload; post_ids become source_refs."""
    from greenbook_agent_core.actionloop.loop import ActionLoop
    value = {
        "ok": True,
        "content": "找到 2 篇",
        "data": {"total": 2, "items": [{"post_id": "p1", "title": "a"}, {"post_id": "p2", "title": "b"}]},
    }
    detail = ActionLoop._project_read_observation(value)
    assert detail["structured_data"]["total"] == 2
    assert len(detail["structured_data"]["items"]) == 2
    assert detail["source_refs"] == ["p1", "p2"]
    assert _extract_resource_refs(value) == ["p1", "p2"]


def test_get_post_structured_survives_as_single_evidence() -> None:
    from greenbook_agent_core.actionloop.loop import ActionLoop
    value = {"ok": True, "content": "正文", "data": {"post_id": "p1", "title": "t", "content": "body"}}
    detail = ActionLoop._project_read_observation(value)
    assert detail["structured_data"]["post_id"] == "p1"
    assert detail["source_refs"] == ["p1"]
    assert _extract_resource_refs(value) == ["p1"]


def test_analytics_structured_metrics_survive() -> None:
    from greenbook_agent_core.actionloop.loop import ActionLoop
    value = {"ok": True, "data": {"post_id": "p1", "metrics": {"views": 120, "likes": 8}}}
    detail = ActionLoop._project_read_observation(value)
    assert detail["structured_data"]["metrics"]["views"] == 120
    assert detail["source_refs"] == ["p1"]


def test_no_invented_source_refs() -> None:
    """No model-invented refs: a read with no real ids yields [].

    R14: source_ref must be real, never derived from model text."""
    from greenbook_agent_core.actionloop.loop import ActionLoop
    value = {"ok": True, "content": "模型编的 source_ref 不算", "data": {}}
    assert ActionLoop._project_read_observation(value)["source_refs"] == []
    assert _extract_resource_refs(value) == []


# ── flow: compare existing resources without forced search ─────────────────


@pytest.mark.asyncio
async def test_flow_compare_existing_resources() -> None:
    """Search-free comparison of two current-Task resources via the same
    deterministic compose path."""
    store = _RecordingStore()
    task = _task(objectives=[_synth_objective("比较两份内容")],
                 resources=[TaskResourceRef(resource_id="a", resource_kind="ANALYSIS_REPORT", title="A"),
                            TaskResourceRef(resource_id="b", resource_kind="ANALYSIS_REPORT", title="B")])
    composer = ResultComposer(generator=lambda intent, ev: "共同点与差异总结")
    loop = _loop(decisions=[_decision(ActionDecisionType.FINISH)],
                 task=task, store=store, composer=composer)
    result = await loop.run(task, _command(), request=_request())
    assert result.status == "COMPLETED"
    assert "共同点与差异总结" in result.content
    assert task.objectives[0].related_artifact_ids


def asyncio_run(coro: Any) -> FinalResult:
    import asyncio
    return asyncio.run(coro)
