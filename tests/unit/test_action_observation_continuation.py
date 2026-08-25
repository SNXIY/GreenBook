"""Observation-Driven continuation MVP tests.

Generate -> durable ActionObservation -> AgentLoop resume -> Schedule.

Covers the focused acceptance cases: real business evidence in the
observation, owned-draft isolation, idempotent persistence and claim,
failure never schedules, worker completion persists the observation before
dispatch, and the read-only fast path stays out of incremental mode.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.main import observation_opens_continuation
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
    _incremental_plan,
)
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.execution.action_observation import (
    INCREMENTAL_PLAN_SOURCE,
    ActionObservation,
    ActionObservationStore,
    ActionObservationWriter,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan

# ── helpers ────────────────────────────────────────────────────────────


def _generate_schedule_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="Write a Redis post and schedule it",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            publication_intent="SCHEDULED_PUBLISH",
            temporal_constraint={"run_at": "2026-08-14T10:00:00+08:00"},
        ),
        task_nodes=[
            TaskNode(task_id="g1:1", goal_id="g1", capability="GENERATE_CONTENT"),
            TaskNode(
                task_id="g1:2",
                goal_id="g1",
                capability="SCHEDULE_PUBLISH",
                dependencies=["g1:1"],
            ),
        ],
    )


def _search_analyze_tree() -> GoalTree:
    """Full multi-step Goal (search -> detail -> analyze -> generate -> schedule).

    Mirrors the multi-task delta path where a task's first capability is a
    read, the middle capability is reasoning-backed (must stay in the loop via
    PRODUCE_RESULT), and the write capabilities are durable submissions.
    """
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="Search Java interview posts, analyze them, then write and schedule",
            goal_type="CREATE",
            required_capabilities=[
                "SEARCH_COMMUNITY",
                "GET_POST_DETAIL",
                "ANALYZE_CONTENT_PATTERNS",
                "GENERATE_CONTENT",
                "SCHEDULE_PUBLISH",
            ],
            publication_intent="SCHEDULED_PUBLISH",
        ),
        task_nodes=[
            TaskNode(task_id="g1:1", goal_id="g1", capability="SEARCH_COMMUNITY"),
            TaskNode(
                task_id="g1:2",
                goal_id="g1",
                capability="GET_POST_DETAIL",
                dependencies=["g1:1"],
            ),
            TaskNode(
                task_id="g1:3",
                goal_id="g1",
                capability="ANALYZE_CONTENT_PATTERNS",
                dependencies=["g1:1", "g1:2"],
            ),
            TaskNode(
                task_id="g1:4",
                goal_id="g1",
                capability="GENERATE_CONTENT",
                dependencies=["g1:3"],
            ),
            TaskNode(
                task_id="g1:5",
                goal_id="g1",
                capability="SCHEDULE_PUBLISH",
                dependencies=["g1:4"],
            ),
        ],
    )


def _incremental_message(
    *,
    execution_id: str = "e1",
    capability: str = "GENERATE_CONTENT",
    goal_id: str = "g1",
) -> ExecutionQueueMessage:
    return ExecutionQueueMessage(
        execution_id=execution_id,
        trace_id="trace-1",
        payload={
            "conversation_id": "c1",
            "task_id": "t1",
            "run_id": "r1",
            "session": {
                "conversation_id": "c1",
                "user_id": "u1",
                "tenant_id": "ten1",
                "timezone": "Asia/Shanghai",
            },
            "execution_input": {
                "goal_id": goal_id,
                "execution_metadata": {
                    "plan_mode": "INCREMENTAL",
                    "goal_tree": _generate_schedule_tree().model_dump(mode="json"),
                    "command": _command().model_dump(mode="json"),
                },
                "steps": [
                    {"step_id": "g1:1", "goal_id": goal_id, "capability": capability},
                ],
            },
        },
    )


def _command() -> Command:
    return Command(
        type=CommandType.CREATE,
        goal="Write a Redis post and schedule it",
        objective="Write a Redis post and schedule it",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        raw_input="Write a Redis post and schedule it",
    )


def _draft_result(*, execution_id: str = "e1") -> RuntimeResult:
    return RuntimeResult(
        success=True,
        status="COMPLETED",
        run_id="r1",
        task_id="t1",
        execution_id=execution_id,
        summary="Draft created",
        artifacts=[
            {
                "artifact_id": "art-1",
                "artifact_type": "DRAFT",
                "type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": "draft-123",
                "title": "Redis 高并发优化",
                "status": "DRAFT",
                "step_id": "g1:1",
            }
        ],
    )


class _LLM:
    """Programmable structured-output client recording every request."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs):
        self.requests.append(dict(_kwargs))
        payload = self.responses.pop(0) if self.responses else {"action": "FINISH", "reason": ""}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(payload, ensure_ascii=False),
                ),
            )],
        )

    def user_payloads(self) -> list[dict[str, Any]]:
        return [
            json.loads(kwargs["messages"][1]["content"])
            for kwargs in self.requests
        ]


async def _make_adapter(
    llm: _LLM,
    *,
    observation: ActionObservation | None = None,
    runtime_service: Any | None = None,
    task_manager: Any | None = None,
) -> ConversationRuntimeAdapter:
    calls: dict[str, Any] = {"submit_plan": []}

    async def submit_plan(*, graph: Any, plan: Any, state: Any, **_extra) -> dict[str, Any]:
        calls["submit_plan"].append(plan)
        return {
            "execution_id": "e2",
            "status": "QUEUED",
            "queued": True,
            "ok": True,
            "message": "Execution accepted by the durable queue.",
            "plan_id": getattr(plan, "plan_id", ""),
        }

    class _SubmitService:
        container = SimpleNamespace(artifact_registry=None, capability_registry=None, tool_registry=None)
        _memory_mgr = None
        _artifact_store = None
        _execution_repository = None

        async def submit_plan(self, context, plan, *, completion_callback=None, **_extra):
            calls["submit_plan"].append(plan)
            return {
                "execution_id": "e2",
                "status": "QUEUED",
                "queued": True,
                "ok": True,
                "message": "Execution accepted by the durable queue.",
                "plan_id": getattr(plan, "plan_id", ""),
            }

    manager = task_manager or SimpleNamespace(
        create_task=async_identity,
        get_task=async_identity,
        bind_execution=async_identity,
        bind_goal_tree=async_identity,
        record_replan=async_identity,
    )

    class _ContextBuilder:
        """Simulate the durable ContextBuilder state after E1 completed.

        In production the builder reads the terminal Execution and its
        artifact from the database on every refresh, so the draft evidence
        survives each AgentLoop refresh cycle. The mock reproduces that
        persistence instead of returning an empty snapshot each time.
        """

        def __init__(self, observation: ActionObservation | None) -> None:
            self._observation = observation

        async def build(self, **kwargs):
            artifacts: list[dict[str, Any]] = []
            execution_states: list[dict[str, Any]] = []
            active_draft_id = ""
            active_schedule_id = ""
            if self._observation is not None:
                if self._observation.draft_id:
                    artifacts.append({
                        "artifact_id": next(iter(self._observation.artifact_refs), ""),
                        "resource_type": "DRAFT",
                        "resource_id": self._observation.draft_id,
                        "status": "DRAFT",
                    })
                    active_draft_id = self._observation.draft_id
                if self._observation.schedule_id:
                    artifacts.append({
                        "resource_type": "SCHEDULE",
                        "resource_id": self._observation.schedule_id,
                        "status": "SCHEDULED",
                    })
                    active_schedule_id = self._observation.schedule_id
                execution_states.append({
                    "execution_id": self._observation.execution_id,
                    "goal_id": self._observation.goal_id,
                    "capability": self._observation.capability,
                    "status": self._observation.status,
                    "draft_id": self._observation.draft_id,
                    "schedule_id": self._observation.schedule_id,
                    "error": self._observation.error,
                })
            ctx = SimpleNamespace(
                snapshot_id="s1",
                conversation_id="c1",
                user_id="u1",
                tenant_id="ten1",
                active_draft_id=active_draft_id,
                active_schedule_id=active_schedule_id,
                artifacts=artifacts,
                execution_states=execution_states,
            )

            def decision_payload() -> dict[str, Any]:
                return {
                    "conversation_id": ctx.conversation_id,
                    "user_id": ctx.user_id,
                    "tenant_id": ctx.tenant_id,
                    "active_draft_id": ctx.active_draft_id,
                    "active_schedule_id": ctx.active_schedule_id,
                    "artifacts": list(ctx.artifacts),
                    "execution_states": list(ctx.execution_states),
                }

            ctx.decision_payload = decision_payload
            return ctx

    class _ConversationService:
        async def get_conversation(self, *_args, **_kwargs):
            return SimpleNamespace(conversation_id="c1")

    adapter = ConversationRuntimeAdapter(
        command_runtime=None,
        goal_decomposer=None,
        agent_loop=AgentLoop(llm=llm, model="test-model"),
        goal_compiler=GoalCompiler(),
        task_manager=manager,
        task_provider=SimpleNamespace(persist_completion_projection=async_identity),
        runtime_service=_SubmitService(),
        conversation_service=_ConversationService(),
        context_builder=_ContextBuilder(observation),
    )
    adapter._submit_calls = calls  # type: ignore[attr-defined]
    return adapter


async def async_identity(*args: Any, **kwargs: Any) -> Any:
    task_id = kwargs.get("task_id") or kwargs.get("goal_id") or "t1"
    if kwargs.get("goal_tree") is not None:
        return SimpleNamespace(
            task_id=task_id,
            plan_version=1,
            goal_tree=kwargs["goal_tree"],
            goal_tree_snapshot=kwargs["goal_tree"].model_dump(mode="json"),
        )
    return SimpleNamespace(task_id=task_id, plan_version=1, goal_tree=None, goal_tree_snapshot=None)


def _observation(
    *,
    execution_id: str = "e1",
    status: str = "COMPLETED",
    draft_id: str = "draft-123",
    capability: str = "GENERATE_CONTENT",
    goal_id: str = "g1",
    tree: GoalTree | None = None,
    error: str = "",
) -> ActionObservation:
    return ActionObservation(
        execution_id=execution_id,
        task_id="t1",
        conversation_id="c1",
        goal_id=goal_id,
        capability=capability,
        status=status,
        draft_id=draft_id,
        artifact_refs=["art-1"],
        resource_refs=[{"resource_type": "DRAFT", "resource_id": draft_id, "artifact_id": "art-1"}],
        error=error,
        payload={
            "goal_tree": (tree or _generate_schedule_tree()).model_dump(mode="json"),
            "command": _command().model_dump(mode="json"),
            "session": {
                "conversation_id": "c1",
                "user_id": "u1",
                "tenant_id": "ten1",
                "timezone": "Asia/Shanghai",
            },
        },
    )


# ── test_action_observation_from_completed_generate ────────────────────


def test_action_observation_from_completed_generate() -> None:
    store = ActionObservationStore()
    writer = ActionObservationWriter(store=store)
    observation = writer.write(_incremental_message(), _draft_result(), _auth())
    assert observation is not None
    assert observation.execution_id == "e1"
    assert observation.goal_id == "g1"
    assert observation.capability == "GENERATE_CONTENT"
    assert observation.draft_id == "draft-123"
    assert observation.status == "COMPLETED"
    assert observation.artifact_refs == ["art-1"]
    # payload must be self-contained for a later resume
    assert observation.payload["goal_tree"]
    assert observation.payload["command"]
    assert observation.payload["session"]


def test_failed_observation_does_not_reenter_agent_loop() -> None:
    assert observation_opens_continuation(_observation(status="COMPLETED")) is True
    assert observation_opens_continuation(
        _observation(status="FAILED", draft_id="", error="FIELD_TOO_LONG")
    ) is False


def test_writer_ignores_whole_plan_executions() -> None:
    store = ActionObservationStore()
    writer = ActionObservationWriter(store=store)
    message = _incremental_message()
    message.payload["execution_input"]["execution_metadata"]["plan_mode"] = "WHOLE_PLAN"
    assert writer.write(message, _draft_result(), _auth()) is None
    assert store.count() == 0


def test_writer_is_idempotent_by_execution_id() -> None:
    store = ActionObservationStore()
    writer = ActionObservationWriter(store=store)
    writer.write(_incremental_message(), _draft_result(), _auth())
    writer.write(_incremental_message(), _draft_result(), _auth())
    assert store.count() == 1
    assert store.get_by_execution("e1") is not None


# ── test_worker_completion_persists_continuation_before_dispatch ───────


def test_worker_completion_persists_continuation_before_dispatch() -> None:
    store = ActionObservationStore()
    writer = ActionObservationWriter(store=store)
    published: list[bool] = []

    async def completion_publisher(_message, _result, _auth) -> None:
        published.append(True)

    async def execute_queued(message, **_kwargs):
        return _draft_result(execution_id=message.execution_id)

    def resolve(_message):
        return _auth()

    handler = RuntimeExecutionQueueHandler(
        service=SimpleNamespace(execute_queued=execute_queued),
        mcp=None,
        credential_resolver=resolve,
        completion_publisher=completion_publisher,
        observation_writer=writer,
    )
    result = None

    async def run() -> None:
        nonlocal result
        await handler(_incremental_message())

    import asyncio

    asyncio.run(run())
    assert published == [True]
    observation = store.get_by_execution("e1")
    assert observation is not None
    assert observation.draft_id == "draft-123"


# ── test_agent_loop_continues_after_generate_observation ───────────────


@pytest.mark.asyncio
async def test_agent_loop_continues_after_generate_observation() -> None:
    llm = _LLM([
        {
            "action": "CREATE_TASK",
            "tool_name": "",
            "tool_args": {},
            "reason": "Draft D1 is ready; schedule publication next",
            "confidence": 0.9,
        },
        {
            "finished": False,
            "needs_next_step": True,
            "retry": False,
            "adjust_plan": False,
            "reason": "",
        },
    ])
    adapter = await _make_adapter(llm)
    result = await adapter.continue_run(
        observation=_observation(),
        conversation_id="c1",
        user_id="u1",
        tenant_id="ten1",
        mcp=None,
        llm=llm,
        model="test-model",
        auth=_auth(),
    )
    plans = adapter._submit_calls["submit_plan"]  # type: ignore[attr-defined]
    assert plans, "AgentLoop must submit the next durable action"
    submitted = plans[-1]
    assert submitted.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in submitted.steps] == ["SCHEDULE_PUBLISH"]
    assert result.started_execution or result.status in {"RUNNING", "COMPLETED"}


@pytest.mark.asyncio
async def test_resume_after_search_advances_to_next_durable_step() -> None:
    """Regression: resuming a Goal whose first capability (SEARCH) completed
    must submit the NEXT durable step (GET_POST_DETAIL) instead of stalling or
    re-queuing the completed read. The reasoning-backed ANALYZE step must
    never reach the durable queue (WRONG_EXECUTION_SEMANTICS observed live for
    the second task under the multi-task parallel path).
    """
    tree = _search_analyze_tree()
    observation = _observation(
        capability="SEARCH_COMMUNITY",
        draft_id="",
        tree=tree,
    )
    llm = _LLM([
        {
            "action": "CREATE_TASK",
            "tool_name": "",
            "tool_args": {},
            "reason": "read the top post detail next",
            "confidence": 0.9,
        },
        {"finished": False, "needs_next_step": True, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    adapter = await _make_adapter(llm, observation=observation)
    result = await adapter.continue_run(
        observation=observation,
        conversation_id="c1",
        user_id="u1",
        tenant_id="ten1",
        mcp=None,
        llm=llm,
        model="test-model",
        auth=_auth(),
    )
    plans = adapter._submit_calls["submit_plan"]  # type: ignore[attr-defined]
    assert plans, "AgentLoop must submit the next durable action after the read"
    caps = [step.capability for plan in plans for step in plan.steps]
    assert caps, "submitted plan must carry at least one step"
    assert "ANALYZE_CONTENT_PATTERNS" not in caps, (
        "reasoning capability must never reach the durable queue"
    )
    assert "SEARCH_COMMUNITY" not in caps, (
        "the completed read must not be resubmitted"
    )
    assert result.status not in {"FAILED", "CANCELLED"}


# ── test_continuation_uses_owned_draft ─────────────────────────────────


@pytest.mark.asyncio
async def test_continuation_uses_owned_draft() -> None:
    # G1's observation carries only G1's goal_tree and Draft D1. The resumed
    # AgentLoop must not observe G2's Draft D2.
    g2_tree = GoalTree(
        root=Goal(
            goal_id="g2",
            description="Second post",
            goal_type="CREATE",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            publication_intent="SCHEDULED_PUBLISH",
            temporal_constraint={"run_at": "2026-08-15T10:00:00+08:00"},
        ),
        task_nodes=[
            TaskNode(task_id="g2:1", goal_id="g2", capability="GENERATE_CONTENT"),
            TaskNode(task_id="g2:2", goal_id="g2", capability="SCHEDULE_PUBLISH", dependencies=["g2:1"]),
        ],
    )
    llm = _LLM([
        {"action": "FINISH", "reason": "goal complete", "confidence": 1.0},
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""},
    ])
    adapter = await _make_adapter(llm)
    await adapter.continue_run(
        observation=_observation(draft_id="D1", tree=g2_tree),
        conversation_id="c1",
        user_id="u1",
        tenant_id="ten1",
        mcp=None,
        llm=llm,
        model="test-model",
        auth=_auth(),
    )
    payloads = llm.user_payloads()
    reason_payload = next(
        (item for item in payloads if "observation" in item),
        None,
    )
    assert reason_payload is not None
    serialized = json.dumps(reason_payload, ensure_ascii=False)
    assert "D1" in serialized
    assert "D2" not in serialized
    assert "g2" in serialized
    assert "g1" not in serialized


# ── test_failed_generate_does_not_schedule ─────────────────────────────


@pytest.mark.asyncio
async def test_failed_generate_does_not_schedule() -> None:
    failed_observation = _observation(status="FAILED", draft_id="", error="CREATOR_ERROR")
    llm = _LLM([
        {"action": "FINISH", "reason": "generation failed; ask the user", "confidence": 0.9},
        {"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": "failed"},
    ])
    adapter = await _make_adapter(llm, observation=failed_observation)
    await adapter.continue_run(
        observation=failed_observation,
        conversation_id="c1",
        user_id="u1",
        tenant_id="ten1",
        mcp=None,
        llm=llm,
        model="test-model",
        auth=_auth(),
    )
    assert adapter._submit_calls["submit_plan"] == []  # type: ignore[attr-defined]
    payloads = llm.user_payloads()
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "CREATOR_ERROR" in serialized or "FAILED" in serialized


# ── test_continuation_is_idempotent ────────────────────────────────────


def test_continuation_claim_is_idempotent() -> None:
    store = ActionObservationStore()
    store.save(_observation())
    first = store.claim_pending(batch_size=10)
    assert len(first) == 1
    second = store.claim_pending(batch_size=10)
    assert second == []
    store.mark_done(first[0].observation_id)
    assert store.list_pending() == []


def test_continuation_claim_by_predecessor_is_idempotent() -> None:
    store = ActionObservationStore()
    store.save(_observation(execution_id="predecessor-1"))

    first = store.claim_continuation("predecessor-1")
    second = store.claim_continuation("predecessor-1")

    assert first is not None
    assert first.execution_id == "predecessor-1"
    assert second is None
    store.mark_done(first.observation_id)
    assert store.list_pending() == []


def test_continuation_crash_recovers_dispatched_observation() -> None:
    store = ActionObservationStore()
    store.save(_observation())
    claimed = store.claim_pending(batch_size=1, dispatch_timeout_seconds=600)
    assert len(claimed) == 1
    # Simulate a consumer crash before mark_done: a fresh poll with an
    # expired dispatch timeout must recover the same observation.
    recovered = store.claim_pending(batch_size=1, dispatch_timeout_seconds=0)
    assert len(recovered) == 1
    assert recovered[0].execution_id == "e1"
    assert recovered[0].draft_id == "draft-123"


# ── incremental mode selection (Case A / Case B) ───────────────────────


def _state_with(goal_tree: GoalTree, *, completed: list[str] | None = None) -> Any:
    return SimpleNamespace(
        goal_tree=goal_tree,
        completed_task_ids=list(completed or []),
        resume_context=SimpleNamespace(completed_step_ids=[]),
    )


def _two_step_plan() -> TaskPlan:
    return TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(step_id="g1:1", ordinal=1, capability="GENERATE_CONTENT", goal_id="g1"),
            PlanStep(step_id="g1:2", ordinal=2, capability="SCHEDULE_PUBLISH", goal_id="g1"),
        ],
    )


def test_read_only_single_capability_stays_whole_plan() -> None:
    tree = GoalTree(root=Goal(goal_id="g1", description="query", goal_type="QUERY", required_capabilities=["SEARCH"]))
    plan = _two_step_plan()
    plan.steps = [PlanStep(step_id="s1", ordinal=1, capability="SEARCH")]
    reduced = _incremental_plan(_state_with(tree), plan)
    assert reduced.plan_source == "GOAL_RUNTIME"
    assert reduced is plan


def test_multi_goal_reduces_to_current_goal_single_step() -> None:
    # Phase 2: multi-goal requests are incremental too — the plan is reduced
    # to the deterministic current unsatisfied Goal's next single action.
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two goals",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g1",
                    goal_type="CREATE",
                    publication_intent="SCHEDULED_PUBLISH",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
                Goal(
                    goal_id="g2",
                    goal_type="CREATE",
                    publication_intent="SCHEDULED_PUBLISH",
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
            ],
        )
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(step_id="g1:1", ordinal=1, capability="GENERATE_CONTENT", goal_id="g1"),
            PlanStep(step_id="g1:2", ordinal=2, capability="SCHEDULE_PUBLISH", goal_id="g1"),
            PlanStep(step_id="g2:1", ordinal=3, capability="GENERATE_CONTENT", goal_id="g2"),
            PlanStep(step_id="g2:2", ordinal=4, capability="SCHEDULE_PUBLISH", goal_id="g2"),
        ],
    )
    reduced = _incremental_plan(_state_with(tree), plan)
    assert reduced.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in reduced.steps] == ["GENERATE_CONTENT"]
    assert reduced.steps[0].goal_id == "g1"
    # deterministic action identity (task, goal, action, resource, run_at)
    assert reduced.plan_id == "inc:t1:g1:GENERATE_CONTENT::"


def test_single_goal_two_capabilities_reduces_to_one_step() -> None:
    plan = _incremental_plan(_state_with(_generate_schedule_tree()), _two_step_plan())
    assert plan.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in plan.steps] == ["GENERATE_CONTENT"]
    assert plan.steps[0].depends_on == []


def test_resumed_goal_skips_completed_first_step() -> None:
    plan = _incremental_plan(
        _state_with(_generate_schedule_tree(), completed=["g1:1"]),
        _two_step_plan(),
    )
    assert plan.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in plan.steps] == ["SCHEDULE_PUBLISH"]


# ── incremental SCHEDULE step inherits the dependency DRAFT ──────────────


def test_incremental_schedule_step_inherits_dependency_draft_id() -> None:
    """Real-chain regression: in incremental mode SCHEDULE_PUBLISH is a
    standalone Execution, so the Worker's same-Execution upstream-artifact
    walk cannot see the DRAFT produced by GENERATE_CONTENT.  The continuation
    must carry the durable DRAFT identity from the dependency Goal's terminal
    observation into the schedule step arguments — otherwise the schedule
    tool receives an empty draft_id and fails VALIDATION (observed failure:
    ``未指定需要排期发布的草稿``)."""
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _facts_from_execution_states,
    )

    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="写帖子并在五分钟后发布",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g4",
                    goal_type="CREATE",
                    publication_intent="DRAFT_ONLY",
                    required_capabilities=["GENERATE_CONTENT"],
                ),
                Goal(
                    goal_id="g5",
                    goal_type="CREATE",
                    publication_intent="SCHEDULE_PUBLISH",
                    required_capabilities=["SCHEDULE_PUBLISH"],
                    dependencies=["g4"],
                ),
            ],
        )
    )
    facts = _facts_from_execution_states([
            {
                "execution_id": "exec-g4",
                "task_id": "t1",
                "goal_id": "g4",
            "capability": "GENERATE_CONTENT",
            "status": "COMPLETED",
            "draft_id": "draft-346608026570067968",
        }
    ])
    state = SimpleNamespace(
        goal_tree=tree,
        completed_task_ids=[],
        resume_context=SimpleNamespace(completed_step_ids=[]),
        context_snapshot={"execution_states": [{
            "execution_id": "exec-g4",
            "task_id": "t1",
            "goal_id": "g4",
            "capability": "GENERATE_CONTENT",
            "status": "COMPLETED",
            "draft_id": "draft-346608026570067968",
        }]},
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(
                step_id="g5:1",
                ordinal=1,
                capability="SCHEDULE_PUBLISH",
                goal_id="g5",
                constraints={"run_at": "2026-08-14T03:00:00Z", "timezone": "Asia/Shanghai"},
            ),
        ],
    )
    reduced = _incremental_plan(state, plan)
    assert reduced.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in reduced.steps] == ["SCHEDULE_PUBLISH"]
    assert reduced.steps[0].constraints.get("draft_id") == "draft-346608026570067968"
    assert facts["g4"]["draft_id"] == "draft-346608026570067968"


def test_incremental_schedule_step_keeps_explicit_draft_id() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="g5",
            goal_type="CREATE",
            publication_intent="SCHEDULE_PUBLISH",
            required_capabilities=["SCHEDULE_PUBLISH"],
        )
    )
    state = SimpleNamespace(
        goal_tree=tree,
        completed_task_ids=[],
        resume_context=SimpleNamespace(completed_step_ids=[]),
        context_snapshot={"execution_states": []},
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(
                step_id="g5:1",
                ordinal=1,
                capability="SCHEDULE_PUBLISH",
                goal_id="g5",
                constraints={"draft_id": "draft-explicit", "run_at": "2026-08-14T03:00:00Z"},
            ),
        ],
    )
    reduced = _incremental_plan(state, plan)
    assert reduced.steps[0].constraints.get("draft_id") == "draft-explicit"


def test_incremental_schedule_step_without_draft_fact_stays_unchanged() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="g5",
            goal_type="CREATE",
            publication_intent="SCHEDULE_PUBLISH",
            required_capabilities=["SCHEDULE_PUBLISH"],
        )
    )
    state = SimpleNamespace(
        goal_tree=tree,
        completed_task_ids=[],
        resume_context=SimpleNamespace(completed_step_ids=[]),
        context_snapshot={"execution_states": []},
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(
                step_id="g5:1",
                ordinal=1,
                capability="SCHEDULE_PUBLISH",
                goal_id="g5",
                constraints={"run_at": "2026-08-14T03:00:00Z"},
            ),
        ],
    )
    reduced = _incremental_plan(state, plan)
    assert "draft_id" not in reduced.steps[0].constraints


def test_incremental_get_post_detail_inherits_dependency_post_id() -> None:
    """Real-chain regression: in incremental mode GET_POST_DETAIL is a
    standalone Execution, so the Worker cannot resolve post_id from an
    upstream SEARCH.  The continuation must carry a real post_id from the
    dependency SEARCH Goal's durable POST references — never invent one."""
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _facts_from_execution_states,
    )

    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="总结社区 Agent 帖子的共同方法",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g2",
                    goal_type="QUERY",
                    semantic_operation="SEARCH",
                    required_capabilities=["SEARCH_COMMUNITY"],
                ),
                Goal(
                    goal_id="g3",
                    goal_type="ANALYZE",
                    semantic_operation="ANALYZE",
                    required_capabilities=["GET_POST_DETAIL"],
                    dependencies=["g2"],
                ),
            ],
        )
    )
    facts = _facts_from_execution_states([
            {
                "execution_id": "exec-search",
                "task_id": "t1",
                "goal_id": "g2",
            "capability": "SEARCH_COMMUNITY",
            "status": "COMPLETED",
            "output_artifact_type": "SEARCH_RESULT",
            "post_ids": ["post-19-a", "post-19-b", "post-19-c"],
        }
    ])
    assert facts["g2"]["post_ids"] == ["post-19-a", "post-19-b", "post-19-c"]
    state = SimpleNamespace(
        goal_tree=tree,
        completed_task_ids=[],
        resume_context=SimpleNamespace(completed_step_ids=[]),
        context_snapshot={"execution_states": [{
            "execution_id": "exec-search",
            "task_id": "t1",
            "goal_id": "g2",
            "capability": "SEARCH_COMMUNITY",
            "status": "COMPLETED",
            "output_artifact_type": "SEARCH_RESULT",
            "post_ids": ["post-19-a", "post-19-b", "post-19-c"],
        }]},
    )
    plan = TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(step_id="g3:1", ordinal=1, capability="GET_POST_DETAIL", goal_id="g3"),
        ],
    )
    reduced = _incremental_plan(state, plan)
    assert reduced.plan_source == INCREMENTAL_PLAN_SOURCE
    assert [step.capability for step in reduced.steps] == ["GET_POST_DETAIL"]
    assert reduced.steps[0].constraints.get("post_id") == "post-19-a"


def test_dependency_post_id_skips_already_read_posts() -> None:
    """Repeated incremental continuations must consume distinct posts instead
    of re-reading the same one."""
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _already_read_post_ids,
        _dependency_post_id,
    )
    from greenbook_agent_core.goal.models import Goal, GoalTree

    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="总结共同方法",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g2",
                    goal_type="QUERY",
                    semantic_operation="SEARCH",
                    required_capabilities=["SEARCH_COMMUNITY"],
                ),
                Goal(
                    goal_id="g3",
                    goal_type="ANALYZE",
                    semantic_operation="ANALYZE",
                    required_capabilities=["GET_POST_DETAIL"],
                    dependencies=["g2"],
                ),
            ],
        )
    )
    facts_by_goal = {
        "g2": {
            "draft_id": "",
            "schedule_id": "",
            "post_id": "",
            "post_ids": ["post-1", "post-2", "post-3"],
            "status": "COMPLETED",
            "completed_capabilities": ["SEARCH_COMMUNITY"],
            "artifact_types": ["SEARCH_RESULT"],
        },
        "g3": {
            "draft_id": "",
            "schedule_id": "",
            "post_id": "post-1",
            "post_ids": [],
            "status": "COMPLETED",
            "completed_capabilities": ["GET_POST_DETAIL"],
            "artifact_types": [],
        },
    }
    already_read = _already_read_post_ids(facts_by_goal)
    assert already_read == {"post-1"}
    assert _dependency_post_id(tree, "g3", facts_by_goal, already_read) == "post-2"


def _auth() -> Any:
    from greenbook_contracts.identity import AuthContext

    return AuthContext(
        user_id="u1",
        tenant_id="ten1",
        roles=[],
        timezone="Asia/Shanghai",
        raw_access_token="",
    )


def test_is_valid_uuid_rejects_corrupt_observations() -> None:
    """Continuation must drop corrupt/test-leftover observations whose
    conversation_id is not a real UUID instead of failing the poll forever."""
    from greenbook_agent_api.main import _is_valid_uuid

    assert _is_valid_uuid("d271cfb8-f572-4fa6-aa59-efd4860381c6") is True
    assert _is_valid_uuid("conv-e5-baca7fb7") is False
    assert _is_valid_uuid("") is False
    assert _is_valid_uuid(None) is False


@pytest.mark.asyncio
async def test_reconcile_does_not_overwrite_run_without_queue_messages() -> None:
    """Real-chain regression: an AgentLoop-internal failure never dispatches a
    queued Execution, so this Run has zero owned queue messages.  Read-time
    reconciliation must NOT converge it to COMPLETED (observed: a
    STRUCTURED_OUTPUT_INVALID Run was shown as COMPLETED with no error); the
    runner owns the durable terminal state for such Runs."""
    from greenbook_agent_api.main import _reconcile_agent_run_status

    class _Run:
        status = "RUNNING"
        version = 1

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()

        def get(self, run_id: str) -> Any:
            return self.run

        def mark_status(self, run_id: str, status: str, **_: Any) -> bool:
            self.run.status = status
            return True

    class _EmptyQueue:
        def list(self) -> list[Any]:
            return []

    class _Persistence:
        execution_queue = _EmptyQueue()
        execution_repository = None
        observation_store = None

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
    })()})()

    await _reconcile_agent_run_status(app=app, run_id="run-internal-failure")

    assert store.run.status == "RUNNING", (
        "a Run without any queued Execution must keep its durable state, "
        "not be guessed as COMPLETED"
    )


@pytest.mark.asyncio
async def test_reconcile_waiting_approval_waits_for_durable_approval_identity() -> None:
    """A poll racing approval capture must not persist an incomplete wait."""
    from greenbook_agent_api.main import _reconcile_agent_run_status

    class _Run:
        status = "RUNNING"
        version = 1
        payload = {}

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()
            self.marked = False

        def get(self, run_id: str) -> Any:
            return self.run

        def mark_status(self, run_id: str, status: str, **_: Any) -> bool:
            self.marked = True
            self.run.status = status
            return True

    class _Queue:
        def list(self) -> list[Any]:
            return [type("Message", (), {
                "execution_id": "execution-1",
                "payload": {"run_id": "run-1"},
            })()]

    class _Repository:
        def find_by_id(self, execution_id: str) -> Any:
            return type("Execution", (), {"status": "WAITING_APPROVAL"})()

    class _ApprovalService:
        async def get_for_execution(self, execution_id: str) -> None:
            return None

    class _Persistence:
        execution_queue = _Queue()
        execution_repository = _Repository()
        observation_store = None

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
        "approval_runtime_service": _ApprovalService(),
    })()})()

    await _reconcile_agent_run_status(
        app=app,
        run_id="run-1",
        result=type("Result", (), {
            "status": "WAITING_APPROVAL",
            "approval_id": "approval-not-yet-durable",
        })(),
    )

    assert store.run.status == "RUNNING"
    assert store.marked is False


@pytest.mark.asyncio
async def test_reconcile_does_not_relabel_waiting_execution_as_running() -> None:
    """A pending Objective must not mask a real durable HITL boundary."""
    from greenbook_agent_api.main import _reconcile_agent_run_status

    class _Run:
        status = "RUNNING"
        version = 1
        payload = {"waiting_state": "WAITING_APPROVAL"}
        user_id = "u1"
        tenant_id = "ten1"
        conversation_id = "c1"
        created_at = "2026-08-20T00:00:00+00:00"

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()

        def get(self, _run_id: str) -> Any:
            return self.run

        def mark_status(self, _run_id: str, status: str, **_: Any) -> bool:
            self.run.status = status
            return True

    class _Queue:
        def list(self) -> list[Any]:
            return [type("Message", (), {
                "execution_id": "execution-waiting",
                "payload": {"run_id": "run-waiting", "task_id": "task-waiting"},
            })()]

    class _Repository:
        def find_by_id(self, _execution_id: str) -> Any:
            return type("Execution", (), {"status": "WAITING_APPROVAL"})()

    class _Approval:
        approval_id = "approval-waiting"

    class _ApprovalService:
        async def get_for_execution(self, _execution_id: str) -> Any:
            return _Approval()

    class _Persistence:
        execution_queue = _Queue()
        execution_repository = _Repository()
        observation_store = None

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
        "approval_runtime_service": _ApprovalService(),
    })()})()

    await _reconcile_agent_run_status(
        app=app,
        run_id="run-waiting",
        result=type("Result", (), {"status": "WAITING_APPROVAL"})(),
    )

    assert store.run.status == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_reconcile_waiting_approval_converges_after_execution_completion() -> None:
    """WAITING_APPROVAL is resumable; a completed Execution may close its Run."""
    from greenbook_agent_api.main import _reconcile_agent_run_status

    class _Run:
        status = "WAITING_APPROVAL"
        version = 2
        payload = {"execution_id": "execution-1", "waiting_state": "WAITING_APPROVAL"}
        created_at = "2026-08-20T00:00:00+00:00"

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()
            self.marked: tuple[str, dict[str, Any] | None] | None = None

        def get(self, run_id: str) -> Any:
            return self.run

        def mark_status(self, run_id: str, status: str, *, payload: dict[str, Any] | None = None, **_: Any) -> bool:
            self.marked = (status, payload)
            self.run.status = status
            return True

    class _Queue:
        def list(self) -> list[Any]:
            return [type("Message", (), {
                "execution_id": "execution-1",
                "payload": {"run_id": "run-1"},
            })()]

        def get_by_execution_id(self, execution_id: str) -> Any:
            return self.list()[0]

    class _Repository:
        def find_by_id(self, execution_id: str) -> Any:
            return type("Execution", (), {"status": "COMPLETED"})()

    class _Approval:
        approval_id = "approval-1"

    class _ApprovalService:
        async def get_for_execution(self, execution_id: str) -> Any:
            return _Approval()

    class _Persistence:
        execution_queue = _Queue()
        execution_repository = _Repository()
        observation_store = None

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
        "approval_runtime_service": _ApprovalService(),
    })()})()

    await _reconcile_agent_run_status(
        app=app,
        run_id="run-1",
        result=type("Result", (), {"status": "COMPLETED", "success": True, "approval_id": ""})(),
    )

    assert store.marked is not None
    assert store.marked[0] == "COMPLETED"


@pytest.mark.asyncio
async def test_reconcile_completed_execution_keeps_run_open_until_task_terminal() -> None:
    """A completed Execution cannot close a Run with pending Objectives."""
    from greenbook_agent_api.main import _reconcile_agent_run_status
    from greenbook_agent_core.task.models import Objective, Task, TaskStatus

    class _Run:
        status = "RUNNING"
        version = 1
        # The semantic-confirmation marker is stale once the Task is resumed.
        payload = {"waiting_state": "WAITING_HUMAN"}
        user_id = "u1"
        tenant_id = "ten1"
        conversation_id = "c1"
        created_at = "2026-08-20T00:00:00+00:00"

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()

        def get(self, _run_id: str) -> Any:
            return self.run

        def mark_status(self, _run_id: str, status: str, **_: Any) -> bool:
            self.run.status = status
            return True

    class _Queue:
        def list(self) -> list[Any]:
            return [type("Message", (), {"execution_id": "e1", "payload": {"run_id": "run-1"}})()]

    class _Repository:
        def find_by_id(self, _execution_id: str) -> Any:
            return type("Execution", (), {"status": "COMPLETED"})()

    class _Persistence:
        execution_queue = _Queue()
        execution_repository = _Repository()
        observation_store = None

    task = Task(
        task_id="t1", conversation_id="c1", user_id="u1", tenant_id="ten1",
        status=TaskStatus.RUNNING,
        objectives=[Objective(task_id="t1", objective_id="A", required_capabilities=["GENERATE_CONTENT"])],
    )

    class _TaskProvider:
        async def get_task(self, _scope: Any, _task_id: str) -> Task:
            return task

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
        "task_provider": _TaskProvider(),
    })()})()
    result = type("Result", (), {
        "status": "COMPLETED", "task_id": "t1", "error_code": "", "approval_id": "",
    })()
    await _reconcile_agent_run_status(app=app, run_id="run-1", result=result)
    assert store.run.status == "RUNNING"
    task.status = TaskStatus.COMPLETED
    await _reconcile_agent_run_status(app=app, run_id="run-1", result=result)
    assert store.run.status == "COMPLETED"


@pytest.mark.asyncio
async def test_reconcile_task_bound_run_fails_closed_when_task_read_is_unavailable() -> None:
    """A transient Task read gap must not orphan a valid continuation queue item."""
    from greenbook_agent_api.main import _reconcile_agent_run_status

    class _Run:
        status = "RUNNING"
        version = 1
        # This marker belongs to the predecessor semantic-confirmation
        # pause; it must not mask the current non-terminal Task.
        payload = {"waiting_state": "WAITING_HUMAN"}
        user_id = "u1"
        tenant_id = "ten1"
        conversation_id = "c1"

    class _RunStore:
        def __init__(self) -> None:
            self.run = _Run()

        def get(self, _run_id: str) -> Any:
            return self.run

        def mark_status(self, _run_id: str, status: str, **_: Any) -> bool:
            self.run.status = status
            return True

    class _Queue:
        def list(self) -> list[Any]:
            return [type("Message", (), {
                "execution_id": "e1",
                "payload": {"run_id": "run-1", "task_id": "task-1"},
            })()]

    class _Repository:
        def find_by_id(self, _execution_id: str) -> Any:
            return type("Execution", (), {"status": "COMPLETED"})()

    class _TaskProvider:
        async def get_task(self, _scope: Any, _task_id: str) -> None:
            return None

    class _Persistence:
        execution_queue = _Queue()
        execution_repository = _Repository()
        observation_store = None

    store = _RunStore()
    app = type("App", (), {"state": type("State", (), {
        "agent_run_store": store,
        "runtime_persistence": _Persistence(),
        "task_provider": _TaskProvider(),
    })()})()

    await _reconcile_agent_run_status(
        app=app,
        run_id="run-1",
        result=type("Result", (), {
            "status": "COMPLETED",
            "task_id": "task-1",
            "error_code": "",
        })(),
    )

    assert store.run.status == "RUNNING"


def test_completed_run_is_terminal_latched() -> None:
    from greenbook_agent_api.runner import RUN_TERMINAL

    assert "COMPLETED" in RUN_TERMINAL
    assert "WAITING_APPROVAL" not in RUN_TERMINAL
