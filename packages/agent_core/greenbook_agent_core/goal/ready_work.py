"""Small deterministic gate for concurrent Goal progress.

This module deliberately stops at *permission to make progress*.  It does
not select a tool or invent the next business action; AgentLoop and the
existing ToolPolicyGate retain that ownership.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import Goal, GoalTree
from .satisfaction import goal_is_satisfied

READ = "READ"
WRITE = "WRITE"
CREATE = "CREATE"
CONTROL = "CONTROL"

_WAITING_STATUSES = {
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "WAITING_USER",
    "WAITING_EXTERNAL",
    "RESULT_UNKNOWN",
    "VERIFYING_RESULT",
    "RECONCILING",
    "FAILED_RETRYABLE",
    "RETRYABLE",
    "RETRYING",
    "PAUSED",
    "CANCELLED",
}
_FAILED_STATUSES = {"FAILED", "ERROR"}
_IN_FLIGHT_STATUSES = {"QUEUED", "SUBMITTED", "RUNNING", "IN_PROGRESS"}
_RESOURCE_FIELDS = (
    ("draft_id", "draft"),
    ("post_id", "post"),
    ("schedule_id", "schedule"),
    ("resource_id", "resource"),
    ("artifact_id", "artifact"),
)


@dataclass(frozen=True, slots=True)
class WorkAccess:
    """Normalized concurrency metadata for one logical work item."""

    task_id: str = ""
    goal_id: str = ""
    resource_keys: tuple[str, ...] = ()
    access_mode: str = READ
    status: str = "PENDING"


@dataclass(frozen=True, slots=True)
class ReadyWork:
    """A Goal that passed the deterministic readiness gate."""

    goal: Goal
    task_id: str = ""
    resource_keys: tuple[str, ...] = ()
    access_mode: str = READ

    @property
    def goal_id(self) -> str:
        return self.goal.goal_id


def select_ready_work(
    goal_tree: GoalTree | None = None,
    facts_by_goal: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    in_flight_goal_ids: Iterable[str] = (),
    active_work: Iterable[Any] = (),
    work_items: Sequence[Any] | None = None,
    limit: int | None = None,
) -> list[ReadyWork]:
    """Return independent Goals that may be advanced now.

    Dependencies are read from ``Goal.dependencies``; list order is never a
    dependency.  ``active_work`` is execution metadata from durable state,
    not an in-memory dispatch set, so callers can use this after a restart.
    Resource conflict is conservative: two non-READ accesses to the same
    structured resource are serialized, while READ + READ is allowed.
    """

    facts = facts_by_goal or {}
    in_flight = {str(item) for item in in_flight_goal_ids}
    candidates: list[tuple[Goal, str, Mapping[str, Any]]] = []
    if work_items is not None:
        for item in work_items:
            goal = _as_goal(item)
            if goal is not None:
                item_facts = _facts_for(item, facts, goal.goal_id)
                candidates.append((goal, _task_id(item), item_facts))
    elif goal_tree is not None:
        candidates = [
            (goal, "", facts.get(goal.goal_id, {}))
            for goal in goal_tree.executable_goals()
        ]
    if not candidates:
        return []

    all_goals = {
        goal.goal_id: goal
        for goal in (goal_tree.all_goals() if goal_tree is not None else [])
    }
    active = [
        _work_access(item)
        for item in active_work
        if _status(item) not in {"COMPLETED", "FAILED", "CANCELLED"}
    ]
    result: list[ReadyWork] = []
    for goal, task_id, item_facts in candidates:
        status = _status(item_facts)
        if goal.goal_id in in_flight or status in _WAITING_STATUSES | _FAILED_STATUSES:
            continue
        if goal_is_satisfied(goal, item_facts) or status == "COMPLETED":
            continue
        if not _dependencies_satisfied(goal, all_goals, facts):
            continue
        access = WorkAccess(
            task_id=task_id,
            goal_id=goal.goal_id,
            resource_keys=resource_keys(goal, item_facts),
            access_mode=access_mode(goal),
            status=status,
        )
        if any(resource_conflict(access, other) for other in active):
            continue
        if any(
            resource_conflict(
                access,
                WorkAccess(
                    task_id=item.task_id,
                    goal_id=item.goal_id,
                    resource_keys=item.resource_keys,
                    access_mode=item.access_mode,
                    status="READY",
                ),
            )
            for item in result
        ):
            continue
        result.append(
            ReadyWork(
                goal=goal,
                task_id=task_id,
                resource_keys=access.resource_keys,
                access_mode=access.access_mode,
            )
        )
        if limit is not None and len(result) >= max(0, limit):
            break
    return result


def resource_conflict(left: Any, right: Any) -> bool:
    """Return whether two work items must not execute concurrently."""

    a = _work_access(left)
    b = _work_access(right)
    shared = set(a.resource_keys) & set(b.resource_keys)
    if not shared:
        return False
    return not (a.access_mode == READ and b.access_mode == READ)


def resource_keys(value: Any, facts: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Extract stable business resource keys, never titles or topics."""

    found: set[str] = set()
    facts = facts or {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for field, prefix in _RESOURCE_FIELDS:
                raw = item.get(field)
                if raw not in (None, "") and not isinstance(raw, (dict, list, tuple, set)):
                    found.add(f"{prefix}:{raw}")
            kind = str(item.get("resource_kind") or item.get("kind") or "").strip().lower()
            raw_id = item.get("id") or item.get("resource_id")
            if kind and raw_id not in (None, "") and kind not in {"goal", "task"}:
                found.add(f"{kind}:{raw_id}")
            for key in (
                "target",
                "resource",
                "resource_ref",
                "resource_refs",
                "artifact_refs",
                "execution_input",
                "task_context",
                "steps",
            ):
                nested = item.get(key)
                if isinstance(nested, (Mapping, list, tuple)):
                    visit(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)

    visit(value)
    visit(facts)
    return tuple(sorted(found))


def access_mode(value: Any) -> str:
    """Normalize explicit capability semantics into READ/WRITE/CREATE/CONTROL."""

    values: list[str] = []
    for item in _mapping_values(value):
        for key in ("access_mode", "resource_access", "mode"):
            raw = item.get(key)
            if raw:
                values.append(str(raw).upper())
        semantic = str(item.get("semantic_operation") or item.get("operation") or "").upper()
        if semantic:
            values.append(semantic)
        names = item.get("required_capabilities") or item.get("capabilities") or ()
        if isinstance(names, str):
            names = (names,)
        values.extend(str(name).upper() for name in names)
        policy = item.get("policy") or item.get("policy_snapshot") or {}
        if isinstance(policy, Mapping):
            side_effect = policy.get("side_effect") or {}
            if isinstance(side_effect, Mapping):
                raw = side_effect.get("access_mode")
                if raw:
                    values.append(str(raw).upper())
                if side_effect.get("destructive"):
                    values.append(CONTROL)
                elif side_effect.get("has_side_effect"):
                    values.append(WRITE)
        for nested in (item.get("execution_input"), item.get("steps")):
            if isinstance(nested, (Mapping, list, tuple)):
                values.extend(
                    value_item
                    for nested_item in (nested if isinstance(nested, (list, tuple)) else [nested])
                    for value_item in (
                        [access_mode(nested_item)]
                        if isinstance(nested_item, Mapping)
                        else []
                    )
                )
    for value_item in values:
        normalized = value_item.replace("-", "_").replace(" ", "_")
        if normalized in {CONTROL, "DELETE", "PUBLISH", "PUBLISH_NOW", "CANCEL"}:
            return CONTROL
        if normalized in {WRITE, "UPDATE", "MODIFY", "REVISE", "SCHEDULE", "WRITE_EXISTING"}:
            return WRITE
        if normalized in {CREATE, "GENERATE", "CREATE_DRAFT", "INSERT"}:
            return CREATE
        if normalized in {READ, "SEARCH", "ANALYZE", "ANALYTICS", "SUMMARIZE", "GET", "LIST"}:
            return READ
    return READ


def _dependencies_satisfied(
    goal: Goal,
    all_goals: Mapping[str, Goal],
    facts: Mapping[str, Mapping[str, Any]],
) -> bool:
    # Single readiness gate shared with Goal selection and the AgentLoop
    # next-task scan (goal.satisfaction.dependencies_satisfied).  FAILED /
    # WAITING statuses and absent business facts both block the dependent.
    from .satisfaction import dependencies_satisfied

    return dependencies_satisfied(goal, all_goals, facts)


def _work_access(value: Any) -> WorkAccess:
    if isinstance(value, WorkAccess):
        return value
    if isinstance(value, ReadyWork):
        return WorkAccess(
            task_id=value.task_id,
            goal_id=value.goal_id,
            resource_keys=value.resource_keys,
            access_mode=value.access_mode,
        )
    goal = _as_goal(value)
    if goal is not None:
        return WorkAccess(
            task_id=_task_id(value),
            goal_id=goal.goal_id,
            resource_keys=resource_keys(goal),
            access_mode=access_mode(goal),
            status=_status(value),
        )
    mapping = value if isinstance(value, Mapping) else {}
    keys = tuple(str(item) for item in (mapping.get("resource_keys") or ()))
    if not keys:
        keys = resource_keys(value)
    return WorkAccess(
        task_id=str(mapping.get("task_id") or ""),
        goal_id=str(mapping.get("goal_id") or ""),
        resource_keys=keys,
        access_mode=access_mode(value),
        status=_status(value),
    )


def _as_goal(value: Any) -> Goal | None:
    if isinstance(value, Goal):
        return value
    if isinstance(value, Mapping):
        raw = value.get("goal")
        if isinstance(raw, Goal):
            return raw
        if isinstance(raw, Mapping):
            try:
                return Goal.model_validate(raw)
            except Exception:
                return None
        if value.get("goal_id"):
            try:
                return Goal.model_validate(value)
            except Exception:
                return None
    return None


def _mapping_values(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        values = [value]
        for key in ("goal", "target", "constraints", "policy", "policy_snapshot"):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                values.append(nested)
            elif isinstance(nested, (list, tuple)):
                values.extend(item for item in nested if isinstance(item, Mapping))
        return values
    if isinstance(value, Goal):
        return _mapping_values(value.model_dump(mode="python"))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _mapping_values(model_dump(mode="python"))
        except TypeError:
            return _mapping_values(model_dump())
    return []


def _facts_for(item: Any, facts: Mapping[str, Mapping[str, Any]], goal_id: str) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        nested = item.get("facts")
        if isinstance(nested, Mapping):
            return nested
        return facts.get(goal_id, {})
    return facts.get(goal_id, {})


def _task_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("task_id") or "")
    return str(getattr(value, "task_id", "") or "")


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("status") or "PENDING").upper()
    return str(getattr(value, "status", "PENDING") or "PENDING").upper()


__all__ = [
    "CONTROL",
    "CREATE",
    "READ",
    "WRITE",
    "ReadyWork",
    "WorkAccess",
    "access_mode",
    "resource_conflict",
    "resource_keys",
    "select_ready_work",
]
