"""Small test-only GoalTree builder for execution infrastructure tests.

Production code receives a GoalTree from GoalDecomposer.  These tests need a
deterministic tree without invoking an LLM, so they compile typed Goals
directly through the canonical GoalCompiler.
"""

from __future__ import annotations

from typing import Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning.contracts import TaskPlan


class GoalPlanFactory:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._compiler = GoalCompiler(registry or CapabilityRegistry())

    def generate_plan(
        self,
        *,
        task_id: str = "",
        goal_category: str = "CREATE_CONTENT",
        requirements: list[dict[str, Any]] | None = None,
        goal_tree: GoalTree | None = None,
    ) -> TaskPlan:
        if goal_tree is not None:
            return self._compiler.compile_plan(goal_tree, task_id=task_id)
        values = requirements or [{"type": goal_category}]
        base = task_id or "test-task"
        goals: list[Goal] = []
        previous: str | None = None
        for index, value in enumerate(values, start=1):
            kind = str(value.get("type", goal_category) or goal_category).upper()
            goal_id = f"{base}-goal-{index}"
            constraint = dict(value)
            extra_constraints: list[dict[str, Any]] = []
            if kind == "PUBLISH" and not any(
                constraint.get(name)
                for name in ("run_at", "publish_at", "scheduled_at", "publish_time")
            ):
                # A schedule is not executable without its time.  Keep the
                # infrastructure fixtures explicit now that PlanValidator
                # fails closed instead of allowing an immediate-publish
                # fallback.
                extra_constraints.append({
                    "type": "run_at",
                    "value": "2030-01-01T00:00:00Z",
                })
            goals.append(Goal(
                goal_id=goal_id,
                description=str(value.get("description", kind)),
                goal_type=kind,
                dependencies=[previous] if previous else [],
                constraints=([constraint] if constraint else []) + extra_constraints,
            ))
            previous = goal_id
        root = Goal(
            goal_id=f"{base}-root",
            description=goal_category,
            goal_type="COMPOSITE",
            children=goals,
        )
        return self._compiler.compile_plan(
            GoalTree(root=root),
            task_id=task_id,
        )

    @staticmethod
    def fix_dependencies(plan: TaskPlan) -> TaskPlan:
        """Resolve placeholder edges used by a few hand-built tests."""

        step_ids = [step.step_id for step in plan.steps]
        for step in plan.steps:
            step.depends_on = [
                step_ids[int(value.removeprefix("_dep_"))]
                if value.startswith("_dep_") and value.removeprefix("_dep_").isdigit()
                else value
                for value in step.depends_on
            ]
        return plan

    _fix_dependencies = fix_dependencies


__all__ = ["GoalPlanFactory"]
