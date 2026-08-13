"""Phase 4 DynamicPlanner and typed replan tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from greenbook_agent_core.agent.state import AgentState
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.planning import (
    DynamicPlanner,
    PlanningDecision,
    PlanningDecisionType,
)


def _tree() -> GoalTree:
    return GoalTree(
        root=Goal(goal_id="research", description="研究 AI 趋势"),
        task_nodes=[
            TaskNode(task_id="search-1", goal_id="research", capability="SEARCH"),
        ],
    )


class _LLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(self.payload, ensure_ascii=False),
                ),
            )],
        )


@pytest.mark.asyncio
async def test_failed_observation_produces_replan_decision() -> None:
    planner = DynamicPlanner()
    decision = await planner.decide(
        goal_tree=_tree(),
        agent_state=AgentState(goal=_tree().root_goal),
        observations=[{"last_result": {"ok": False, "error_code": "NO_RESULTS"}}],
    )
    assert decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS


@pytest.mark.asyncio
async def test_structured_decision_maker_can_insert_a_step() -> None:
    async def decide(_payload):
        return {
            "decision": "INSERT_STEP",
            "reason": "需要补充分析",
            "insert_nodes": [
                {
                    "task_id": "analyze-1",
                    "goal_id": "research",
                    "capability": "ANALYZE",
                }
            ],
        }

    planner = DynamicPlanner(decision_maker=decide)
    decision = await planner.decide(goal_tree=_tree(), agent_state=AgentState())
    updated = planner.apply(_tree(), decision)
    assert decision.decision == PlanningDecisionType.INSERT_STEP
    assert [node.task_id for node in updated.task_nodes] == ["search-1", "analyze-1"]


@pytest.mark.asyncio
async def test_dependency_failure_rejects_blind_same_query_retry() -> None:
    planner = DynamicPlanner(
        llm=_LLM({
            "decision": "RETRY_WITH_NEW_ARGS",
            "reason": "Narrow the query after the exact search dependency failed.",
            "arguments": {"query": "AI"},
        }),
    )
    decision = await planner.decide(
        goal_tree=_tree(),
        agent_state=AgentState(goal=_tree().root_goal),
        tool_catalog=[],
        observations=[{
            "result_status": "FAILED",
            "failure_kind": "DEPENDENCY_UNAVAILABLE",
            "available_fallback_capabilities": [],
            "last_result": {
                "tool_name": "community.search_public_posts",
                "tool_arguments": {"query": "AI Agent", "size": 50},
                "ok": False,
            },
        }],
    )
    assert decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS
    assert decision.tool_name == "community.search_public_posts"
    assert decision.arguments == {"query": "AI"}


def test_decision_is_typed_and_plan_mutation_does_not_execute() -> None:
    tree = _tree()
    decision = PlanningDecision(
        decision=PlanningDecisionType.RETRY_WITH_NEW_ARGS,
        task_id="search-1",
        arguments={"query": "AI research"},
    )
    updated = DynamicPlanner.apply(tree, decision)
    assert updated.task_nodes[0].inputs == {"query": "AI research"}
    assert tree.task_nodes[0].inputs == {}
