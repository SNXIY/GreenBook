"""Phase 4.4 focused tests: Goal execution semantics (TOOL/REASONING/CONTROL),
reasoning result persistence + lineage + satisfaction, and Search arg binding.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent import AgentAction, AgentActionType, AgentLoop
from greenbook_agent_core.agent.loop import StructuredOutputError
from greenbook_agent_core.agent.state import AgentState
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command import Command, CommandType
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.goal.satisfaction import goal_is_satisfied


class _LLM:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"Fake LLM received more calls than expected: {len(self.calls)}")
        payload = self.responses.pop(0)
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry()


def _analysis_tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="g3",
        description="总结社区对 Agent 的讨论主题",
        goal_type="ANALYZE",
        required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
        target={"source_artifact": "artifact-search-18"},
    ))


def _analysis_state_with_evidence() -> AgentState:
    """A reasoning-backed Goal state already grounded by a real SEARCH result.

    The readiness guard only lets a reasoning Goal PRODUCE_RESULT after real
    community evidence exists in the current run; these tests exercise the
    recorder/validation layer, so they seed that evidence.
    """
    tree = _analysis_tree()
    return AgentState(
        goal=tree.root_goal,
        goal_tree=tree,
        current_task=TaskNode(
            task_id="t_analysis", goal_id="g3", capability="ANALYZE_CONTENT_PATTERNS",
        ),
        conversation_context={
            "task_id": "t_analysis",
            "conversation_id": "c1",
            "user_id": "u1",
            "tenant_id": "t1",
        },
        tool_results=[{
            "ok": True,
            "tool_name": "community.search_public_posts",
            "tool_arguments": {"query": "Agent"},
            "data": {"items": [{"post_id": "p1", "title": "Agent 工程"}]},
        }],
    )


def _produce_action(*, source_refs: list[str], payload: dict[str, Any]) -> AgentAction:
    return AgentAction(
        action=AgentActionType.PRODUCE_RESULT,
        result_type="CONTENT_ANALYSIS",
        source_refs=source_refs,
        result_payload=payload,
        reason="分析搜索结果。",
    )


def _multi_tree() -> GoalTree:
    return GoalTree(root=Goal(
        goal_id="root",
        description="搜索并总结再写文章",
        goal_type="CREATE",
        children=[
            Goal(
                goal_id="g2",
                description="搜索社区 Agent 帖子",
                goal_type="RESEARCH",
                required_capabilities=["SEARCH_COMMUNITY"],
                target={"keyword": "Agent"},
            ),
            Goal(
                goal_id="g3",
                description="总结讨论主题",
                goal_type="ANALYZE",
                required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
                dependencies=["g2"],
            ),
            Goal(
                goal_id="g4",
                description="根据观点写一篇文章",
                goal_type="CREATE",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["g3"],
                publication_intent="DRAFT_ONLY",
            ),
        ],
    ))


# ── capability classification ──────────────────────────────────────────────


def test_analyze_is_reasoning_backed() -> None:
    from greenbook_agent_core.agent.loop import _is_reasoning_capability

    cap = _registry().get("ANALYZE_CONTENT_PATTERNS")
    assert cap is not None
    assert _is_reasoning_capability(cap) is True
    search = _registry().get("SEARCH_COMMUNITY")
    assert search is not None
    assert _is_reasoning_capability(search) is False


# ── Search query binding (goal target.keyword -> tool query) ───────────────


def test_search_query_derived_from_goal_target_keyword() -> None:
    tree = GoalTree(root=Goal(
        goal_id="g2",
        description="搜索社区 Agent 帖子",
        goal_type="RESEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
        target={"keyword": "Agent"},
    ))
    plan = GoalCompiler(registry=_registry()).compile_plan(tree, task_id="t1")
    step = next(s for s in plan.steps if s.capability == "SEARCH_COMMUNITY")
    assert step.constraints.get("query") == "Agent"


# ── reasoning-backed goal executes in AgentLoop, no ToolSelector ───────────


@pytest.mark.asyncio
async def test_reasoning_goal_produce_result_no_tool_call() -> None:
    """A grounded reasoning Goal produces its result without calling a tool."""
    recorded: dict[str, Any] = {}

    async def recorder(**kwargs: Any) -> dict[str, Any]:
        recorded.update(kwargs)
        return {"execution_id": "exec-reasoning-1", "artifact_id": "art-analysis-1"}

    action = _produce_action(
        source_refs=["artifact-search-18"],
        payload={"summary": "社区主要讨论 Agent 落地工程实践与多智能体协作。",
                 "key_points": ["工程实践", "多智能体协作"]},
    )
    result = await AgentLoop(llm=_LLM(), capability_registry=_registry())._produce_reasoning_result(
        action,
        _analysis_state_with_evidence(),
        reasoning_result_recorder=recorder,
    )

    assert result["ok"] is True
    assert result["artifact_id"] == "art-analysis-1"
    assert recorded["goal_id"] == "g3"
    assert recorded["capability"] == "ANALYZE_CONTENT_PATTERNS"
    assert "Agent" in str(recorded["payload"].get("summary"))


@pytest.mark.asyncio
async def test_reasoning_goal_invalid_result_is_controlled_failure() -> None:
    with pytest.raises(StructuredOutputError) as exc:
        await AgentLoop(llm=_LLM(), capability_registry=_registry())._produce_reasoning_result(
            _produce_action(source_refs=[], payload={}),
            _analysis_state_with_evidence(),
            reasoning_result_recorder=None,
        )
    assert exc.value.code == "REASONING_RESULT_INVALID"
    assert "recorder" not in str(exc.value.technical)


# ── satisfaction for reasoning-backed goals ────────────────────────────────


def test_reasoning_goal_satisfied_when_capability_completed() -> None:
    goal = _analysis_tree().root_goal
    assert goal_is_satisfied(goal, {"completed_capabilities": []}) is False
    assert goal_is_satisfied(
        goal,
        {"completed_capabilities": ["ANALYZE_CONTENT_PATTERNS"]},
    ) is True


# ── multi-capability goals must not be satisfied by a partial completion ──


def _draft_schedule_goal() -> Goal:
    """A delta-style Goal with no publication_intent but two capabilities."""
    return Goal(
        goal_id="delta-goal-1",
        description="写一篇帖子，五分钟之后发布",
        goal_type="TASK",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        constraints=[{"run_at": "2026-08-14T13:44:51Z"}],
    )


def test_multi_capability_goal_not_satisfied_by_draft_alone() -> None:
    goal = _draft_schedule_goal()
    # Draft exists but SCHEDULE_PUBLISH has not run: the Goal must stay open.
    assert goal_is_satisfied(
        goal,
        {
            "draft_id": "D1",
            "completed_capabilities": ["GENERATE_CONTENT"],
        },
    ) is False


def test_multi_capability_goal_satisfied_only_when_all_capabilities_done() -> None:
    goal = _draft_schedule_goal()
    assert goal_is_satisfied(
        goal,
        {
            "draft_id": "D1",
            "completed_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        },
    ) is True
    assert goal_is_satisfied(
        goal,
        {
            "draft_id": "D1",
            "completed_capabilities": ["GENERATE_CONTENT"],
            "artifact_types": [],
        },
    ) is False


# ── downstream lineage injection ────────────────────────────────────────────


def test_inject_reasoning_context_feeds_downstream_step() -> None:
    from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan

    from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
        _inject_reasoning_context,
    )

    state = AgentState(
        goal=_multi_tree().root_goal,
        goal_tree=_multi_tree(),
        command=Command(type=CommandType.CREATE, objective="搜索总结写文章"),
        execution_results=[{
            "ok": True,
            "status": "COMPLETED",
            "goal_id": "g3",
            "capability": "ANALYZE_CONTENT_PATTERNS",
            "reasoning_result": {"summary": "社区主要讨论工程实践。"},
        }],
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="INCREMENTAL",
        steps=[PlanStep(
            step_id="g4:1",
            ordinal=1,
            capability="GENERATE_CONTENT",
            tool_name="content.create_draft",
            goal_id="g4",
            depends_on=["g3:1"],
            constraints={"instruction": "写一篇文章"},
        )],
    )
    _inject_reasoning_context(state, plan)
    assert "社区主要讨论工程实践" in plan.steps[0].constraints.get("summary", "")
    assert "社区主要讨论工程实践" in plan.steps[0].constraints.get("instruction", "")


# ── durable continuation credential broker ─────────────────────────────────


def test_credential_broker_resolve_identity_reconstructs_auth() -> None:
    from greenbook_contracts.identity import AuthContext

    from apps.agent_api.greenbook_agent_api.services.execution_credential_broker import (
        ExecutionCredentialBroker,
    )

    broker = ExecutionCredentialBroker()
    broker.register(AuthContext(
        user_id="u1",
        tenant_id="t1",
        roles=["USER"],
        raw_access_token="jwt-token",
    ))
    resolved = broker.resolve_identity("u1", "t1")
    assert resolved is not None
    assert resolved.raw_access_token == "jwt-token"
    # Wrong user must not resolve another user's credential.
    assert broker.resolve_identity("u2", "t1") is None
    assert broker.resolve_identity("u1", "t2") is None


# ── reasoning result must be durably persisted before it counts as complete ─


@pytest.mark.asyncio
async def test_reasoning_result_without_persisted_execution_is_controlled_failure() -> None:
    """A recorder that returns no durable execution_id must not fabricate
    COMPLETED evidence: the Goal (and Run) must not be reported complete
    without a persisted fact (design goal 0813: intermediate steps are never
    reported as completed)."""

    async def recorder(**kwargs: Any) -> dict[str, Any]:
        # Recorder exists but does not confirm a durable write.
        return {}

    with pytest.raises(StructuredOutputError) as exc:
        await AgentLoop(llm=_LLM(), capability_registry=_registry())._produce_reasoning_result(
            _produce_action(
                source_refs=["artifact-search-18"],
                payload={"summary": "社区主要讨论 Agent 工程实践。", "key_points": ["工程实践"]},
            ),
            _analysis_state_with_evidence(),
            reasoning_result_recorder=recorder,
        )
    assert exc.value.code == "REASONING_RESULT_NOT_PERSISTED"
    # The technical detail must not leak into the user-facing message.
    assert "recorder" not in str(exc.value)


@pytest.mark.asyncio
async def test_reasoning_result_without_recorder_is_controlled_failure() -> None:
    """Without any recorder there is no durable boundary; the loop must fail
    closed instead of claiming completion from in-memory evidence."""
    with pytest.raises(StructuredOutputError) as exc:
        await AgentLoop(llm=_LLM(), capability_registry=_registry())._produce_reasoning_result(
            _produce_action(
                source_refs=["artifact-search-18"],
                payload={"summary": "社区主要讨论 Agent 工程实践。", "key_points": ["工程实践"]},
            ),
            _analysis_state_with_evidence(),
            reasoning_result_recorder=None,
        )
    assert exc.value.code == "REASONING_RESULT_NOT_PERSISTED"


# ── user-safe error projection ──────────────────────────────────────────────


def test_tool_selection_errors_are_user_safe() -> None:
    from greenbook_agent_core.agent.loop import _user_safe_error

    for code in (
        "TOOL_SELECTION_EMPTY",
        "TOOL_SELECTION_INVALID_JSON",
        "TOOL_SELECTION_SCHEMA_INVALID",
        "TOOL_NOT_IN_CATALOG",
        "TOOL_POLICY_DENIED",
        "GOAL_NOT_SATISFIED",
        "REASONING_RESULT_NOT_PERSISTED",
    ):
        message = _user_safe_error(code, f"raw technical detail for {code}")
        assert message != f"raw technical detail for {code}", code
        assert "LLM" not in message, code
        assert "JSON" not in message, code

    # Raw Pydantic / compiler text is also projected to a safe message.
    assert _user_safe_error("", "GoalTree could not be compiled into TaskGraph.")
    assert "could not be compiled" not in _user_safe_error(
        "", "GoalTree could not be compiled into TaskGraph."
    )
    assert _user_safe_error("", "Pydantic validation errors for AgentAction")


# ── per-conversation idempotency and bounded state (design goal 0813) ───────


def test_message_idempotency_returns_same_run_for_duplicate() -> None:
    from greenbook_agent_core.runtime.container import RuntimeContainer

    from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
        ConversationRuntimeAdapter,
    )

    adapter = ConversationRuntimeAdapter(container=RuntimeContainer.for_testing())
    first = adapter._claim_message_idempotency(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="帮我写一篇文章", idempotency_key="", run_id="run-1",
    )
    assert first == "run-1"
    duplicate = adapter._claim_message_idempotency(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="帮我写一篇文章", idempotency_key="", run_id="run-2",
    )
    assert duplicate == "run-1", "an identical message must be deduplicated"

    distinct = adapter._claim_message_idempotency(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="帮我写另一篇", idempotency_key="", run_id="run-3",
    )
    assert distinct == "run-3", "a different message must always run"


def test_message_idempotency_explicit_key_wins() -> None:
    from greenbook_agent_core.runtime.container import RuntimeContainer

    from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
        ConversationRuntimeAdapter,
    )

    adapter = ConversationRuntimeAdapter(container=RuntimeContainer.for_testing())
    first = adapter._claim_message_idempotency(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="任意内容", idempotency_key="client-key-1", run_id="run-a",
    )
    assert first == "run-a"
    duplicate = adapter._claim_message_idempotency(
        conversation_id="c1", user_id="u1", tenant_id="t1",
        message="完全不同的内容", idempotency_key="client-key-1", run_id="run-b",
    )
    assert duplicate == "run-a", "the explicit Idempotency-Key must deduplicate"


def test_conversation_semaphore_cache_is_bounded() -> None:
    from greenbook_agent_core.runtime.container import RuntimeContainer

    from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
        ConversationRuntimeAdapter,
    )

    adapter = ConversationRuntimeAdapter(container=RuntimeContainer.for_testing())
    adapter._conversation_semaphore_cap = 8
    for i in range(20):
        adapter._semaphore_for_conversation(f"conversation-{i}")
    assert len(adapter._conversation_work_semaphores) <= 8, (
        "the per-conversation semaphore cache must never grow unbounded"
    )


# ── business Objective requires ALL capabilities (not first-only) ─────────


def _res(resource_id, kind):
    from greenbook_agent_core.task.models import TaskResourceRef
    return TaskResourceRef(resource_id=resource_id, resource_kind=kind)


def test_business_objective_draft_only_not_complete() -> None:
    """An Objective requiring GENERATE_CONTENT + SCHEDULE_PUBLISH is NOT complete
    with only a verified DRAFT — it must also have a verified SCHEDULE."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="Java学习", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-1", "DRAFT")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is False


def test_business_objective_draft_and_schedule_complete() -> None:
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="Java学习", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                    related_resource_ids=["draft-1", "sched-1"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-1", "DRAFT"), _res("sched-1", "SCHEDULE")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is True


def test_business_objective_generate_only_complete_on_draft() -> None:
    """An Objective requiring only GENERATE_CONTENT completes on a verified DRAFT."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="Agent草稿", required_capabilities=["GENERATE_CONTENT"], related_resource_ids=["draft-1"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-1", "DRAFT")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is True


# ── MUTATION caps are NOT satisfied by resource existence ─────────────────


def test_manage_draft_not_satisfied_by_existing_draft() -> None:
    """MANAGE_DRAFT must NOT be satisfied merely because a DRAFT exists (the
    draft existed before the mutation).  It needs a verified postcondition."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="改标题", required_capabilities=["MANAGE_DRAFT"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-1", "DRAFT")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is False


def test_cancel_schedule_not_satisfied_by_existing_schedule() -> None:
    """CANCEL_SCHEDULE must NOT be satisfied by an existing SCHEDULE — that is
    exactly the pre-cancel state.  Needs a verified cancelled postcondition."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="取消", required_capabilities=["CANCEL_SCHEDULE"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("sched-1", "SCHEDULE")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is False


def test_mutation_satisfied_by_verified_operation_binding() -> None:
    """A mutation Objective completes when it has an explicit verified operation
    binding (the requested postcondition), not by resource existence."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj = Objective(task_id="t", intent="改标题", required_capabilities=["MANAGE_DRAFT"],
                    related_operations=["op-update-draft-1"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-1", "DRAFT")], execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj) is True


# ── per-Objective resource ownership (T4) ────────────────────────────────


def test_t4a_objective_b_does_not_use_objective_a_schedule() -> None:
    """A owns Draft A + Schedule A; B owns only Draft B.  B must NOT be satisfied
    by A's Schedule (no cross-objective pollution)."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj_a = Objective(task_id="t", intent="A", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                      related_resource_ids=["draft-a", "sched-a"])
    obj_b = Objective(task_id="t", intent="B", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                      related_resource_ids=["draft-b"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-a", "DRAFT"), _res("draft-b", "DRAFT"), _res("sched-a", "SCHEDULE")],
                execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj_a) is True
    assert is_objective_satisfied(task, obj_b) is False  # Schedule A must not satisfy B


def test_t4b_reverse_no_pollution() -> None:
    """A owns only Draft A; B owns Draft B + Schedule B.  A must stay IN_PROGRESS."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj_a = Objective(task_id="t", intent="A", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                      related_resource_ids=["draft-a"])
    obj_b = Objective(task_id="t", intent="B", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                      related_resource_ids=["draft-b", "sched-b"])
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-a", "DRAFT"), _res("draft-b", "DRAFT"), _res("sched-b", "SCHEDULE")],
                execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj_a) is False
    assert is_objective_satisfied(task, obj_b) is True


# ── new business Objective: NO task-global fallback (T4e) ────────────────


def test_t4e_new_objective_no_global_fallback() -> None:
    """A new business Objective (required_capabilities present) with EMPTY
    ownership must NOT be satisfied by OTHER objectives' resources in the task."""
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus
    from greenbook_agent_core.task.objective_reducer import is_objective_satisfied
    obj_b = Objective(task_id="t", intent="B", required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                      related_resource_ids=[])  # B owns nothing
    task = Task(task_id="t", conversation_id="c1", user_id="u1", tenant_id="t1", status=TaskStatus.RUNNING,
                resource_index=[_res("draft-a", "DRAFT"), _res("sched-a", "SCHEDULE")],  # A's resources
                execution_refs=[], plan_history=[])
    assert is_objective_satisfied(task, obj_b) is False
