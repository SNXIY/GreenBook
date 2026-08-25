"""Compatibility adapters between the legacy Goal/GoalTree/TaskNode models and
the new Objective / ActionStep models.

The new main path is Objective-driven; these adapters let the legacy fallback
translate without a schema migration.  Nothing here extends GoalTree or TaskNode
capability — it only converts existing data.
"""

from __future__ import annotations

from typing import Any

from .models import Objective, ObjectiveStatus


def goal_to_objective(goal: Any, task_id: str) -> Objective:
    """Convert one Goal into an Objective (LEGACY_COMPATIBILITY)."""
    kind = str(getattr(goal, "goal_type", "") or getattr(goal, "kind", "") or "").upper()
    target = dict(getattr(goal, "target", None) or {})
    capabilities = [
        str(item) for item in (getattr(goal, "required_capabilities", None) or ())
    ]
    expected_kind = _expected_kind(kind)
    if not expected_kind:
        # BUSINESS_OPERATION Goals carry their real resource contract in the
        # declared capability/target.  Do not leave the compatibility
        # projection resource-less, otherwise the Objective-driven runtime
        # loses the historical ResourceBinding at the GoalTree boundary.
        for capability in capabilities:
            expected_kind = _CAPABILITY_OBJECTIVE.get(
                capability.upper(), ("", "")
            )[0]
            if expected_kind:
                break
    related_resource_ids: list[str] = []
    for field in ("resource_id", "draft_id", "schedule_id", "post_id"):
        value = target.get(field)
        if value not in (None, "") and str(value) not in related_resource_ids:
            related_resource_ids.append(str(value))
    return Objective(
        objective_id=str(getattr(goal, "goal_id", "") or ""),
        task_id=task_id,
        description=str(getattr(goal, "description", "") or ""),
        intent=str(getattr(goal, "description", "") or ""),
        status=_objective_status(getattr(goal, "status", "")),
        expected_resource_kind=expected_kind,
        expected_postcondition={
            "outputs": [str(item) for item in (getattr(goal, "expected_outputs", None) or ())]
        },
        dependencies=[str(item) for item in (getattr(goal, "dependencies", None) or ())],
        constraints={
            "items": [dict(item) for item in (getattr(goal, "constraints", None) or ())
                      if isinstance(item, dict)],
            "target": target,
            "temporal": dict(getattr(goal, "temporal_constraint", None) or {}),
            "publication_intent": str(getattr(goal, "publication_intent", "") or ""),
        },
        required_capabilities=capabilities,
        related_resource_ids=related_resource_ids,
    )


def goals_to_objectives(goals: Any, task_id: str) -> list[Objective]:
    return [goal_to_objective(g, task_id) for g in (goals or ())]


def tasknode_to_action_step(node: Any):
    """Compatibility conversion retained for existing callers/tests."""
    from ..actionloop.models import ActionStepPlan

    return ActionStepPlan(
        step_id=str(getattr(node, "task_id", "") or ""),
        semantic_action=str(getattr(node, "capability", "") or ""),
        status="PENDING",
    )


def resolve_objectives(task: Any) -> list[Objective]:
    """Return the Task's objectives, converting legacy goals when none exist.

    The new main path stores Objectives; a Task that predates them still has
    ``goals``, which are translated deterministically (no new GoalTree).
    """
    objectives = list(getattr(task, "objectives", ()) or ())
    if objectives:
        return objectives
    goals = list(getattr(task, "goals", ()) or ())
    if goals:
        return goals_to_objectives(goals, str(getattr(task, "task_id", "") or ""))
    return []


_CAPABILITY_OBJECTIVE: dict[str, tuple[str, str]] = {
    "SEARCH_COMMUNITY": ("SEARCH_RESULT", "检索相关内容"),
    "GET_POST_DETAIL": ("POST", "获取内容详情"),
    "LIST_OWN_POSTS": ("POST", "查看自己的帖子"),
    "GENERATE_CONTENT": ("DRAFT", "创作内容"),
    "MANAGE_DRAFT": ("DRAFT", "修改草稿"),
    "DELETE_DRAFT": ("DRAFT", "删除草稿"),
    "SCHEDULE_PUBLISH": ("SCHEDULE", "安排发布"),
    "MANAGE_SCHEDULE": ("SCHEDULE", "调整发布计划"),
    "CANCEL_SCHEDULE": ("SCHEDULE", "取消发布"),
    "PUBLISH_NOW": ("POST", "立即发布"),
    "LIST_COMMENTS": ("COMMENT", "查看评论"),
    "REPLY_USER": ("COMMENT", "回复评论"),
    "ANALYZE_PERFORMANCE": ("POST", "分析表现"),
}


_WRITE_CAPS = {
    "GENERATE_CONTENT", "MANAGE_DRAFT", "MANAGE_SCHEDULE", "SCHEDULE_PUBLISH",
    "CANCEL_SCHEDULE", "PUBLISH_NOW", "DELETE_DRAFT", "REPLY_USER", "UPDATE_DRAFT",
    "CREATE_DRAFT",
}
_LLM_CAPS = {"ANALYZE_CONTENT_PATTERNS", "VALIDATE_QUALITY"}


def _item_result_requirement(caps: list[str]) -> str:
    names = {str(c).upper() for c in caps}
    if names & _WRITE_CAPS:
        return "RESOURCE_MUTATION"
    if names & _LLM_CAPS:
        return "GROUNDED_SYNTHESIS"
    return "DIRECT_RESULT"


def objectives_for_capabilities(
    capabilities: Any,
    task_id: str,
    *,
    fallback_intent: str = "",
) -> list[Objective]:
    """Deterministically create one Objective per required capability."""
    seen: set[str] = set()
    objectives: list[Objective] = []
    for capability in (capabilities or ()):
        name = str(getattr(capability, "name", "") or capability or "").upper()
        if not name or name in seen:
            continue
        seen.add(name)
        kind, label = _CAPABILITY_OBJECTIVE.get(name, ("", ""))
        objectives.append(Objective(
            task_id=task_id,
            description=label or name,
            intent=label or name,
            expected_resource_kind=kind,
        ))
    if not objectives and fallback_intent:
        objectives.append(Objective(
            task_id=task_id,
            description=fallback_intent,
            intent=fallback_intent,
            expected_resource_kind="",
        ))
    return objectives


def objectives_from_items(
    items: Any,
    task_id: str,
    *,
    timezone: str = "Asia/Shanghai",
    now: Any = None,
    resolved_state: Any = None,
) -> list[Objective]:
    """Create exactly one business Objective per CommandItem.

    A CommandItem is a business target, not a capability: one item may carry
    multiple capabilities (GENERATE_CONTENT + SCHEDULE_PUBLISH).  Each item with
    a non-empty ``temporal_text`` is resolved through TemporalResolver to a
    canonical absolute run_at stored in ``Objective.constraints``.  Items without
    a temporal expression get no run_at (they must not inherit another item's).
    """
    # Production callers pass the TurnCoordinator's resolved semantic state.
    # The resolver fallback remains only for historical direct callers that
    # predate the canonical state boundary.
    state_items = list(getattr(resolved_state, "items", ()) or ())
    resolver = None
    if resolved_state is None:
        from datetime import UTC, datetime
        from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
        now = now if now is not None else datetime.now(UTC)
        resolver = TemporalResolver()
    objectives: list[Objective] = []
    for item in (items or ()):
        if item is None:
            continue
        title = str(getattr(item, "title", "") or getattr(item, "topic", "") or "")
        intent = title or str(getattr(item, "operation", "") or "TASK")
        caps = list(getattr(item, "capabilities", ()) or ())
        kind = ""
        for cap in caps:
            k, _label = _CAPABILITY_OBJECTIVE.get(str(cap).upper(), ("", ""))
            if k:
                kind = k
                break
        objective = Objective(task_id=task_id, description=title, intent=intent,
                              expected_resource_kind=kind)
        requirements = [str(value) for value in (getattr(item, "requirements", ()) or ()) if str(value).strip()]
        if requirements:
            objective.constraints["requirements"] = requirements
        # result_requirement from the item's capabilities (metadata-driven):
        # any write/mutation capability => RESOURCE_MUTATION; pure LLM => synthesis.
        if caps:
            objective.result_requirement = _item_result_requirement(caps)
            # Preserve ALL required capabilities so the Objective is only
            # COMPLETED when every one has a verified resource (DRAFT AND
            # SCHEDULE), not just the first.
            objective.required_capabilities = [str(c).upper() for c in caps]
        resolved_item = state_items[len(objectives)] if len(state_items) > len(objectives) else None
        if resolved_item is not None:
            canonical_constraints = dict(getattr(resolved_item, "constraints", {}) or {})
            objective.constraints.update(canonical_constraints)
            if getattr(resolved_item, "publication_intent", ""):
                objective.constraints["publication_intent"] = str(
                    getattr(resolved_item, "publication_intent", "")
                )
            if getattr(resolved_item, "run_at", None):
                objective.constraints["run_at"] = str(resolved_item.run_at)
                objective.constraints["timezone"] = timezone
        elif resolver is not None:
            temporal_text = str(getattr(item, "temporal_text", "") or "").strip()
            if temporal_text:
                resolved = resolver.resolve(temporal_text, timezone=timezone, now=now)
                if resolved:
                    objective.constraints["run_at"] = str(resolved)
                    objective.constraints["timezone"] = timezone
        objectives.append(objective)
    return objectives


def _objective_status(value: Any) -> ObjectiveStatus:
    normalized = str(getattr(value, "value", value) if not isinstance(value, str) else value).upper()
    mapping = {
        "SUCCEEDED": ObjectiveStatus.COMPLETED,
        "COMPLETED": ObjectiveStatus.COMPLETED,
        "DONE": ObjectiveStatus.COMPLETED,
        "FAILED": ObjectiveStatus.FAILED,
        "ERROR": ObjectiveStatus.FAILED,
        "SUPERSEDED": ObjectiveStatus.SUPERSEDED,
        "WAITING": ObjectiveStatus.WAITING,
        "WAITING_HUMAN": ObjectiveStatus.WAITING,
        "WAITING_EXTERNAL": ObjectiveStatus.WAITING,
        "IN_PROGRESS": ObjectiveStatus.IN_PROGRESS,
        "RUNNING": ObjectiveStatus.IN_PROGRESS,
        "PROCESSING": ObjectiveStatus.IN_PROGRESS,
    }
    return mapping.get(normalized, ObjectiveStatus.PENDING)


def _expected_kind(goal_type: str) -> str:
    normalized = (goal_type or "").upper()
    if "DRAFT" in normalized or "CONTENT" in normalized or "GENERATE" in normalized:
        return "DRAFT"
    if "SCHEDULE" in normalized or "PUBLISH" in normalized:
        return "SCHEDULE"
    if "SEARCH" in normalized:
        return "SEARCH_RESULT"
    if "POST" in normalized:
        return "POST"
    return ""


__all__ = [
    "goal_to_objective",
    "goals_to_objectives",
    "objectives_for_capabilities",
    "resolve_objectives",
    "tasknode_to_action_step",
]
