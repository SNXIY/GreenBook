"""Phase 7 semantic understanding, selection, planning, and evaluation tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.agent.loop import AgentLoop, AgentLoopError
from greenbook_agent_core.agent.selector import ToolSelector
from greenbook_agent_core.agent.state import AgentState, Observation
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command import (
    Command,
    CommandContext,
    CommandInterpreter,
    CommandTarget,
    CommandType,
    TargetKind,
    TargetReferenceType,
)
from greenbook_agent_core.conversation.service import ConversationService
from greenbook_agent_core.goal import GoalCompiler, GoalDecomposer, GoalDecompositionError
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning import DynamicPlanner, PlanningDecisionType
from greenbook_contracts.tool_contract import (
    SideEffectMetadata,
    ToolMetadata,
    ToolPolicyMetadata,
)
from greenbook_evaluation.metrics import EvaluationMetricsCalculator
from greenbook_evaluation.models import EvalResult


class _LLM:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)),
            )],
        )


def _metadata(
    name: str,
    capability: str,
    *,
    policy: ToolPolicyMetadata | None = None,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description=f"Tool for {capability}",
        capabilities=(capability,),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        policy=policy or ToolPolicyMetadata(),
    )


@pytest.mark.asyncio
async def test_command_understanding_preserves_continuation_semantics() -> None:
    llm = _LLM({
        "command": "MODIFY",
        "goal": "将现有文章改成面试方向并安排晚上发布",
        "target": {
            "kind": "TASK",
            "reference_type": "ACTIVE",
        },
        "entities": {"style": "interview", "time": "晚上"},
        "constraints": {"publication_window": "晚上"},
        "references": [{"kind": "TASK", "reference": "现有文章任务"}],
        "ambiguity": "",
        "needs_clarification": False,
        "required_capabilities": ["IMPROVE_CONTENT", "SCHEDULE_PUBLISH"],
        "confidence": 0.94,
    })
    context = CommandContext(
        active_target=CommandTarget(
            kind=TargetKind.TASK,
            task_id="task-article",
            reference_type=TargetReferenceType.ACTIVE,
        ),
        active_tasks=[{"task_id": "task-article", "status": "RUNNING"}],
        history=[
            {"role": "user", "content": "帮我写一篇 Agent 文章"},
            {"role": "assistant", "content": "文章任务已经建立"},
        ],
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "改成面试方向，晚上发布",
        context,
    )

    assert command.type == CommandType.MODIFY
    assert command.requested_goal.endswith("晚上发布")
    assert command.entities["style"] == "interview"
    assert command.constraints["publication_window"] == "晚上"
    assert command.required_capabilities == ["IMPROVE_CONTENT", "SCHEDULE_PUBLISH"]
    assert command.target is not None
    assert command.target.task_id == "task-article"
    request = json.loads(llm.calls[0]["messages"][1]["content"])
    # Interpreter context carries the task's user-facing lifecycle facts, but
    # canonical task identity remains resolver-owned.
    assert "task_id" not in request["context"]["active_tasks"][0]
    assert request["context"]["active_tasks"][0]["status"] == "RUNNING"
    assert len(request["context"]["history"]) == 2


@pytest.mark.asyncio
async def test_goal_decomposer_rejects_lost_required_capability() -> None:
    payload = {
        "root": {
            "goal_id": "article",
            "description": "Create an article",
            "goal_type": "CREATE",
            "required_capabilities": ["GENERATE_CONTENT"],
        }
    }
    # The decomposer now gives the real LLM one contract-repair attempt.  This
    # fixture deliberately returns the same incomplete tree twice so the
    # safety validation remains covered.
    decomposer = GoalDecomposer(llm=_LLM(payload, payload))

    with pytest.raises(GoalDecompositionError) as error:
        await decomposer.decompose(
            # The model must preserve both semantic requirements from the
            # understanding contract, not silently collapse the task.
            command=Command(
                type=CommandType.CREATE,
                goal="Create and schedule an article",
                required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            ),
            available_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        )
    assert error.value.code == "GOAL_REQUIRED_CAPABILITY_MISSING"


@pytest.mark.asyncio
async def test_tool_selector_narrows_llm_candidates_by_semantic_metadata() -> None:
    llm = _LLM({
        "tool_name": "community.search_public_posts",
        "arguments": {"query": "Agent"},
        "reason": "The current task requires community search.",
        "confidence": 0.91,
    })
    search = _metadata("community.search_public_posts", "SEARCH_COMMUNITY")
    publish = _metadata("publication.schedule", "SCHEDULE_PUBLISH")
    goal = Goal(
        goal_id="research",
        description="Research Agent posts",
        required_capabilities=["SEARCH_COMMUNITY"],
    )
    observation = Observation(current_task={"capability": "SEARCH_COMMUNITY"})

    selected = await ToolSelector(llm=llm, model="test").select(
        goal,
        observation,
        [search, publish],
    )

    assert selected.tool_name == search.name
    request = json.loads(llm.calls[0]["messages"][1]["content"])
    assert request["candidate_tool_names"] == [search.name]


@pytest.mark.asyncio
async def test_dynamic_planner_reobserves_side_effect_failure() -> None:
    schedule = _metadata(
        "publication.schedule",
        "SCHEDULE_PUBLISH",
        policy=ToolPolicyMetadata(
            side_effect=SideEffectMetadata(has_side_effect=True, idempotent=True),
        ),
    )
    tree = GoalTree(root=Goal(goal_id="schedule", description="Schedule a post"))
    decision = await DynamicPlanner().decide(
        goal_tree=tree,
        agent_state=AgentState(goal=tree.root_goal),
        tool_catalog=[schedule],
        observations=[{
            "last_result": {
                "ok": False,
                "tool_name": schedule.name,
                "request_sent": True,
            }
        }],
    )

    assert decision.decision == PlanningDecisionType.CONTINUE
    assert "re-observe" in decision.reason


@pytest.mark.asyncio
async def test_dynamic_planner_rejects_continue_after_empty_read() -> None:
    planner = DynamicPlanner(llm=_LLM({
        "decision": "CONTINUE",
        "reason": "The analysis step can continue.",
    }))
    tree = GoalTree(root=Goal(goal_id="research", description="Research posts"))

    decision = await planner.decide(
        goal_tree=tree,
        agent_state=AgentState(goal=tree.root_goal),
        observations=[{"result_status": "EMPTY", "resource_count": 0}],
    )

    assert decision.decision == PlanningDecisionType.ASK_HUMAN
    assert "empty result" in decision.reason


@pytest.mark.asyncio
async def test_dynamic_planner_does_not_repeat_non_idempotent_failure() -> None:
    publish = _metadata(
        "publication.publish_now",
        "PUBLISH_NOW",
        policy=ToolPolicyMetadata(
            side_effect=SideEffectMetadata(
                has_side_effect=True,
                idempotent=False,
                destructive=True,
            ),
        ),
    )
    tree = GoalTree(root=Goal(goal_id="publish", description="Publish a post"))
    decision = await DynamicPlanner().decide(
        goal_tree=tree,
        agent_state=AgentState(goal=tree.root_goal),
        tool_catalog=[publish],
        observations=[{
            "last_result": {
                "ok": False,
                "tool_name": publish.name,
                "request_sent": None,
            }
        }],
    )

    assert decision.decision == PlanningDecisionType.ASK_HUMAN


@pytest.mark.asyncio
async def test_dynamic_planner_alternative_tool_is_catalog_validated() -> None:
    fallback = _metadata("community.get_post", "SEARCH_COMMUNITY")
    primary = _metadata("community.search_public_posts", "SEARCH_COMMUNITY")
    tree = GoalTree(root=Goal(goal_id="research", description="Research posts"))
    planner = DynamicPlanner(decision_maker=lambda _payload: {
        "decision": "SELECT_ALTERNATIVE_TOOL",
        "tool_name": fallback.name,
        "reason": "The primary search path is unavailable.",
    })
    decision = await planner.decide(
        goal_tree=tree,
        agent_state=AgentState(goal=tree.root_goal),
        tool_catalog=[primary, fallback],
    )

    assert decision.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL
    assert decision.tool_name == fallback.name
    state = AgentState(
        goal=tree.root_goal,
        available_tools=[primary, fallback],
    )
    AgentLoop._set_preferred_tool(state, decision)
    assert state.preferred_tool_name == fallback.name
    with pytest.raises(AgentLoopError, match="unavailable tool"):
        AgentLoop._set_preferred_tool(
            state,
            decision.model_copy(update={"tool_name": "community.missing"}),
        )


def test_multi_goal_plan_preserves_parallel_and_dependent_work() -> None:
    tree = GoalTree(root=Goal(
        goal_id="content_operation",
        description="Research, create, validate, schedule, and analyze an article",
        children=[
            Goal(
                goal_id="research",
                description="Research recent community posts",
                goal_type="RESEARCH",
                required_capabilities=["SEARCH_COMMUNITY"],
            ),
            Goal(
                goal_id="create",
                description="Create a draft from the research",
                goal_type="CREATE",
                required_capabilities=["GENERATE_CONTENT"],
                dependencies=["research"],
            ),
            Goal(
                goal_id="validate",
                description="Validate draft quality",
                goal_type="VALIDATE",
                required_capabilities=["VALIDATE_QUALITY"],
                dependencies=["create"],
            ),
            Goal(
                goal_id="schedule",
                description="Schedule the draft for tomorrow morning",
                goal_type="PUBLISH",
                required_capabilities=["SCHEDULE_PUBLISH"],
                dependencies=["create"],
            ),
            Goal(
                goal_id="analyze",
                description="Analyze performance after publication",
                goal_type="ANALYZE",
                required_capabilities=["ANALYZE_PERFORMANCE"],
                dependencies=["schedule"],
            ),
        ],
    ))

    compiler = GoalCompiler(CapabilityRegistry())
    graph = compiler.compile(tree)
    plan = compiler.compile_plan(tree, task_id="task-content-operation")
    order = [node.node_id for node in graph.topological_order()]
    positions = {value: index for index, value in enumerate(order)}

    assert set(order) == {"research", "create", "validate", "schedule", "analyze"}
    assert positions["research"] < positions["create"]
    assert positions["create"] < positions["validate"]
    assert positions["create"] < positions["schedule"]
    assert positions["schedule"] < positions["analyze"]
    assert {step.capability for step in plan.steps} == {
        "SEARCH_COMMUNITY",
        "GENERATE_CONTENT",
        "VALIDATE_QUALITY",
        "SCHEDULE_PUBLISH",
        "ANALYZE_PERFORMANCE",
    }


def test_phase7_evaluation_metrics_cover_intelligence_quality() -> None:
    metrics = EvaluationMetricsCalculator.compute([
        EvalResult(metrics={
            "task_success": 1.0,
            "plan_quality": 0.8,
            "recovery_success": 1.0,
            "multi_task": 1.0,
            "long_conversation_consistency": 0.9,
        })
    ])

    assert metrics.task_success_rate == 1.0
    assert metrics.plan_quality == 0.8
    assert metrics.recovery_success == 1.0
    assert metrics.multi_task_accuracy == 1.0
    assert metrics.long_conversation_consistency == 0.9


@pytest.mark.asyncio
async def test_context_compression_preserves_prior_summary_facts() -> None:
    service = ConversationService(recent_message_limit=2)
    summary = await service._build_summary(
        None,
        "Active article task: publish tomorrow morning.",
        [
            {"role": "user", "content": "Use recent community posts as references."},
            {"role": "assistant", "content": "The research step is complete."},
            {"role": "user", "content": "Keep the interview-oriented style."},
        ],
    )

    assert summary.startswith("Active article task: publish tomorrow morning.")
    assert "Use recent community posts as references." in summary
