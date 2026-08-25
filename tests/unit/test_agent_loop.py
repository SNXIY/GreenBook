"""Phase 3 AgentLoop, metadata selection, and reflection tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentAction, AgentActionType, AgentLoop
from greenbook_agent_core.agent.recovery import RecoveryKind, ResumeContext
from greenbook_agent_core.agent.state import AgentState, Observation
from greenbook_agent_core.capability.registry import get_capability_registry
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_contracts.tool_contract import ToolMetadata, ToolPolicyMetadata


class _LLM:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.payloads:
            raise AssertionError("Fake LLM received more calls than expected")
        payload = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
            )],
        )


def _tool(name: str = "community.search_public_posts") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description="Search public GreenBook community posts",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        policy=ToolPolicyMetadata(risk_level="READ"),
    )


def _reasoning_tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="summarize_community",
        description="总结社区帖子共同方法",
        goal_type="ANALYZE",
        required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
    ))


def _reasoning_state(*, tool_results: list[dict[str, Any]] | None = None) -> AgentState:
    tree = _reasoning_tree()
    return AgentState(
        goal=tree.root_goal,
        goal_tree=tree,
        current_task=TaskNode(
            task_id="t_summarize",
            goal_id="summarize_community",
            capability="ANALYZE_CONTENT_PATTERNS",
        ),
        available_tools=[_tool()],
        tool_results=list(tool_results or []),
    )


def _search_result(*, ok: bool = True, items: list[dict[str, Any]] | None = None,
                   tool: str = "community.search_public_posts") -> dict[str, Any]:
    return {
        "ok": ok,
        "tool_name": tool,
        "tool_arguments": {"query": "Agent"},
        "data": {"total": len(items or []), "items": items or []},
    }


def _reasoning_loop() -> AgentLoop:
    return AgentLoop(llm=_LLM(), capability_registry=get_capability_registry())


def test_reasoning_synthesis_not_allowed_without_evidence() -> None:
    """A grounded reasoning Goal may not produce a result before real evidence."""
    loop = _reasoning_loop()
    state = _reasoning_state()
    assert loop._produce_result_allowed(state) is False


def test_reasoning_synthesis_allowed_with_search_evidence() -> None:
    loop = _reasoning_loop()
    state = _reasoning_state(tool_results=[
        _search_result(items=[{"post_id": "p1", "title": "a"}, {"post_id": "p2"}])
    ])
    assert loop._produce_result_allowed(state) is True


def test_reasoning_synthesis_not_allowed_on_failed_search() -> None:
    loop = _reasoning_loop()
    state = _reasoning_state(tool_results=[_search_result(ok=False, items=[])])
    assert loop._produce_result_allowed(state) is False


def test_reasoning_synthesis_not_allowed_on_empty_search() -> None:
    """A search that returned zero posts is not grounding evidence."""
    loop = _reasoning_loop()
    state = _reasoning_state(tool_results=[_search_result(items=[])])
    assert loop._produce_result_allowed(state) is False


def test_reasoning_synthesis_ignores_non_evidence_write() -> None:
    """A write result (not a community read) cannot satisfy grounding."""
    loop = _reasoning_loop()
    state = _reasoning_state(tool_results=[
        {"ok": True, "tool_name": "content.create_draft",
         "tool_arguments": {"title": "x"}, "data": {"draft_id": "d1"}}
    ])
    assert loop._produce_result_allowed(state) is False


def _tree(*, composite: bool = False) -> GoalTree:
    if not composite:
        return GoalTree(root=Goal(
            goal_id="research_ai",
            description="搜索最近 AI 文章",
            goal_type="RESEARCH",
            required_capabilities=["SEARCH_COMMUNITY"],
        ))
    return GoalTree(root=Goal(
        goal_id="write_ai_article",
        description="分析热门文章然后写文章",
        goal_type="CREATE",
        children=[
            Goal(
                goal_id="research_ai",
                description="搜索最近 AI 文章",
                goal_type="RESEARCH",
                required_capabilities=["SEARCH_COMMUNITY"],
            ),
            Goal(
                goal_id="generate_article",
                description="根据热门文章写文章",
                goal_type="CREATE",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["research_ai"],
            ),
        ],
    ))


@pytest.mark.asyncio
async def test_reason_normalizes_nullable_json_mode_defaults() -> None:
    llm = _LLM(
        {
            "action": "TOOL_CALL",
            "tool_name": "content.get_draft",
            "tool_args": {},
            "goal_tree": None,
            "plan_patch": None,
            "question": None,
            "reason": "确认草稿",
            "confidence": 0.8,
        },
    )
    state = AgentState(
        goal=_tree().root_goal,
        command=Command(type=CommandType.QUERY, objective="查看草稿"),
        goal_tree=_tree(),
    )

    action = await AgentLoop(llm=llm).reason(Observation(), state)

    assert action.action == AgentActionType.TOOL_CALL
    assert action.plan_patch == {}
    assert action.question == ""


@pytest.mark.asyncio
async def test_reason_tolerates_echoed_goal_tree_envelope_fields() -> None:
    """Real-chain regression: a reasoning model occasionally echoes GoalTree /
    Command envelope fields (goals, task_nodes, command_id, source) into the
    AgentAction object.  They are not part of the action contract; the reason
    path must strip them so the decision survives instead of failing
    extra_forbidden (observed: 3 parse failures + failed repair →
    STRUCTURED_OUTPUT_INVALID)."""
    llm = _LLM({
        "action": "CREATE_TASK",
        "goal_tree": None,
        "plan_patch": {},
        "reason": "需要生成并排期发布",
        "confidence": 0.9,
        # Model-echoed envelope fields that AgentAction forbids:
        "goals": [],
        "task_nodes": [{
            "task_id": "g2:1",
            "goal_id": "g2",
            "capability": "SEARCH_COMMUNITY",
            "status": "PENDING",
        }],
        "command_id": "2dc8c0b8-13a5-4ff3-8e96-c3af33127a4f",
        "source": "LLM_STRUCTURED_OUTPUT",
        "version": 1,
    })
    state = AgentState(
        goal=_tree().root_goal,
        command=Command(type=CommandType.CREATE, objective="写帖子并发布"),
        goal_tree=_tree(),
    )

    action = await AgentLoop(llm=llm).reason(Observation(), state)

    assert action.action == AgentActionType.CREATE_TASK
    assert action.goal_tree is None
    assert action.reason == "需要生成并排期发布"


@pytest.mark.asyncio
async def test_agent_selects_search_tool_from_metadata() -> None:
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {
            "tool_name": "community.search_public_posts",
            "arguments": {"query": "AI"},
            "reason": "The metadata describes community search.",
            "confidence": 0.95,
        },
        {"finished": True, "needs_next_step": False, "reason": "Search completed."},
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def raw_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, args))
        return {"ok": True, "data": {"items": [{"title": "AI trend"}]}}

    result = await AgentLoop(llm=llm, max_iterations=2).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _tree(),
        available_tools=[_tool()],
        tool_runtime=ToolRuntime(raw_tool, ledger=ToolExecutionLedger()),
    )

    assert result.success is True
    assert calls == [("community.search_public_posts", {"query": "AI"})]
    assert result.actions[0]["action"] == AgentActionType.TOOL_CALL.value
    assert len(llm.calls) == 3  # Reason -> Selector -> Reflect


@pytest.mark.asyncio
async def test_reason_receives_consumed_read_evidence_constraints() -> None:
    llm = _LLM(
        {
            "action": "FINISH",
            "reason": "The existing read evidence is sufficient.",
        },
    )
    tool = _tool()
    state = AgentState(
        goal=_tree().root_goal,
        goal_tree=_tree(),
        available_tools=[tool],
        tool_results=[
            {
                "ok": True,
                "tool_name": tool.name,
                "tool_arguments": {"query": "Agent Memory"},
                "data": {"items": [{"post_id": "p1"}]},
            }
        ],
    )

    action = await AgentLoop(llm=llm).reason(
        Observation(tool_results=list(state.tool_results)),
        state,
    )

    assert action.action == AgentActionType.FINISH
    request = json.loads(llm.calls[0]["messages"][1]["content"])
    constraints = request["runtime_evidence_constraints"]
    assert constraints["same_scope_read_redispatch"] == "FORBIDDEN"
    assert constraints["consumed_read_evidence"][0]["tool_name"] == tool.name


@pytest.mark.asyncio
async def test_reflection_ignores_provider_schema_metadata_only() -> None:
    llm = _LLM({
        "finished": True,
        "needs_next_step": False,
        "reason": "The observation is sufficient.",
        "additionalProperties": False,
    })
    state = AgentState(
        goal=_tree().root_goal,
        command=Command(type=CommandType.QUERY, objective="Search AI posts"),
        goal_tree=_tree(),
    )

    reflection = await AgentLoop(llm=llm).reflect(
        Observation(),
        AgentAction(action=AgentActionType.TOOL_CALL),
        {"ok": True},
        state,
    )

    assert reflection.finished is True
    assert reflection.needs_next_step is False


@pytest.mark.asyncio
async def test_agent_searches_then_creates_next_execution_task() -> None:
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {"tool_name": "community.search_public_posts", "arguments": {"query": "AI"}},
        {"finished": False, "needs_next_step": True, "reason": "Use the research for writing."},
        {"action": "CREATE_TASK", "reason": "Compile the remaining writing Goal."},
        {"finished": True, "needs_next_step": False, "reason": "The task was submitted."},
    )
    tool_calls: list[str] = []
    executions: list[tuple[str, int]] = []

    async def tool_runtime(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(name)
        return {"ok": True, "data": {"items": [{"title": "AI trend"}]}}

    async def execution_runtime(*, graph: Any, plan: Any, state: Any) -> dict[str, Any]:
        executions.append((plan.plan_source, len(graph.nodes)))
        assert state.goal_tree is not None
        return {
            "ok": True,
            "success": True,
            "status": "COMPLETED",
            "task_id": "task-ai-article",
            "execution_id": "execution-ai-article",
        }

    result = await AgentLoop(llm=llm, max_iterations=4).run(
        Command(type=CommandType.CREATE, objective="分析热门文章然后写文章"),
        _tree(composite=True),
        available_tools=[_tool()],
        tool_runtime=tool_runtime,
        execution_runtime=execution_runtime,
    )

    assert result.success is True
    assert tool_calls == ["community.search_public_posts"]
    assert executions == [("GOAL_RUNTIME", 2)]
    assert [item["action"] for item in result.actions] == [
        AgentActionType.TOOL_CALL.value,
        AgentActionType.CREATE_TASK.value,
    ]
    assert result.execution_results[-1]["execution_id"] == "execution-ai-article"


@pytest.mark.asyncio
async def test_queued_task_handoff_stops_agent_loop_without_duplicate_submission() -> None:
    llm = _LLM(
        {"action": "CREATE_TASK", "reason": "Submit the writing plan."},
        # This response must not be consumed: queue acceptance is the runtime
        # hand-off boundary, so the AgentLoop must not ask for another action.
        {"finished": False, "needs_next_step": True, "reason": "Submit again."},
    )

    async def execution_runtime(*, graph: Any, plan: Any, state: Any) -> dict[str, Any]:
        del graph, plan, state
        return {
            "status": "QUEUED",
            "execution_id": "execution-queued",
            "task_id": "task-queued",
        }

    result = await AgentLoop(llm=llm, max_iterations=3).run(
        Command(type=CommandType.CREATE, objective="写一篇 Agent 文章"),
        _tree(),
        execution_runtime=execution_runtime,
    )

    assert result.success is False
    assert result.status.value == "RUNNING"
    assert result.execution_results[-1]["execution_id"] == "execution-queued"
    assert [item["action"] for item in result.actions] == [
        AgentActionType.CREATE_TASK.value,
    ]
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_resumed_loop_advances_past_completed_search_to_next_goal() -> None:
    """Regression: a resumed AgentLoop whose first capability already completed
    (queued hand-off + ActionObservation) must select the NEXT TaskNode of the
    Goal instead of re-observing the completed step and stalling. Mirrors the
    multi-task delta path where the first task can otherwise stop after its
    first capability (observed live: the first of three posts never generated).
    """
    llm = _LLM(
        {"action": "CREATE_TASK", "reason": "Compile the writing Goal now."},
        # Must not be consumed: queue acceptance stops the loop after submit.
        {"finished": False, "needs_next_step": True, "reason": "Submit again."},
    )
    submitted: list[str] = []

    async def execution_runtime(*, graph: Any, plan: Any, state: Any) -> dict[str, Any]:
        del graph, state
        submitted.append(plan.plan_source)
        return {
            "status": "QUEUED",
            "execution_id": "execution-resumed",
            "task_id": "task-resumed",
        }

    result = await AgentLoop(llm=llm, max_iterations=3).run(
        Command(type=CommandType.CREATE, objective="分析热门文章然后写文章"),
        _tree(composite=True),
        execution_runtime=execution_runtime,
        resume_context=ResumeContext(
            task_id="task-resumed",
            execution_id="execution-search",
            completed_step_ids=["research_ai:1"],
            recovery_action=RecoveryKind.RESUME_EXECUTION,
        ),
    )

    assert result.status.value == "RUNNING"
    assert [item["action"] for item in result.actions] == [
        AgentActionType.CREATE_TASK.value,
    ]
    assert submitted == ["GOAL_RUNTIME"]
    assert len(llm.calls) == 1
    assert result.execution_results[-1]["execution_id"] == "execution-resumed"


@pytest.mark.asyncio
async def test_failed_tool_causes_agent_to_update_goal_plan() -> None:
    updated_tree = _tree()
    llm = _LLM(
        {"action": "TOOL_CALL", "tool_args": {"query": "AI"}},
        {"tool_name": "community.search_public_posts", "arguments": {"query": "AI"}},
        {"finished": False, "needs_next_step": True, "retry": True, "reason": "Retry with a revised plan."},
        {
            "action": "UPDATE_PLAN",
            "goal_tree": updated_tree.model_dump(mode="json"),
            "reason": "The search tool failed; keep the Goal explicit.",
        },
        {"finished": False, "needs_next_step": True, "reason": "Plan updated."},
        {"action": "FINISH", "reason": "Waiting for the next user turn."},
    )

    async def failing_tool(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "TIMEOUT",
            "retryable": True,
            "message": "Search timed out",
        }

    result = await AgentLoop(llm=llm, max_iterations=5).run(
        Command(type=CommandType.QUERY, objective="搜索最近AI文章"),
        _tree(),
        available_tools=[_tool()],
        tool_runtime=failing_tool,
    )

    assert result.success is True
    assert [item["action"] for item in result.actions] == [
        AgentActionType.TOOL_CALL.value,
        AgentActionType.UPDATE_PLAN.value,
        AgentActionType.FINISH.value,
    ]
    assert result.tool_results[0]["error_code"] == "TIMEOUT"
    assert result.state is not None
    assert result.state.goal_tree is not None


# ── completed_goal_ids / completed_task_ids namespace separation ──────────


def test_execution_read_evidence_exposes_search_post_ids() -> None:
    """Regression: SEARCH_RESULT executions must surface their referenced post
    ids as consumed evidence so a follow-up GET_POST_DETAIL step can pick a
    real post_id (observed live: the first of three tasks looped 20+ search
    iterations because the ids were invisible to the model)."""
    from greenbook_agent_core.agent.loop import _execution_read_evidence

    search_tool = ToolMetadata(
        name="community.search_public_posts",
        description="Search public posts",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        policy=ToolPolicyMetadata(risk_level="READ"),
        capabilities=["SEARCH_COMMUNITY"],
    )
    state = AgentState(
        goal=Goal(goal_id="research_ai", description="search", required_capabilities=["SEARCH_COMMUNITY"]),
        command=Command(type=CommandType.QUERY, objective="search"),
        goal_tree=GoalTree(root=Goal(
            goal_id="research_ai",
            description="search",
            required_capabilities=["SEARCH_COMMUNITY"],
        )),
    )
    state.available_tools = [search_tool]
    state.context_snapshot = {
        "execution_states": [{
            "goal_id": "research_ai",
            "capability": "SEARCH_COMMUNITY",
            "status": "COMPLETED",
            "steps": [{
                "goal_id": "research_ai",
                "capability": "SEARCH_COMMUNITY",
                "status": "COMPLETED",
                "output_artifact": {
                    "artifact_type": "SEARCH_RESULT",
                    "resource_refs": [
                        {"kind": "POST", "resource_id": "p1"},
                        {"kind": "POST", "resource_id": "p2"},
                    ],
                },
            }],
        }],
    }
    evidence = _execution_read_evidence(state)
    assert evidence, "completed SEARCH execution must project read evidence"
    assert evidence[0]["post_ids"] == ["p1", "p2"]
    assert evidence[0]["resource_count"] == 2


def _make_three_goal_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="search analyze write",
            goal_type="CREATE",
            children=[
                Goal(
                    goal_id="g-search",
                    description="search",
                    required_capabilities=["SEARCH_COMMUNITY"],
                ),
                Goal(
                    goal_id="g-analyze",
                    description="analyze",
                    required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
                    dependencies=["g-search"],
                ),
                Goal(
                    goal_id="g-write",
                    description="write",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                    dependencies=["g-analyze"],
                ),
            ],
        ),
        task_nodes=[
            {"task_id": "t-search", "goal_id": "g-search", "capability": "SEARCH_COMMUNITY"},
            {"task_id": "t-analyze", "goal_id": "g-analyze", "capability": "ANALYZE_CONTENT_PATTERNS"},
            {"task_id": "t-write", "goal_id": "g-write", "capability": "GENERATE_CONTENT"},
        ],
    )


def test_next_task_skips_tasks_of_completed_goal_from_resume() -> None:
    """A resumed completed_goal_id (goal namespace) must skip every TaskNode
    of that Goal, even though task_ids are a different namespace."""
    from greenbook_agent_core.agent.loop import _next_task

    tree = _make_three_goal_tree()
    state = AgentState(
        goal=tree.root_goal,
        goal_tree=tree,
        command=Command(type=CommandType.CREATE, objective="search analyze write"),
        # Resume says g-search is already satisfied (durable fact).
        completed_goal_ids=["g-search"],
        completed_task_ids=[],
        context_snapshot={"execution_states": []},
    )
    state.current_task = _next_task(state)
    # g-search's task is skipped even though no task_id matches a goal id.
    assert state.current_task is None or state.current_task.goal_id != "g-search"
    assert "t-search" in state.completed_task_ids
    assert "g-search" in state.completed_goal_ids


def test_next_task_keeps_goal_and_task_namespaces_separate() -> None:
    """completed_task_ids entries are task/step ids; goal completion is tracked
    separately.  A satisfied Goal adds its id to completed_goal_ids and its
    TaskNodes to completed_task_ids."""
    from greenbook_agent_core.agent.loop import _next_task

    tree = _make_three_goal_tree()
    state = AgentState(
        goal=tree.root_goal,
        goal_tree=tree,
        command=Command(type=CommandType.CREATE, objective="search analyze write"),
        completed_goal_ids=[],
        completed_task_ids=[],
        context_snapshot={
            "execution_states": [
                {"goal_id": "g-search", "capability": "SEARCH_COMMUNITY", "status": "COMPLETED"},
            ],
        },
    )
    state.current_task = _next_task(state)
    assert "g-search" in state.completed_goal_ids
    assert "t-search" in state.completed_task_ids
    # The next executable task is g-analyze's task, not g-search's.
    assert state.current_task is None or state.current_task.goal_id != "g-search"
