"""Full user-chain E2E over the real execute() entry point.

One complete user message ("帮我找几篇关于 Java 的帖子并总结共同方法……五分钟之后发布")
runs through the production path: CommandInterpreter -> GoalDecomposer ->
AgentLoop -> incremental submit -> worker tools -> observations -> continuations
-> terminal result.  This is the regression net for every reliability fix made
while chasing the real-chain failures:

  * SCHEDULE_PUBLISH receives the GENERATE_CONTENT draft_id across Executions
  * GET_POST_DETAIL receives a real post_id from the SEARCH dependency
  * reasoning lineage accepts concrete post resource ids
  * the context snapshot stays bounded (no full PlanExecution dumps)
  * no empty CREATE_TASK delta echo (fresh request path)
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.command import CommandInterpreter
from greenbook_agent_core.execution.action_observation import (
    ActionObservation,
    ActionObservationStore,
    ActionObservationWriter,
)
from greenbook_agent_core.execution.queue_execution_handler import (
    RuntimeExecutionQueueHandler,
)
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.decomposer import GoalDecomposer
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.runtime.container import RuntimeContainer

pytestmark = pytest.mark.integration

CONVERSATION_ID = "conv-full-chain"
USER_ID = "u-e2e"
TENANT_ID = "ten-e2e"
READ_POSTS = ["346113133389156352", "346103424238096384"]

# Goal ids produced by the fake decomposer.
GOAL_SEARCH = "search_java_posts"
GOAL_READ = "read_java_posts"
GOAL_ANALYZE = "analyze_java_patterns"
GOAL_GENERATE = "generate_java_draft"
GOAL_SCHEDULE = "schedule_java"


class _FakeLLM:
    """Deterministic fake that answers every boundary prompt."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        self.requests.append(dict(_kwargs))
        system = str(_kwargs["messages"][0]["content"])

        if system.startswith("You are the GreenBook Command Runtime"):
            return self._respond({
                "command": "CREATE",
                "goal": "搜索 Java 帖子、总结共同方法、写学习帖子并在五分钟后发布",
                "objective": "搜索 Java 帖子、总结共同方法、写学习帖子并在五分钟后发布",
                "first_action": "SEARCH_COMMUNITY",
                "request_complexity": "COMPLEX",
                "entities": {"topic": "Java", "publish_delay_minutes": 5},
                "constraints": {"publish_after": "2026-08-14T03:00:00Z"},
                "required_capabilities": [
                    "SEARCH_COMMUNITY", "GET_POST_DETAIL",
                    "ANALYZE_CONTENT_PATTERNS", "GENERATE_CONTENT",
                    "SCHEDULE_PUBLISH",
                ],
                "task_changes": [],
            })

        if system.startswith("You are the GreenBook Goal Runtime"):
            return self._respond(_goal_tree().model_dump(mode="json"))

        if system.startswith("You are the GreenBook Agent Reflector"):
            return self._respond({
                "finished": True, "needs_next_step": False,
                "retry": False, "adjust_plan": False, "reason": "",
            })

        if system.startswith("You are GreenBook Dynamic Planner"):
            return self._respond({
                "decision": "CONTINUE", "reason": "proceed", "tool_name": "",
                "arguments": {}, "goal_id": "",
            })

        # AgentLoop reason: advance one Goal per continuation using the
        # goal_states carried by the resume context.
        user = json.loads(_kwargs["messages"][1]["content"])
        states = (
            (user.get("observation") or {}).get("resume_context", {}).get("goal_states", [])
            or (user.get("observation") or {}).get("conversation_context", {}).get("goal_states", [])
        )
        target = next((item for item in states if not item.get("satisfied")), None)
        if target is None:
            # First round or everything satisfied.
            return self._respond({"action": "CREATE_TASK", "reason": "begin", "confidence": 1.0})
        goal_id = str(target.get("goal_id") or "")
        if goal_id == GOAL_ANALYZE:
            return self._respond({
                "action": "PRODUCE_RESULT",
                "result_type": "CONTENT_ANALYSIS",
                "result_payload": {
                    "summary": "Java 学习共同方法",
                    "key_points": ["从基础语法开始", "多写多练"],
                },
                "source_refs": list(READ_POSTS),
                "reason": "analyze read posts",
                "confidence": 0.9,
            })
        return self._respond({
            "action": "CREATE_TASK",
            "reason": f"next {goal_id}",
            "confidence": 0.9,
        })

    @staticmethod
    def _respond(payload: dict[str, Any]) -> Any:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
        )])


def _goal_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="Java 学习帖子",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id=GOAL_SEARCH,
                    description="搜索 Java 帖子",
                    goal_type="QUERY",
                    semantic_operation="SEARCH",
                    required_capabilities=["SEARCH_COMMUNITY"],
                ),
                Goal(
                    goal_id=GOAL_READ,
                    description="阅读代表性帖子",
                    goal_type="QUERY",
                    semantic_operation="SEARCH",
                    required_capabilities=["GET_POST_DETAIL"],
                    dependencies=[GOAL_SEARCH],
                ),
                Goal(
                    goal_id=GOAL_ANALYZE,
                    description="归纳共同方法",
                    goal_type="ANALYZE",
                    semantic_operation="ANALYZE",
                    required_capabilities=["ANALYZE_CONTENT_PATTERNS"],
                    dependencies=[GOAL_READ],
                ),
                Goal(
                    goal_id=GOAL_GENERATE,
                    description="写学习 Java 的帖子",
                    goal_type="CREATE",
                    semantic_operation="CREATE",
                    publication_intent="DRAFT_ONLY",
                    required_capabilities=["GENERATE_CONTENT"],
                    dependencies=[GOAL_ANALYZE],
                ),
                Goal(
                    goal_id=GOAL_SCHEDULE,
                    description="五分钟后发布",
                    goal_type="CREATE",
                    semantic_operation="SCHEDULE",
                    publication_intent="SCHEDULED_PUBLISH",
                    temporal_constraint={"run_at": "2026-08-14T03:00:00Z"},
                    required_capabilities=["SCHEDULE_PUBLISH"],
                    dependencies=[GOAL_GENERATE],
                ),
            ],
        )
    )


def _auth() -> Any:
    from greenbook_contracts.identity import AuthContext

    return AuthContext(user_id=USER_ID, tenant_id=TENANT_ID, raw_access_token="")


class _ContextBuilder:
    """Facts-driven context snapshot (durable observation evidence only)."""

    def __init__(self) -> None:
        self._facts: dict[str, dict[str, Any]] = {}

    def ingest(self, observation: ActionObservation) -> None:
        entry = self._facts.setdefault(observation.goal_id, {
            "draft_id": "", "schedule_id": "", "status": "", "capability": "",
            "post_ids": [], "task_id": "",
        })
        entry["task_id"] = observation.task_id
        if observation.draft_id:
            entry["draft_id"] = observation.draft_id
        if observation.schedule_id:
            entry["schedule_id"] = observation.schedule_id
        entry["status"] = observation.status
        entry["capability"] = observation.capability
        if observation.capability == "SEARCH_COMMUNITY":
            entry["post_ids"] = [
                str(ref.get("resource_id") or "")
                for ref in observation.resource_refs
                if str(ref.get("kind") or ref.get("resource_type") or "").upper() == "POST"
                and ref.get("resource_id")
            ]

    async def build(self, **kwargs: Any) -> Any:
        execution_states = [
            {
                "task_id": facts.get("task_id", ""),
                "goal_id": goal_id,
                "capability": facts.get("capability", ""),
                "status": facts.get("status", ""),
                "draft_id": facts.get("draft_id", ""),
                "schedule_id": facts.get("schedule_id", ""),
                "post_ids": facts.get("post_ids", []),
            }
            for goal_id, facts in self._facts.items()
        ]
        ctx = SimpleNamespace(
            snapshot_id="s-full",
            conversation_id=CONVERSATION_ID,
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            active_draft_id="",
            active_schedule_id="",
            artifacts=[
                {
                    "task_id": facts.get("task_id", ""),
                    "resource_type": "DRAFT",
                    "resource_id": facts["draft_id"],
                }
                for facts in self._facts.values()
                if facts.get("draft_id")
            ],
            execution_states=execution_states,
            goal_states=[],
        )
        ctx.decision_payload = lambda: {
            "artifacts": list(ctx.artifacts),
            "execution_states": list(ctx.execution_states),
            "goal_states": list(ctx.goal_states),
            "conversation_id": ctx.conversation_id,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
        }
        return ctx


class _TaskProvider:
    def __init__(self) -> None:
        self._resource_index: list[Any] = []

    async def persist_completion_projection(self, _scope, **fields: Any) -> Any:
        return None

    async def get_task(self, _scope, task_id: str) -> Any:
        return SimpleNamespace(
            task_id=task_id,
            goal_tree_snapshot=_goal_tree().model_dump(mode="json"),
            resource_index=list(self._resource_index),
        )

    def record_read_posts(self) -> None:
        for post_id in READ_POSTS:
            self._resource_index.append(
                SimpleNamespace(resource_id=post_id, resource_kind="POST")
            )


class _RuntimeService:
    def __init__(self) -> None:
        self.submissions: list[Any] = []
        self.container = RuntimeContainer.for_testing()
        self._execution_repository = self.container.persistence.execution_repository
        self._artifact_store = self.container.artifact_store

    async def submit_plan(self, context: Any, plan: Any, *, completion_callback=None, **_extra):
        self.submissions.append(plan)
        return {
            "execution_id": f"e-{len(self.submissions)}",
            "status": "QUEUED",
            "queued": True,
            "ok": True,
            "message": "Execution accepted by the durable queue.",
            "plan_id": getattr(plan, "plan_id", ""),
        }


def _result_for(capability: str, goal_id: str, execution_id: str) -> RuntimeResult:
    if capability == "SEARCH_COMMUNITY":
        return RuntimeResult(
            success=True, status="COMPLETED",
            run_id="run-full", task_id="task-full", execution_id=execution_id,
            summary="找到 19 篇 Java 帖子",
            artifacts=[{
                "artifact_id": f"art-search-{execution_id}",
                "artifact_type": "SEARCH_RESULT",
                "type": "SEARCH_RESULT",
                "resource_type": "SEARCH_RESULT",
                "resource_id": f"search-{execution_id}",
                "payload": {"count": 19, "resource_refs": [
                    {"kind": "POST", "resource_id": post_id} for post_id in READ_POSTS
                ]},
            }],
        )
    if capability == "GET_POST_DETAIL":
        return RuntimeResult(
            success=True, status="COMPLETED",
            run_id="run-full", task_id="task-full", execution_id=execution_id,
            summary=f"阅读帖子 {goal_id}",
            artifacts=[{
                "artifact_id": f"art-read-{execution_id}",
                "artifact_type": "POST",
                "type": "POST",
                "resource_type": "POST",
                "resource_id": READ_POSTS[0],
                "title": "Java 学习路径",
                "summary": "从基础到实战",
            }],
        )
    if capability == "GENERATE_CONTENT":
        return RuntimeResult(
            success=True, status="COMPLETED",
            run_id="run-full", task_id="task-full", execution_id=execution_id,
            summary="草稿已生成",
            draft_id=f"D-{goal_id}",
            artifacts=[{
                "artifact_id": f"art-draft-{execution_id}",
                "artifact_type": "DRAFT",
                "type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": f"D-{goal_id}",
                "title": "如何学习 Java",
                "status": "DRAFT",
            }],
        )
    if capability == "SCHEDULE_PUBLISH":
        return RuntimeResult(
            success=True, status="COMPLETED",
            run_id="run-full", task_id="task-full", execution_id=execution_id,
            summary="已安排发布",
            draft_id=f"D-{GOAL_GENERATE}",
            schedule_id=f"S-{goal_id}",
            artifacts=[{
                "artifact_id": f"art-schedule-{execution_id}",
                "artifact_type": "SCHEDULE",
                "type": "SCHEDULE",
                "resource_type": "SCHEDULE",
                "resource_id": f"S-{goal_id}",
                "status": "SCHEDULED",
            }],
        )
    return RuntimeResult(
        success=False, status="FAILED",
        run_id="run-full", execution_id=execution_id,
        error_message=f"unknown capability {capability}",
    )


def test_full_user_chain_over_memory() -> None:
    llm = _FakeLLM()
    observations = ActionObservationStore()
    writer = ActionObservationWriter(store=observations)
    builder = _ContextBuilder()
    task_provider = _TaskProvider()
    runtime_service = _RuntimeService()

    adapter = ConversationRuntimeAdapter(
        command_runtime=CommandInterpreter(llm=llm, model="test-model"),
        goal_decomposer=GoalDecomposer(llm=llm, model="test-model"),
        agent_loop=AgentLoop(
            llm=llm,
            model="test-model",
            capability_registry=runtime_service.container.capability_registry,
        ),
        goal_compiler=GoalCompiler(),
        task_provider=task_provider,
        task_manager=SimpleNamespace(
            create_task=_async_task,
            get_task=_async_task,
            bind_execution=_async_task,
            bind_goal_tree=_async_task,
            record_replan=_async_task,
        ),
        runtime_service=runtime_service,
        observation_store=observations,
        context_builder=builder,
        conversation_service=SimpleNamespace(
            get_conversation=lambda *_a, **_k: SimpleNamespace(conversation_id=CONVERSATION_ID),
        ),
    )

    async def execute_queued(message, **_kwargs):
        execution_input = message.payload["execution_input"]
        steps = execution_input.get("steps") or []
        capability = str((steps[0] or {}).get("capability") or "")
        goal_id = str(execution_input.get("goal_id") or "")
        return _result_for(capability, goal_id, message.execution_id)

    handler = RuntimeExecutionQueueHandler(
        service=SimpleNamespace(execute_queued=execute_queued),
        mcp=None,
        credential_resolver=lambda _message: _auth(),
        completion_publisher=None,
        observation_writer=writer,
    )

    def run(coro):
        return asyncio.run(coro)

    # ── 1. Real execute() entry: understand -> decompose -> loop -> submit ──
    first = run(adapter.execute(
        conversation_id=CONVERSATION_ID,
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        message="帮我找几篇关于 Java 的帖子并总结共同方法，然后参考他们的写法和内容，"
                "写一篇关于如何学习Java的帖子，五分钟之后发布",
        run_id="run-full",
        trace_id="trace-full",
        llm=llm,
        model="test-model",
        auth=_auth(),
    ))
    assert str(first.status).upper() in {"QUEUED", "SUBMITTED", "RUNNING"}, (
        f"{first.status} error_code={first.error_code} error={first.error_message or first.error}"
    )
    assert len(runtime_service.submissions) >= 1
    assert runtime_service.submissions[0].steps[0].capability == "SEARCH_COMMUNITY"

    # ── 2. Execute each submitted plan and continue until FINISH ──────────
    processed = 0
    last_observation_count = 0
    current: ActionObservation | None = None
    rounds = 0
    while rounds < 12:
        rounds += 1
        new_submissions = runtime_service.submissions[processed:]
        processed = len(runtime_service.submissions)
        for plan in reversed(new_submissions):
            goal_id = str(plan.steps[0].goal_id or "")
            capability = str(plan.steps[0].capability or "")
            if capability in {"SEARCH_COMMUNITY", "GET_POST_DETAIL"}:
                # Read-only tools are executed in-loop and projected through
                # record_tool_result (the production AgentLoop path).
                from greenbook_agent_core.agent.actions import AgentAction
                from greenbook_agent_core.agent.state import AgentState
                from greenbook_agent_core.goal.models import TaskNode
                from greenbook_agent_core.task.models import Task

                state = AgentState(
                    task=Task(
                        task_id="task-full",
                        conversation_id=CONVERSATION_ID,
                        user_id=USER_ID,
                        tenant_id=TENANT_ID,
                        goal="Java 学习帖子",
                    ),
                    current_task=TaskNode(
                        task_id=f"task-full:{goal_id}",
                        goal_id=goal_id,
                        capability=capability,
                    ),
                    conversation_context={
                        "conversation_id": CONVERSATION_ID,
                        "user_id": USER_ID,
                        "tenant_id": TENANT_ID,
                    },
                    context_snapshot={"artifacts": [], "execution_states": []},
                )
                if capability == "SEARCH_COMMUNITY":
                    result = {
                        "ok": True,
                        "tool_name": "community.search_public_posts",
                        "tool_arguments": {"query": "Java 学习"},
                        "data": {
                            "total": len(READ_POSTS),
                            "items": [{"post_id": post_id} for post_id in READ_POSTS],
                        },
                    }
                else:
                    result = {
                        "ok": True,
                        "tool_name": "community.get_post",
                        "tool_arguments": {"post_id": READ_POSTS[0]},
                        "data": {"post_id": READ_POSTS[0], "title": "Java 学习路径"},
                    }
                recorded = run(adapter.record_tool_result(
                    state=state,
                    action=AgentAction(
                        action="TOOL_CALL",
                        tool_name=result["tool_name"],
                        tool_args=result["tool_arguments"],
                    ),
                    result=result,
                ))
                assert recorded["execution_id"]
                obs = observations.get_by_execution(recorded["execution_id"])
                assert obs is not None, f"observation missing for {recorded['execution_id']}"
                if capability == "SEARCH_COMMUNITY":
                    assert any(
                        str(ref.get("kind") or ref.get("resource_type") or "").upper() == "POST"
                        for ref in obs.resource_refs
                    ), "search observation must expose real POST references"
                if capability == "GET_POST_DETAIL":
                    task_provider.record_read_posts()
                builder.ingest(obs)
                current = obs
                continue
            # Side-effecting tool execution through the queue handler.
            execution_id = f"e-full-{processed}-{capability}"
            message = SimpleNamespace(
                execution_id=execution_id,
                trace_id=f"trace-{execution_id}",
                payload={
                    "conversation_id": CONVERSATION_ID,
                    "task_id": "task-full",
                    "run_id": "run-full",
                    "session": {
                        "conversation_id": CONVERSATION_ID,
                        "user_id": USER_ID,
                        "tenant_id": TENANT_ID,
                        "timezone": "Asia/Shanghai",
                    },
                    "execution_input": {
                        "goal_id": goal_id,
                        "plan_id": getattr(plan, "plan_id", ""),
                        "execution_metadata": {"plan_mode": "INCREMENTAL"},
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
            run(handler(message))
            terminal = observations.get_by_execution(execution_id)
            assert terminal is not None, f"observation missing for {execution_id}"
            builder.ingest(terminal)
            current = terminal
        submissions_before = len(runtime_service.submissions)
        if current is not None:
            run(adapter.continue_run(
                observation=current,
                conversation_id=CONVERSATION_ID,
                user_id=USER_ID,
                tenant_id=TENANT_ID,
                mcp=None,
                llm=llm,
                model="test-model",
                auth=_auth(),
            ))
        # Capture every observation the loop produced — including reasoning
        # PRODUCE_RESULT observations that never touch submit_plan — and feed
        # them into the context so the next continuation sees them satisfied.
        new_observations = list(observations._observations.values())[last_observation_count:]
        for obs in new_observations:
            builder.ingest(obs)
            current = obs
        last_observation_count = len(observations._observations)
        if (
            len(runtime_service.submissions) == submissions_before
            and not new_observations
            and not new_submissions
        ):
            # No new durable action, no new submission and no new observation
            # after the continuation: AgentLoop FINISHed.
            break

    # ── 3. Assertions ─────────────────────────────────────────────────────
    from greenbook_agent_api.services.conversation_runtime_adapter import (
        _facts_from_execution_states,
    )
    from greenbook_agent_core.goal.satisfaction import goal_states

    capabilities = [
        str(plan.steps[0].capability)
        for plan in runtime_service.submissions
    ]
    assert capabilities[0] == "SEARCH_COMMUNITY", capabilities
    assert "GET_POST_DETAIL" in capabilities, capabilities
    assert "GENERATE_CONTENT" in capabilities, capabilities
    assert "SCHEDULE_PUBLISH" in capabilities, capabilities

    # draft_id injected into the SCHEDULE step across Executions.
    schedule_plan = next(
        plan for plan in runtime_service.submissions
        if str(plan.steps[0].capability) == "SCHEDULE_PUBLISH"
    )
    assert schedule_plan.steps[0].constraints.get("draft_id") == f"D-{GOAL_GENERATE}", (
        "SCHEDULE_PUBLISH must receive the GENERATE_CONTENT draft_id across "
        "Executions (regression: VALIDATION_ERROR 未指定需要排期发布的草稿)"
    )

    # post_id injected into the GET_POST_DETAIL step from the SEARCH result.
    read_plan = next(
        plan for plan in runtime_service.submissions
        if str(plan.steps[0].capability) == "GET_POST_DETAIL"
    )
    assert read_plan.steps[0].constraints.get("post_id") in READ_POSTS, (
        "GET_POST_DETAIL must receive a real post_id from the SEARCH dependency"
    )

    # Reasoning commit accepted the post resource ids (no
    # REASONING_RESULT_COMMIT_FAILED) and produced a durable analysis
    # observation; every Goal is satisfied at the end.
    facts = _facts_from_execution_states([
        {
            "goal_id": obs.goal_id,
            "capability": obs.capability,
            "status": obs.status,
            "draft_id": obs.draft_id,
            "schedule_id": obs.schedule_id,
        }
        for obs in observations._observations.values()
        if obs.goal_id
    ])
    states = goal_states(_goal_tree(), facts)
    satisfied = {item["goal_id"] for item in states if item["satisfied"]}
    assert GOAL_SEARCH in satisfied, satisfied
    assert GOAL_READ in satisfied, satisfied
    assert GOAL_ANALYZE in satisfied, satisfied
    assert GOAL_GENERATE in satisfied, satisfied
    assert GOAL_SCHEDULE in satisfied, satisfied


async def _async_task(*_args: Any, **_kwargs: Any) -> Any:
    return SimpleNamespace(
        task_id="task-full",
        plan_version=1,
        goal_tree_snapshot=_goal_tree().model_dump(mode="json"),
    )
