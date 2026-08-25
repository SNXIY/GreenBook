"""Phase 2 — three-Goal Observation-Driven continuation over real PostgreSQL.

One original multi-goal request ("准备三篇帖子") completes through durable
incremental actions: 3 GENERATE_CONTENT + 2 SCHEDULE_PUBLISH, each its own
Execution and its own persisted ActionObservation, with AgentLoop resuming
after every observation and goal ownership never crossing.

The decision layer uses a goal-state-aware fake LLM (reads the goal_states
injected from real observations); the durability layer uses real Postgres.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import ConversationRuntimeAdapter
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.execution.action_observation import (
    ActionObservation,
    ActionObservationWriter,
    PostgresActionObservationStore,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning.contracts import TaskPlan

pytestmark = pytest.mark.integration


def _database_url() -> str:
    url = os.getenv(
        "GREENBOOK_AGENT_DATABASE_URL",
        "postgresql+psycopg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
    )
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


@pytest.fixture
def bind() -> Any:
    import sqlalchemy as sa

    url = _database_url()
    try:
        engine = sa.create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
        )
        with engine.connect():
            pass
    except Exception as exc:
        engine = None
        pytest.skip(f"PostgreSQL unavailable ({exc}) — DB-backed test skipped")
    yield engine
    if engine is not None:
        engine.dispose()


@pytest.fixture
def store(bind: Any) -> PostgresActionObservationStore:
    store_instance = PostgresActionObservationStore(bind, create_tables=True)
    with bind.begin() as connection:
        connection.execute(store_instance._table.delete())
    return store_instance


def _three_goal_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="准备三篇帖子",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="G1",
                    description="Redis 高并发优化",
                    goal_type="CREATE",
                    publication_intent="SCHEDULED_PUBLISH",
                    temporal_constraint={"run_at": "T1"},
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
                Goal(
                    goal_id="G2",
                    description="Agent Memory 设计",
                    goal_type="CREATE",
                    publication_intent="SCHEDULED_PUBLISH",
                    temporal_constraint={"run_at": "T2"},
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
                Goal(
                    goal_id="G3",
                    description="Java 并发",
                    goal_type="CREATE",
                    publication_intent="DRAFT_ONLY",
                    required_capabilities=["GENERATE_CONTENT"],
                ),
            ],
        )
    )


class _GoalAwareLLM:
    """Fake LLM that decides the next action from real goal_states.

    It never invents a Goal: it reads the goal_states injected from durable
    observations and picks the first unsatisfied Goal's missing capability.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        self.requests.append(dict(_kwargs))
        system = str(_kwargs["messages"][0]["content"])
        if system.startswith("You are the GreenBook Agent Reflector"):
            return self._respond({"finished": True, "needs_next_step": False, "retry": False, "adjust_plan": False, "reason": ""})
        user = json.loads(_kwargs["messages"][1]["content"])
        # Goal satisfaction travels with the resume state (survives the
        # per-round context refresh); conversation_context is a fallback.
        states = (
            (user.get("observation") or {}).get("resume_context", {}).get("goal_states", [])
            or (user.get("observation") or {}).get("conversation_context", {}).get("goal_states", [])
        )
        target = next((item for item in states if not item["satisfied"]), None)
        if target is None:
            return self._respond({"action": "FINISH", "reason": "all goals satisfied", "confidence": 1.0})
        missing = target.get("missing", [])
        if "draft" in missing:
            return self._respond({
                "action": "CREATE_TASK",
                "tool_name": "",
                "tool_args": {},
                "reason": f"generate draft for {target['goal_id']}",
                "confidence": 0.9,
            })
        if "schedule" in missing:
            return self._respond({
                "action": "CREATE_TASK",
                "tool_name": "",
                "tool_args": {"draft_id": target.get("draft_id", "")},
                "reason": f"schedule {target['goal_id']}",
                "confidence": 0.9,
            })
        return self._respond({"action": "FINISH", "reason": f"goal {target['goal_id']} blocked", "confidence": 0.9})

    @staticmethod
    def _respond(payload: dict[str, Any]) -> Any:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
        )])


def _auth() -> Any:
    from greenbook_contracts.identity import AuthContext

    return AuthContext(user_id="u-e2e", tenant_id="ten-e2e", raw_access_token="")


def _message_from_plan(plan: TaskPlan, observation: ActionObservation, execution_id: str) -> ExecutionQueueMessage:
    return ExecutionQueueMessage(
        execution_id=execution_id,
        trace_id=f"trace-{execution_id}",
        payload={
            "conversation_id": observation.conversation_id,
            "task_id": observation.task_id,
            "run_id": f"run-{execution_id}",
            "session": observation.payload.get("session") or {},
            "execution_input": {
                "goal_id": plan.steps[0].goal_id,
                "plan_id": plan.plan_id,
                "execution_metadata": {
                    "plan_mode": "INCREMENTAL",
                    "goal_tree": observation.payload.get("goal_tree") or {},
                    "command": observation.payload.get("command") or {},
                },
                "steps": [
                    {
                        "step_id": step.step_id,
                        "goal_id": step.goal_id,
                        "capability": step.capability,
                        "constraints": dict(step.constraints or {}),
                    }
                    for step in plan.steps
                ],
            },
        },
    )


def _result_for(capability: str, goal_id: str, execution_id: str) -> RuntimeResult:
    if capability == "GENERATE_CONTENT":
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=f"run-{execution_id}",
            task_id=f"task-{execution_id}",
            execution_id=execution_id,
            summary=f"Draft for {goal_id}",
            artifacts=[{
                "artifact_id": f"art-{execution_id}",
                "artifact_type": "DRAFT",
                "type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": f"D-{goal_id}",
                "title": f"draft {goal_id}",
                "status": "DRAFT",
            }],
        )
    if capability == "SCHEDULE_PUBLISH":
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=f"run-{execution_id}",
            task_id=f"task-{execution_id}",
            execution_id=execution_id,
            summary=f"Schedule for {goal_id}",
            artifacts=[{
                "artifact_id": f"art-{execution_id}",
                "artifact_type": "SCHEDULE",
                "type": "SCHEDULE",
                "resource_type": "SCHEDULE",
                "resource_id": f"S-{goal_id}",
                "status": "SCHEDULED",
            }],
        )
    return RuntimeResult(
        success=False,
        status="FAILED",
        run_id=f"run-{execution_id}",
        execution_id=execution_id,
        error_message=f"unknown capability {capability}",
    )


def test_three_goal_incremental_trace_over_postgres(bind: Any, store: Any) -> None:
    llm = _GoalAwareLLM()
    submissions: list[TaskPlan] = []
    observations_in_store: list[ActionObservation] = []
    writer = ActionObservationWriter(store=store)
    tree = _three_goal_tree()
    seed = ActionObservation(
        execution_id=f"seed-{uuid.uuid4().hex[:8]}",
        task_id="task-3goal",
        conversation_id="conv-3goal",
        goal_id="",
        capability="",
        status="COMPLETED",
        payload={
            "goal_tree": tree.model_dump(mode="json"),
            "command": {
                "type": "CREATE",
                "goal": "准备三篇帖子",
                "objective": "准备三篇帖子",
                "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            },
            "session": {
                "conversation_id": "conv-3goal",
                "user_id": "u-e2e",
                "tenant_id": "ten-e2e",
                "timezone": "Asia/Shanghai",
            },
        },
    )

    class _SubmitService:
        container = SimpleNamespace(artifact_registry=None, capability_registry=None, tool_registry=None)
        _memory_mgr = None
        _artifact_store = None
        _execution_repository = None

        async def submit_plan(self, context, plan, *, completion_callback=None, **_extra):
            submissions.append(plan)
            return {
                "execution_id": f"e-{len(submissions)}",
                "status": "QUEUED",
                "queued": True,
                "ok": True,
                "message": "Execution accepted by the durable queue.",
                "plan_id": getattr(plan, "plan_id", ""),
            }

    class _ContextBuilder:
        def __init__(self) -> None:
            self._facts: dict[str, dict[str, str]] = {}

        def ingest(self, observation: ActionObservation) -> None:
            entry = self._facts.setdefault(
                observation.goal_id,
                {"draft_id": "", "schedule_id": "", "status": "", "capability": "", "task_id": ""},
            )
            entry["task_id"] = observation.task_id
            if observation.draft_id:
                entry["draft_id"] = observation.draft_id
            if observation.schedule_id:
                entry["schedule_id"] = observation.schedule_id
            entry["status"] = observation.status
            entry["capability"] = observation.capability

        async def build(self, **kwargs):
            artifacts = [
                {
                    "task_id": facts.get("task_id", ""),
                    "resource_type": "DRAFT",
                    "resource_id": facts["draft_id"],
                }
                for facts in self._facts.values()
                if facts.get("draft_id")
            ]
            execution_states = [
                {
                    "task_id": facts.get("task_id", ""),
                    "goal_id": goal_id,
                    "capability": facts.get("capability", ""),
                    "status": facts.get("status", ""),
                    "draft_id": facts.get("draft_id", ""),
                    "schedule_id": facts.get("schedule_id", ""),
                }
                for goal_id, facts in self._facts.items()
            ]
            ctx = SimpleNamespace(
                snapshot_id="s1",
                conversation_id="conv-3goal",
                user_id="u-e2e",
                tenant_id="ten-e2e",
                active_draft_id="",
                active_schedule_id="",
                artifacts=artifacts,
                execution_states=execution_states,
                goal_states=[],
            )

            def decision_payload() -> dict[str, Any]:
                return {
                    "artifacts": list(ctx.artifacts),
                    "execution_states": list(ctx.execution_states),
                    "goal_states": list(ctx.goal_states),
                    "conversation_id": ctx.conversation_id,
                    "user_id": ctx.user_id,
                    "tenant_id": ctx.tenant_id,
                }

            ctx.decision_payload = decision_payload
            return ctx

    class _ConversationService:
        async def get_conversation(self, *_args, **_kwargs):
            return SimpleNamespace(conversation_id="conv-3goal")

    builder = _ContextBuilder()
    adapter = ConversationRuntimeAdapter(
        command_runtime=None,
        goal_decomposer=None,
        agent_loop=AgentLoop(llm=llm, model="test-model"),
        goal_compiler=GoalCompiler(),
        task_manager=SimpleNamespace(
            create_task=_async_task,
            get_task=_async_task,
            bind_execution=_async_task,
            bind_goal_tree=_async_task,
            record_replan=_async_task,
        ),
        task_provider=SimpleNamespace(persist_completion_projection=_async_task),
        runtime_service=_SubmitService(),
        conversation_service=_ConversationService(),
        context_builder=builder,
    )

    async def execute_queued(message, **_kwargs):
        execution_input = message.payload["execution_input"]
        steps = execution_input.get("steps") or []
        capability = str((steps[0] or {}).get("capability") or "")
        goal_id = str(execution_input.get("goal_id") or "")
        return _result_for(capability, goal_id, message.execution_id)

    def resolve(_message):
        return _auth()

    handler = RuntimeExecutionQueueHandler(
        service=SimpleNamespace(execute_queued=execute_queued),
        mcp=None,
        credential_resolver=resolve,
        completion_publisher=None,
        observation_writer=writer,
    )

    def run_async(coro):
        return asyncio.run(coro)

    current_observation = seed
    processed_submissions = 0
    for _round in range(10):
        # Resume AgentLoop with the latest observation; the fake LLM picks the
        # next action from goal_states injected from real observations.
        run_async(adapter.continue_run(
            observation=current_observation,
            conversation_id="conv-3goal",
            user_id="u-e2e",
            tenant_id="ten-e2e",
            mcp=None,
            llm=llm,
            model="test-model",
            auth=_auth(),
        ))
        new_submissions = submissions[processed_submissions:]
        if len(new_submissions) == 0:
            break  # AgentLoop FINISHed: no new durable action this round.
        processed_submissions = len(submissions)
        # Durable completions are intentionally consumed out of submission
        # order.  The next continuation sees all persisted sibling facts.
        for plan in reversed(new_submissions):
            execution_id = f"E{len(observations_in_store) + 1}"
            message = _message_from_plan(plan, current_observation, execution_id)
            run_async(handler(message))
            terminal = store.get_by_execution(execution_id)
            assert terminal is not None, f"observation missing for {execution_id}"
            observations_in_store.append(terminal)
            builder.ingest(terminal)
            current_observation = terminal

    # ── assertions ──
    actions = [(obs.goal_id, obs.capability) for obs in observations_in_store]
    assert sorted(actions) == sorted([
        ("G1", "GENERATE_CONTENT"),
        ("G1", "SCHEDULE_PUBLISH"),
        ("G2", "GENERATE_CONTENT"),
        ("G2", "SCHEDULE_PUBLISH"),
        ("G3", "GENERATE_CONTENT"),
    ]), actions
    assert sorted(
        (obs.goal_id, obs.draft_id)
        for obs in observations_in_store
        if obs.capability == "GENERATE_CONTENT"
    ) == [("G1", "D-G1"), ("G2", "D-G2"), ("G3", "D-G3")]
    assert sorted(
        (obs.goal_id, obs.schedule_id)
        for obs in observations_in_store
        if obs.capability == "SCHEDULE_PUBLISH"
    ) == [("G1", "S-G1"), ("G2", "S-G2")]
    # G3 (draft-only) never scheduled or published.
    assert not any(
        obs.goal_id == "G3" and obs.capability in {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}
        for obs in observations_in_store
    )
    # Every execution's plan_id is the deterministic action identity
    # (task:goal:action:owned-resource:run-at).
    plan_ids = {plan.plan_id for plan in submissions}
    assert len(plan_ids) == 5
    assert "inc:task-3goal:G1:GENERATE_CONTENT::T1" in plan_ids
    assert "inc:task-3goal:G1:SCHEDULE_PUBLISH:D-G1:T1" in plan_ids
    assert "inc:task-3goal:G3:GENERATE_CONTENT::" in plan_ids
    assert "inc:task-3goal:G2:SCHEDULE_PUBLISH:D-G2:T2" in plan_ids
    # All goals satisfied at the end.
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _facts_from_execution_states,
    )
    from greenbook_agent_core.goal.satisfaction import goal_states

    facts = _facts_from_execution_states([
        {
            "goal_id": obs.goal_id,
            "draft_id": obs.draft_id,
            "schedule_id": obs.schedule_id,
            "status": obs.status,
        }
        for obs in observations_in_store
    ])
    states = goal_states(tree, facts)
    assert all(item["satisfied"] for item in states)


async def _async_task(*_args: Any, **_kwargs: Any) -> Any:
    return SimpleNamespace(task_id="task-3goal", plan_version=1, goal_tree=None, goal_tree_snapshot=None)
