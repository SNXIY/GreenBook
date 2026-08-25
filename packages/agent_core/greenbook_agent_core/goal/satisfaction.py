"""Deterministic Goal satisfaction and selection from real business facts.

A Goal is satisfied only when its desired business state provably exists in
the durable facts (owned Draft / Schedule / Post artifacts), never because an
Execution completed or the LLM said so. This module decides *which* Goal is
still unsatisfied; it never chooses the next action.

Facts are flat dictionaries keyed by goal_id with the shape:

    {"draft_id": str, "schedule_id": str, "post_id": str, "status": str}
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import Goal, GoalTree

PUBLICATION_INTENT_ALIASES = {
    "DRAFT": "DRAFT_ONLY",
    "SAVE_DRAFT": "DRAFT_ONLY",
    "DO_NOT_PUBLISH": "DRAFT_ONLY",
    "NO_PUBLISH": "DRAFT_ONLY",
    "SCHEDULE": "SCHEDULED_PUBLISH",
    "SCHEDULE_PUBLISH": "SCHEDULED_PUBLISH",
    "IMMEDIATE": "IMMEDIATE_PUBLISH",
    "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
    "NOW": "IMMEDIATE_PUBLISH",
}

_SKIPPED_GOAL_STATUSES = {"WAITING_APPROVAL", "WAITING_HUMAN", "CANCELLED"}


def publication_intent_of(goal: Goal) -> str:
    """Normalize a Goal's publication intent (field or constraints form)."""

    value = str(getattr(goal, "publication_intent", "") or "").strip()
    if not value:
        for item in getattr(goal, "constraints", ()) or ():
            if isinstance(item, Mapping) and item.get("publication_intent") not in (None, ""):
                value = str(item["publication_intent"]).strip()
                break
    normalized = value.upper().replace("-", "_").replace(" ", "_")
    return PUBLICATION_INTENT_ALIASES.get(normalized, normalized)


def goal_is_satisfied(goal: Goal, facts: Mapping[str, Any] | None) -> bool:
    """Return whether the Goal's desired business state provably exists.

    Publication-producing Goals require the owned artifact (draft/schedule/
    post).  Observation/reasoning Goals (no publication intent) are satisfied
    when every required capability has completed — including a reasoning-backed
    capability whose result artifact was produced by the AgentLoop.  A Goal is
    never satisfied merely because an LLM returned text.
    """

    facts = facts or {}
    intent = publication_intent_of(goal)
    if intent == "DRAFT_ONLY":
        return bool(facts.get("draft_id"))
    if intent == "SCHEDULED_PUBLISH":
        return bool(facts.get("draft_id") and facts.get("schedule_id"))
    if intent == "IMMEDIATE_PUBLISH":
        return bool(facts.get("post_id"))
    capabilities = {
        str(value).upper()
        for value in (getattr(goal, "required_capabilities", ()) or ())
        if str(value)
    }
    if capabilities:
        completed = {
            str(value).upper()
            for value in (facts.get("completed_capabilities") or ())
        }
        if capabilities <= completed:
            # A reasoning/tool result is only a business fact when its typed
            # output artifact is durable.  Keep the compatibility behaviour
            # for callers that only provide capability facts, but whenever a
            # projection supplies artifact types they become mandatory.
            artifact_types = facts.get("artifact_types")
            return not (artifact_types is not None and not artifact_types)
        # Declared capabilities that are not all completed prove the Goal is
        # still in flight — e.g. GENERATE_CONTENT done but SCHEDULE_PUBLISH
        # pending.  Falling through to the Draft-only fallback below would
        # wrongly mark the multi-step Goal satisfied the moment a Draft
        # exists, skipping the remaining publication step (observed: a
        # "…五分钟之后发布" task completed right after the draft was saved).
        return False
    # No publication intent and no completed capability requirement: an owned
    # Draft is the default desired result for content-producing Goals.
    return bool(facts.get("draft_id"))


def dependencies_satisfied(
    goal: Goal,
    all_goals: Mapping[str, Goal],
    facts_by_goal: Mapping[str, Mapping[str, Any]],
    *,
    skipped_statuses: frozenset[str] = _SKIPPED_GOAL_STATUSES,
) -> bool:
    """Return whether every dependency of ``goal`` is provably satisfied.

    Single readiness gate shared by Goal selection (``select_unsatisfied_goal_id``),
    concurrent ready-work selection (``ready_work.select_ready_work``), and the
    AgentLoop next-task scan.  A dependency that is waiting, cancelled, failed,
    absent, or merely present without its desired business state blocks the
    dependent Goal; goal order is never a dependency.
    """
    for dependency_id in goal.dependencies:
        dependency = all_goals.get(str(dependency_id))
        dependency_facts = facts_by_goal.get(str(dependency_id), {})
        dependency_status = str(dependency_facts.get("status") or "").upper()
        if dependency_status in skipped_statuses | {"FAILED"}:
            return False
        if dependency_status != "COMPLETED" and (
            dependency is None or not goal_is_satisfied(dependency, dependency_facts)
        ):
            return False
    return True


def goal_missing(goal: Goal, facts: Mapping[str, Any] | None) -> list[str]:
    """List the business facts still missing for satisfaction (diagnostics)."""

    facts = facts or {}
    intent = publication_intent_of(goal)
    missing: list[str] = []
    if not facts.get("draft_id"):
        missing.append("draft")
    if intent == "SCHEDULED_PUBLISH" and not facts.get("schedule_id"):
        missing.append("schedule")
    if intent == "IMMEDIATE_PUBLISH" and not facts.get("post_id"):
        missing.append("post")
    return missing


def select_unsatisfied_goal_id(
    goal_tree: GoalTree,
    facts_by_goal: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Select one still-unsatisfied executable Goal, deterministically.

    Rules (in order):
    1. an IN_PROGRESS, unsatisfied Goal is continued first;
    2. otherwise the first PENDING/unsatisfied Goal in GoalTree order;
    3. WAITING_APPROVAL / WAITING_HUMAN / CANCELLED Goals are skipped
       (independent siblings may continue; the waiting Goal itself is not
       re-submitted);
    4. FAILED Goals are skipped so one Goal's failure does not block
       independent siblings (failure isolation; dependency blocking is the
       planner's concern via GoalTree dependencies);
    5. every executable Goal satisfied or skipped -> "" (caller FINISHes).
    """

    if goal_tree is None:
        return ""
    facts_by_goal = facts_by_goal or {}
    # Keep this compatibility helper for callers that need one Goal, but make
    # its answer obey the same dependency gate as concurrent selection.  The
    # helper still returns one item; it no longer treats GoalTree list order as
    # an implicit dependency.
    all_goals = {goal.goal_id: goal for goal in goal_tree.all_goals()}

    unsatisfied: list[Goal] = []
    in_progress_unsatisfied: list[Goal] = []
    for goal in goal_tree.executable_goals():
        facts = facts_by_goal.get(goal.goal_id, {})
        status = str(facts.get("status") or "").upper()
        if status in _SKIPPED_GOAL_STATUSES:
            continue
        if status == "FAILED":
            continue
        if goal_is_satisfied(goal, facts):
            continue
        if not dependencies_satisfied(goal, all_goals, facts_by_goal):
            continue
        unsatisfied.append(goal)
        if status in {"IN_PROGRESS", "RUNNING", "QUEUED", "SUBMITTED"}:
            in_progress_unsatisfied.append(goal)
    candidates = in_progress_unsatisfied or unsatisfied
    return str(candidates[0].goal_id) if candidates else ""


def goal_states(
    goal_tree: GoalTree,
    facts_by_goal: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project per-Goal satisfaction state for the AgentLoop observation."""

    facts_by_goal = facts_by_goal or {}
    states: list[dict[str, Any]] = []
    for goal in goal_tree.executable_goals():
        facts = facts_by_goal.get(goal.goal_id, {})
        states.append({
            "goal_id": goal.goal_id,
            "description": str(goal.description or "")[:200],
            "publication_intent": publication_intent_of(goal),
            "run_at": facts.get("run_at") or "",
            "draft_id": str(facts.get("draft_id") or ""),
            "schedule_id": str(facts.get("schedule_id") or ""),
            "post_id": str(facts.get("post_id") or ""),
            "status": str(facts.get("status") or "PENDING"),
            "satisfied": goal_is_satisfied(goal, facts),
            "missing": goal_missing(goal, facts),
        })
    return states


__all__ = [
    "dependencies_satisfied",
    "goal_is_satisfied",
    "goal_missing",
    "goal_states",
    "publication_intent_of",
    "select_unsatisfied_goal_id",
]
