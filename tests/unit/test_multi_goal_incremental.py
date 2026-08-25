"""Phase 2 — multi-goal incremental execution tests.

Goal selection, real-business-fact satisfaction, owned-resource isolation,
failure isolation, and the 1..N incremental trace (3 generate + 2 schedule,
never a whole-plan 5-step execution).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from greenbook_agent_api.services.conversation_runtime_adapter import (
    _facts_from_execution_states,
    _find_incremental_submission,
    _incremental_plan,
)
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.goal.satisfaction import (
    goal_is_satisfied,
    goal_missing,
    goal_states,
    select_unsatisfied_goal_id,
)
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan

SCHEDULED = "SCHEDULED_PUBLISH"
DRAFT_ONLY = "DRAFT_ONLY"


def _three_goal_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="三篇帖子",
            goal_type="TASK",
            children=[
                Goal(
                    goal_id="G1",
                    description="Redis 高并发优化",
                    goal_type="CREATE",
                    publication_intent=SCHEDULED,
                    temporal_constraint={"run_at": "T1"},
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
                Goal(
                    goal_id="G2",
                    description="Agent Memory 设计",
                    goal_type="CREATE",
                    publication_intent=SCHEDULED,
                    temporal_constraint={"run_at": "T2"},
                    required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                ),
                Goal(
                    goal_id="G3",
                    description="Java 并发",
                    goal_type="CREATE",
                    publication_intent=DRAFT_ONLY,
                    required_capabilities=["GENERATE_CONTENT"],
                ),
            ],
        )
    )


def _facts(**goals: dict[str, str]) -> dict[str, dict[str, str]]:
    return {goal_id: dict(values) for goal_id, values in goals.items()}


def _execution_states(
    observations: list[dict[str, str]],
) -> list[dict[str, Any]]:
    return [dict(item) for item in observations]


# ── §39 Goal selection ──────────────────────────────────────────────────


def test_selects_in_progress_unsatisfied_goal_first() -> None:
    tree = _three_goal_tree()
    # G1 IN_PROGRESS + unsatisfied (has draft, missing schedule), G2 PENDING
    facts = _facts(
        G1={"draft_id": "D1", "status": "IN_PROGRESS"},
        G2={"status": "PENDING"},
    )
    assert select_unsatisfied_goal_id(tree, facts) == "G1"


def test_selects_next_pending_after_goal_satisfied() -> None:
    tree = _three_goal_tree()
    facts = _facts(
        G1={"draft_id": "D1", "schedule_id": "S1", "status": "COMPLETED"},
        G2={"status": "PENDING"},
        G3={"status": "PENDING"},
    )
    assert select_unsatisfied_goal_id(tree, facts) == "G2"


def test_selects_none_when_all_satisfied() -> None:
    tree = _three_goal_tree()
    facts = _facts(
        G1={"draft_id": "D1", "schedule_id": "S1", "status": "COMPLETED"},
        G2={"draft_id": "D2", "schedule_id": "S2", "status": "COMPLETED"},
        G3={"draft_id": "D3", "status": "COMPLETED"},
    )
    assert select_unsatisfied_goal_id(tree, facts) == ""


# ── §40 Draft-only satisfaction ─────────────────────────────────────────


def test_draft_only_goal_satisfied_by_owned_draft() -> None:
    tree = _three_goal_tree()
    g3 = next(g for g in tree.executable_goals() if g.goal_id == "G3")
    assert goal_is_satisfied(g3, {"draft_id": "D3"}) is True
    assert goal_missing(g3, {"draft_id": "D3"}) == []
    assert goal_is_satisfied(g3, {}) is False
    assert goal_missing(g3, {}) == ["draft"]


def test_draft_only_goal_never_requires_schedule() -> None:
    g3 = next(g for g in _three_goal_tree().executable_goals() if g.goal_id == "G3")
    assert "schedule" not in goal_missing(g3, {"draft_id": "D3"})


# ── §41 Scheduled satisfaction ──────────────────────────────────────────


def test_scheduled_goal_unsatisfied_with_draft_only() -> None:
    g1 = next(g for g in _three_goal_tree().executable_goals() if g.goal_id == "G1")
    assert goal_is_satisfied(g1, {"draft_id": "D1"}) is False
    assert goal_missing(g1, {"draft_id": "D1"}) == ["schedule"]


def test_scheduled_goal_satisfied_with_draft_and_schedule() -> None:
    g1 = next(g for g in _three_goal_tree().executable_goals() if g.goal_id == "G1")
    assert goal_is_satisfied(g1, {"draft_id": "D1", "schedule_id": "S1"}) is True
    assert goal_missing(g1, {"draft_id": "D1", "schedule_id": "S1"}) == []


# ── §42 Owned resource isolation ────────────────────────────────────────


def _state_with(tree: GoalTree, observations: list[dict[str, Any]]) -> Any:
    # Mirror the real continuation: completed step ids are derived from the
    # observed terminal capabilities using the compiler's step_id convention
    # (goal_id:capability-index), matching the GoalCompiler TaskNode order.
    owned_observations = [
        {"task_id": "t1", **observation}
        for observation in observations
    ]
    completed: list[str] = []
    for observation in owned_observations:
        goal = next(
            (
                item
                for item in tree.executable_goals()
                if item.goal_id == observation.get("goal_id")
            ),
            None,
        )
        if goal is None:
            continue
        capabilities = list(goal.required_capabilities)
        capability = str(observation.get("capability") or "")
        if capability in capabilities:
            completed.append(f"{goal.goal_id}:{capabilities.index(capability) + 1}")
    return SimpleNamespace(
        goal_tree=tree,
        # Execution evidence must carry a durable Task owner.  A missing
        # owner is deliberately ignored by the production continuation path
        # rather than being allowed to leak into another Task.
        context_snapshot={"execution_states": owned_observations},
        completed_task_ids=completed,
        resume_context=SimpleNamespace(completed_step_ids=completed),
    )


def _whole_plan() -> TaskPlan:
    return TaskPlan(
        task_id="t1",
        plan_source="GOAL_RUNTIME",
        steps=[
            PlanStep(step_id="G1:1", ordinal=1, capability="GENERATE_CONTENT", goal_id="G1"),
            PlanStep(step_id="G1:2", ordinal=2, capability="SCHEDULE_PUBLISH", goal_id="G1"),
            PlanStep(step_id="G2:1", ordinal=3, capability="GENERATE_CONTENT", goal_id="G2"),
            PlanStep(step_id="G2:2", ordinal=4, capability="SCHEDULE_PUBLISH", goal_id="G2"),
            PlanStep(step_id="G3:1", ordinal=5, capability="GENERATE_CONTENT", goal_id="G3"),
        ],
    )


def test_owned_draft_isolation_between_goals() -> None:
    tree = _three_goal_tree()
    # G1 generated D1; G1's next action must carry D1.
    state = _state_with(tree, [
        {"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"},
    ])
    plan = _incremental_plan(state, _whole_plan())
    assert [step.capability for step in plan.steps] == ["SCHEDULE_PUBLISH"]
    assert plan.steps[0].goal_id == "G1"
    assert plan.plan_id == "inc:t1:G1:SCHEDULE_PUBLISH:D1:"

    # G2's own draft D2 must never leak into G1's schedule identity.
    state_g2 = _state_with(tree, [
        {"goal_id": "G2", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D2"},
    ])
    plan_g2 = _incremental_plan(state_g2, _whole_plan())
    assert plan_g2.steps[0].goal_id == "G1"
    assert plan_g2.plan_id == "inc:t1:G1:GENERATE_CONTENT::"


def test_resumed_goal_advances_past_completed_step() -> None:
    tree = _three_goal_tree()
    state = _state_with(tree, [
        {"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"},
    ])
    state.completed_task_ids = ["G1:1"]
    plan = _incremental_plan(state, _whole_plan())
    assert [step.capability for step in plan.steps] == ["SCHEDULE_PUBLISH"]
    assert plan.plan_id == "inc:t1:G1:SCHEDULE_PUBLISH:D1:"


# ── §43 3-goal incremental trace ────────────────────────────────────────


def test_three_goal_incremental_trace_produces_five_durable_actions() -> None:
    tree = _three_goal_tree()
    observations: list[dict[str, Any]] = []
    actions: list[tuple[str, str]] = []
    plan_ids: list[str] = []
    state = _state_with(tree, observations)

    for _round in range(10):
        facts = _facts_from_execution_states(observations)
        current = select_unsatisfied_goal_id(tree, facts)
        if not current:
            break
        plan = _incremental_plan(state, _whole_plan())
        capability = str(plan.steps[0].capability)
        actions.append((current, capability))
        plan_ids.append(plan.plan_id)
        # Simulate the terminal observation for this action.
        observation = {
            "goal_id": current,
            "capability": capability,
            "status": "COMPLETED",
            "draft_id": f"D-{current}" if capability == "GENERATE_CONTENT" else "",
            "schedule_id": f"S-{current}" if capability == "SCHEDULE_PUBLISH" else "",
        }
        observations.append(observation)
        state = _state_with(tree, observations)

    assert actions == [
        ("G1", "GENERATE_CONTENT"),
        ("G1", "SCHEDULE_PUBLISH"),
        ("G2", "GENERATE_CONTENT"),
        ("G2", "SCHEDULE_PUBLISH"),
        ("G3", "GENERATE_CONTENT"),
    ]
    # 3 generate + 2 schedule, never a 5-step whole-plan.
    assert [cap for _, cap in actions].count("GENERATE_CONTENT") == 3
    assert [cap for _, cap in actions].count("SCHEDULE_PUBLISH") == 2
    # Deterministic identities: the same action for the same Goal and owned
    # resource must be stable.
    assert "inc:t1:G1:GENERATE_CONTENT::" in plan_ids
    assert "inc:t1:G1:SCHEDULE_PUBLISH:D-G1:" in plan_ids
    assert "inc:t1:G3:GENERATE_CONTENT::" in plan_ids


def test_goal_states_projection_reports_satisfaction() -> None:
    tree = _three_goal_tree()
    states = goal_states(tree, _facts(
        G1={"draft_id": "D1", "schedule_id": "S1"},
        G2={"draft_id": "D2"},
        G3={"draft_id": "D3"},
    ))
    by_id = {item["goal_id"]: item for item in states}
    assert by_id["G1"]["satisfied"] is True
    assert by_id["G2"]["satisfied"] is False
    assert by_id["G2"]["missing"] == ["schedule"]
    assert by_id["G3"]["satisfied"] is True
    assert by_id["G2"]["draft_id"] == "D2"


# ── §44 Failure isolation ───────────────────────────────────────────────


def test_failed_goal_does_not_block_siblings() -> None:
    tree = _three_goal_tree()
    facts = _facts(
        G1={"draft_id": "D1", "status": "FAILED", "schedule_id": ""},
        G2={"status": "PENDING"},
        G3={"status": "PENDING"},
    )
    assert select_unsatisfied_goal_id(tree, facts) == "G2"


def test_waiting_approval_goal_skipped_but_siblings_continue() -> None:
    tree = _three_goal_tree()
    facts = _facts(
        G1={"draft_id": "D1", "status": "WAITING_APPROVAL"},
        G2={"status": "PENDING"},
    )
    assert select_unsatisfied_goal_id(tree, facts) == "G2"


# ── §28 deterministic submission dedup ──────────────────────────────────


class _Repo:
    def __init__(self, executions: list[Any]) -> None:
        self._executions = executions

    def list_all(self):
        return list(self._executions)


class _Execution:
    def __init__(self, execution_id: str, plan_id: str, status: str) -> None:
        self.execution_id = execution_id
        self.plan_id = plan_id
        self.status = SimpleNamespace(value=status)


def test_deduplicated_submission_returns_existing_execution() -> None:
    repo = _Repo([
        _Execution("e1", "inc:t1:G1:SCHEDULE_PUBLISH:D1:", "QUEUED"),
    ])
    plan = _incremental_plan(
        _state_with(_three_goal_tree(), [{"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"}]),
        _whole_plan(),
    )
    result = _find_incremental_submission(repo, plan)
    assert result is not None
    assert result["execution_id"] == "e1"
    assert result["queued"] is True
    assert result["deduplicated"] is True


def test_completed_action_deduplicates_as_completed() -> None:
    repo = _Repo([
        _Execution("e1", "inc:t1:G1:SCHEDULE_PUBLISH:D1:", "COMPLETED"),
    ])
    plan = _incremental_plan(
        _state_with(_three_goal_tree(), [{"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"}]),
        _whole_plan(),
    )
    result = _find_incremental_submission(repo, plan)
    assert result is not None
    assert result["status"] == "COMPLETED"


def test_completed_dedup_preserves_resource_projection() -> None:
    artifact = SimpleNamespace(resource_type="DRAFT", artifact_type="DRAFT", resource_id="D2")
    step = SimpleNamespace(output_artifact=artifact, checkpoint_data={})
    execution = _Execution("e1", "inc:t1:G1:SCHEDULE_PUBLISH:D1:", "COMPLETED")
    execution.steps = [step]
    repo = _Repo([execution])
    plan = _incremental_plan(
        _state_with(_three_goal_tree(), [{"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"}]),
        _whole_plan(),
    )

    result = _find_incremental_submission(repo, plan)

    assert result is not None
    assert result["draft_id"] == "D2"
    assert result["resource_refs"] == [{"kind": "DRAFT", "resource_id": "D2"}]


def test_completed_dedup_reads_sibling_tool_result_refs() -> None:
    execution = _Execution("e1", "inc:t1:G1:SCHEDULE_PUBLISH:D1:", "COMPLETED")
    execution.steps = [SimpleNamespace(
        output_artifact=None,
        checkpoint_data={
            "completed_tool_result": {
                "data": {},
                "resource_refs": [{"kind": "DRAFT", "resource_id": "D3"}],
            }
        },
    )]
    repo = _Repo([execution])
    plan = _incremental_plan(
        _state_with(_three_goal_tree(), [{"goal_id": "G1", "capability": "GENERATE_CONTENT", "status": "COMPLETED", "draft_id": "D1"}]),
        _whole_plan(),
    )

    result = _find_incremental_submission(repo, plan)

    assert result is not None
    assert result["draft_id"] == "D3"


def test_failed_action_allows_fresh_retry() -> None:
    repo = _Repo([
        _Execution("e1", "inc:t1:G1:GENERATE_CONTENT::", "FAILED"),
    ])
    plan = _incremental_plan(_state_with(_three_goal_tree(), []), _whole_plan())
    assert _find_incremental_submission(repo, plan) is None
