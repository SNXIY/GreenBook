"""TaskDelta normalization: split one delta spanning multiple business resources.

A single user turn can mutate a draft AND its schedule in one sentence ("改标题
+ 改到下午4点").  The CommandInterpreter occasionally bundles both into ONE
TaskDelta with a single ``semantic_action`` (e.g. ``UPDATE_DRAFT``) whose
``desired_changes`` carries fields of both the draft (title/content/...) and the
schedule (run_at/timezone/...).  The mutation pipeline schedules one business
execution per declared semantic_action, so a bundled ``run_at`` would silently
never reach ``UPDATE_SCHEDULE``.

This module decomposes such a delta at the command boundary: one TaskDelta per
business-resource group present in ``desired_changes``, each carrying its own
``semantic_action``.  It never invents user text, times, or resources — it only
partitions the fields the interpreter already produced, keeps the same owning
Task/Goal reference, and preserves every existing constraint.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import TaskDelta

# Field ownership: which business resource a mutation field belongs to.  A
# delta whose desired_changes carries fields from more than one group is one
# user turn mutating several distinct resources.
_DRAFT_MUTATION_FIELDS = frozenset({
    "title", "content", "instruction", "body", "summary",
})
_SCHEDULE_MUTATION_FIELDS = frozenset({
    "run_at", "timezone", "scheduled_at", "publish_at", "publish_time",
    "temporal_base",
})

# Cross-cutting fields copied verbatim into every decomposed delta so each
# mutation keeps the same owning Task/Goal reference and binding metadata.
_CROSS_CUTTING_FIELDS = frozenset({
    "goal_id", "task_id", "resource_target", "target",
})

_DRAFT_ACTION = "UPDATE_DRAFT"
_SCHEDULE_ACTION = "UPDATE_SCHEDULE"
_BUSINESS_ACTIONS = frozenset({_DRAFT_ACTION, _SCHEDULE_ACTION})


def normalize_task_deltas(deltas: Sequence[TaskDelta]) -> list[TaskDelta]:
    """Return the deltas with every multi-resource delta decomposed.

    Idempotent: a delta that already describes a single business resource is
    returned verbatim, so repeated normalization is a no-op.
    """
    normalized: list[TaskDelta] = []
    for delta in deltas:
        normalized.extend(_decompose_delta(delta))
    return normalized


def _decompose_delta(delta: TaskDelta) -> list[TaskDelta]:
    """Split one delta into one delta per owned business-resource group."""

    desired = dict(delta.desired_changes or {})
    semantic = str(
        desired.get("semantic_action") or desired.get("semantic_operation") or ""
    ).upper()
    if semantic not in _BUSINESS_ACTIONS:
        return [delta]
    draft_fields = {
        key: value
        for key, value in desired.items()
        if key in _DRAFT_MUTATION_FIELDS
    }
    schedule_fields = {
        key: value
        for key, value in desired.items()
        if key in _SCHEDULE_MUTATION_FIELDS
    }
    # No business fields at all, or the declared action already owns every
    # present field: nothing to decompose.
    if not draft_fields and not schedule_fields:
        return [delta]
    if semantic == _DRAFT_ACTION and not schedule_fields:
        return [delta]
    if semantic == _SCHEDULE_ACTION and not draft_fields:
        return [delta]

    groups: list[tuple[str, dict[str, Any]]] = []
    if draft_fields:
        groups.append((_DRAFT_ACTION, draft_fields))
    if schedule_fields:
        groups.append((_SCHEDULE_ACTION, schedule_fields))
    if not groups:
        return [delta]

    cross_cutting = {
        key: value
        for key, value in desired.items()
        if key in _CROSS_CUTTING_FIELDS
    }
    # Fields that belong to no declared business group (e.g. a descriptive
    # ``description``) are kept on the mutation matching the declared action so
    # no desired constraint is lost during the split.
    other_fields = {
        key: value
        for key, value in desired.items()
        if key not in _DRAFT_MUTATION_FIELDS
        and key not in _SCHEDULE_MUTATION_FIELDS
        and key not in _CROSS_CUTTING_FIELDS
        and key not in {"semantic_action", "semantic_operation"}
    }
    group_actions = {action for action, _fields in groups}
    decomposed: list[TaskDelta] = []
    for index, (action, fields) in enumerate(groups):
        changes: dict[str, Any] = dict(cross_cutting)
        changes.update(fields)
        if other_fields and (
            action == semantic or (semantic not in group_actions and index == 0)
        ):
            changes.update(other_fields)
        changes["semantic_action"] = action
        changes.pop("semantic_operation", None)
        change_id = delta.change_id
        if len(groups) > 1:
            # Distinct change ids so a consumer that deduplicates by change_id
            # does not drop the sibling mutation.
            base = change_id or delta.operation.value
            change_id = f"{base}:{index + 1}"
        decomposed.append(delta.model_copy(update={
            "desired_changes": changes,
            "change_id": change_id,
        }))
    return decomposed


__all__ = ["normalize_task_deltas"]
