"""Semantic action monotonicity tests.

A semantic action may only become more concrete as it crosses the Goal ->
Plan -> Replan boundaries; it must never change business meaning. A DRAFT_ONLY
Goal cannot gain a publication capability, a scheduled Goal cannot become
PUBLISH_NOW, and an immediate-publish Goal cannot become a scheduled one.

Two layers are covered:
- GoalCompiler.compile_plan rejects intent/capability mismatches (fail closed).
- DynamicPlanner.apply rejects an INSERT_STEP that introduces publication
  semantics the Goal did not declare.
"""

from __future__ import annotations

import pytest
from greenbook_agent_core.goal.compiler import GoalCompilationError, GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode
from greenbook_agent_core.planning.contracts import PlanningDecision, PlanningDecisionType
from greenbook_agent_core.planning.dynamic import DynamicPlanner

_RUN_AT = "2026-08-14T10:00:00+08:00"


def _tree(
    *,
    intent: str = "",
    capabilities: list[str] | None = None,
    run_at: str | None = None,
    constraints: list[dict] | None = None,
) -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="content goal",
            goal_type="CREATE",
            required_capabilities=list(capabilities or []),
            publication_intent=intent,
            temporal_constraint={"run_at": run_at} if run_at else {},
            constraints=constraints or [],
        )
    )


def _compile(tree: GoalTree) -> None:
    GoalCompiler().compile_plan(tree, task_id="t1")


# ── §24 Semantic transition: SCHEDULED ────────────────────────────────


def test_scheduled_intent_allows_schedule_capability() -> None:
    plan = GoalCompiler().compile_plan(
        _tree(intent="SCHEDULED_PUBLISH", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], run_at=_RUN_AT),
        task_id="t1",
    )
    assert [step.capability for step in plan.steps] == ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    assert plan.steps[1].constraints["run_at"] == _RUN_AT


def test_scheduled_intent_rejects_publish_now() -> None:
    with pytest.raises(GoalCompilationError, match="scheduled"):
        _compile(_tree(intent="SCHEDULED_PUBLISH", capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"]))


def test_scheduled_intent_requires_schedule_capability() -> None:
    with pytest.raises(GoalCompilationError, match="SCHEDULE_PUBLISH"):
        _compile(_tree(intent="SCHEDULED_PUBLISH", capabilities=["GENERATE_CONTENT"]))


# ── §25 Draft-only ─────────────────────────────────────────────────────


def test_draft_only_allows_generate_content() -> None:
    plan = GoalCompiler().compile_plan(
        _tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT"]),
        task_id="t1",
    )
    assert [step.capability for step in plan.steps] == ["GENERATE_CONTENT"]


def test_draft_only_rejects_schedule_capability() -> None:
    with pytest.raises(GoalCompilationError, match="DRAFT_ONLY"):
        _compile(_tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], run_at=_RUN_AT))


def test_draft_only_rejects_publish_now() -> None:
    with pytest.raises(GoalCompilationError, match="DRAFT_ONLY"):
        _compile(_tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"]))


def test_draft_only_rejects_run_at_without_publication_capability() -> None:
    # A publication time alone is a publication side effect for a DRAFT_ONLY Goal.
    with pytest.raises(GoalCompilationError, match="DRAFT_ONLY"):
        _compile(_tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT"], run_at=_RUN_AT))


def test_constraints_form_publication_intent_is_enforced() -> None:
    # Backwards-compatible constraints representation must obey the same rules.
    tree = _tree(
        capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        constraints=[{"publication_intent": "DRAFT_ONLY"}],
    )
    with pytest.raises(GoalCompilationError, match="DRAFT_ONLY"):
        _compile(tree)


# ── §26 Immediate publish ──────────────────────────────────────────────


def test_immediate_intent_requires_publish_now_capability() -> None:
    with pytest.raises(GoalCompilationError, match="PUBLISH_NOW"):
        _compile(_tree(intent="IMMEDIATE_PUBLISH", capabilities=["GENERATE_CONTENT"]))


def test_immediate_intent_allows_publish_now() -> None:
    plan = GoalCompiler().compile_plan(
        _tree(intent="IMMEDIATE_PUBLISH", capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"]),
        task_id="t1",
    )
    assert plan.steps[-1].capability == "PUBLISH_NOW"


def test_immediate_intent_rejects_schedule_capability() -> None:
    with pytest.raises(GoalCompilationError, match="immediate"):
        _compile(_tree(intent="IMMEDIATE_PUBLISH", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]))


# ── §26 Multi-goal decision isolation ──────────────────────────────────


def test_multi_goal_implicit_publish_now_rejected_without_explicit_intent() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two goals",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g1",
                    description="publish one",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
                ),
                Goal(
                    goal_id="g2",
                    description="draft the other",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                ),
            ],
        )
    )
    with pytest.raises(GoalCompilationError, match="explicitly declare IMMEDIATE_PUBLISH"):
        _compile(tree)


def test_multi_goal_explicit_immediate_publish_isolated() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two goals",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="g1",
                    description="publish one",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
                    publication_intent="IMMEDIATE_PUBLISH",
                ),
                Goal(
                    goal_id="g2",
                    description="draft the other",
                    goal_type="CREATE",
                    required_capabilities=["GENERATE_CONTENT"],
                    publication_intent="DRAFT_ONLY",
                ),
            ],
        )
    )
    plan = GoalCompiler().compile_plan(tree, task_id="t1")
    by_goal = {goal_id: [step for step in plan.steps if step.goal_id == goal_id] for goal_id in ("g1", "g2")}
    assert [step.capability for step in by_goal["g1"]] == ["GENERATE_CONTENT", "PUBLISH_NOW"]
    assert [step.capability for step in by_goal["g2"]] == ["GENERATE_CONTENT"]


# ── INSERT_STEP monotonicity (DynamicPlanner.apply) ───────────────────


def _insert(goal_tree: GoalTree, *, capability: str) -> None:
    decision = PlanningDecision(
        decision=PlanningDecisionType.INSERT_STEP,
        reason="replan insertion",
        insert_nodes=[TaskNode(task_id="insert-1", goal_id="g1", capability=capability)],
    )
    DynamicPlanner.apply(goal_tree, decision)


def test_insert_step_cannot_add_schedule_to_draft_only_goal() -> None:
    tree = _tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT"])
    with pytest.raises(ValueError, match="DRAFT_ONLY"):
        _insert(tree, capability="SCHEDULE_PUBLISH")


def test_insert_step_cannot_add_publish_now_to_draft_only_goal() -> None:
    tree = _tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT"])
    with pytest.raises(ValueError, match="DRAFT_ONLY"):
        _insert(tree, capability="PUBLISH_NOW")


def test_insert_step_cannot_add_publish_now_to_scheduled_goal() -> None:
    tree = _tree(
        intent="SCHEDULED_PUBLISH",
        capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        run_at=_RUN_AT,
    )
    with pytest.raises(ValueError, match="scheduled"):
        _insert(tree, capability="PUBLISH_NOW")


def test_insert_step_cannot_add_schedule_to_immediate_goal() -> None:
    tree = _tree(intent="IMMEDIATE_PUBLISH", capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"])
    with pytest.raises(ValueError, match="immediate-publish"):
        _insert(tree, capability="SCHEDULE_PUBLISH")


def test_insert_step_allows_non_publication_capability() -> None:
    tree = _tree(intent="DRAFT_ONLY", capabilities=["GENERATE_CONTENT"])
    decision = PlanningDecision(
        decision=PlanningDecisionType.INSERT_STEP,
        reason="add analysis",
        insert_nodes=[TaskNode(task_id="insert-1", goal_id="g1", capability="ANALYZE")],
    )
    updated = DynamicPlanner.apply(tree, decision)
    assert [node.task_id for node in updated.task_nodes] == ["insert-1"]


def test_insert_step_allows_declared_publication_capability() -> None:
    # A recovery insertion of an already-declared publication step is legal.
    tree = _tree(
        intent="SCHEDULED_PUBLISH",
        capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        run_at=_RUN_AT,
    )
    decision = PlanningDecision(
        decision=PlanningDecisionType.INSERT_STEP,
        reason="retry schedule step",
        insert_nodes=[
            TaskNode(task_id="insert-1", goal_id="g1", capability="SCHEDULE_PUBLISH", inputs={"run_at": _RUN_AT})
        ],
    )
    updated = DynamicPlanner.apply(tree, decision)
    assert any(node.capability == "SCHEDULE_PUBLISH" for node in updated.task_nodes)


def test_insert_step_without_publication_intent_is_unrestricted() -> None:
    tree = _tree(capabilities=["SEARCH"])
    decision = PlanningDecision(
        decision=PlanningDecisionType.INSERT_STEP,
        reason="add analysis",
        insert_nodes=[TaskNode(task_id="insert-1", goal_id="g1", capability="ANALYZE")],
    )
    updated = DynamicPlanner.apply(tree, decision)
    assert [node.task_id for node in updated.task_nodes] == ["insert-1"]
