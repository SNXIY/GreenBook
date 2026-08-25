"""Deterministic Task-level Semantic Confirmation policy and projection.

This module consumes already-resolved semantic facts.  It never parses user
language, calls an LLM, resolves a target, or changes Task/Objective state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_READ_CAPABILITIES = {
    "SEARCH_COMMUNITY",
    "SEARCH_POSTS",
    "GET_POST_DETAIL",
    "GET_POST",
    "READ_POST",
    "READ_CONTENT",
    "LIST_OWN_POSTS",
    "LIST_DRAFTS",
    "GET_DRAFT",
    "GET_SCHEDULE",
    "LIST_COMMENTS",
    "GET_POST_PERFORMANCE",
    "GET_ACCOUNT_SUMMARY",
    "ANALYZE_PERFORMANCE",
}

_DRAFT_ONLY_CAPABILITIES = {
    "GENERATE_CONTENT",
    "CREATE_DRAFT",
    "IMPROVE_CONTENT",
    "REVISE_DRAFT",
    "UPDATE_DRAFT",
    "MANAGE_DRAFT",
    "DELETE_DRAFT",
}

_WRITE_CAPABILITIES = _DRAFT_ONLY_CAPABILITIES | {
    "SCHEDULE_PUBLISH",
    "CREATE_SCHEDULE",
    "MANAGE_SCHEDULE",
    "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
    "DELETE_POST",
    "REPLY_USER",
    "REPLY_COMMENT",
}

_WRITE_ACTIONS = {
    "CREATE_DRAFT",
    "UPDATE_DRAFT",
    "DELETE_DRAFT",
    "CREATE_SCHEDULE",
    "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
    "DELETE_POST",
    "REPLY_COMMENT",
}

_NON_SEMANTIC_HASH_FIELDS = frozenset({
    "source_command_id",
    "change_id",
    "revision_id",
    "created_at",
    "updated_at",
    "completed_at",
})


@dataclass(frozen=True, slots=True)
class ConfirmationPolicyDecision:
    required: bool
    reason: str = ""


def confirmation_policy(command: Any, semantic_state: Any) -> ConfirmationPolicyDecision:
    """Return a deterministic confirmation decision from canonical facts only."""

    if semantic_state is None or bool(getattr(semantic_state, "clarification_required", False)):
        return ConfirmationPolicyDecision(False, "clarification_required")

    item_rows = list(getattr(semantic_state, "items", None) or ())
    command_caps = _normalized_values(getattr(command, "required_capabilities", ()) or ())
    item_caps = [
        _normalized_values(getattr(item, "capabilities", ()) or ())
        for item in item_rows
    ]
    # Resolved per-item capabilities are authoritative when present.  A
    # command without items still exposes its canonical capability list.
    capability_rows = item_caps if any(item_caps) else [command_caps]

    delta_rows = list(getattr(command, "task_changes", None) or ())
    delta_actions = [
        _semantic_action(change)
        for change in delta_rows
        if _semantic_action(change)
    ]
    capability_write_count = sum(
        len(set(values) & _WRITE_CAPABILITIES)
        for values in capability_rows
    )
    delta_write_count = sum(action in _WRITE_ACTIONS for action in delta_actions)
    # Command capabilities and TaskDelta actions are two canonical views of
    # the same resolved mutation in different command shapes.  Count the
    # larger view instead of adding them, otherwise one PUBLISH_NOW mutation
    # represented in both fields becomes a false "multiple writes" trigger.
    write_count = max(capability_write_count, delta_write_count)
    capability_non_draft_count = sum(
        len(set(values) & (_WRITE_CAPABILITIES - _DRAFT_ONLY_CAPABILITIES))
        for values in capability_rows
    )
    delta_non_draft_count = sum(
        action not in {"CREATE_DRAFT", "UPDATE_DRAFT", "DELETE_DRAFT"}
        for action in delta_actions
    )
    non_draft_write_count = max(capability_non_draft_count, delta_non_draft_count)

    if write_count < 1:
        return ConfirmationPolicyDecision(False, "no_write")

    # Draft-only work is intentionally excluded from the breadth rule.  This
    # keeps multiple low-risk drafts from becoming a mechanical confirmation.
    if non_draft_write_count == 0:
        return ConfirmationPolicyDecision(False, "draft_only")

    target_ids = {
        target_id
        for change in delta_rows
        for target_id in [_target_id(change)]
        if target_id
    }
    for item in item_rows:
        reference = getattr(item, "target_reference", None) or {}
        if isinstance(reference, Mapping):
            for key in ("resource_id", "id", "target_id", "task_id"):
                value = str(reference.get(key) or "").strip()
                if value:
                    target_ids.add(value)
                    break
    if len(target_ids) >= 2:
        return ConfirmationPolicyDecision(True, "bulk_mutation")

    bindings = {
        (
            str(getattr(item, "publication_intent", "") or "").upper(),
            str(getattr(item, "run_at", None) or ""),
        )
        for item in item_rows
    }
    if len(item_rows) >= 2 and len(bindings) >= 2 and non_draft_write_count >= 1:
        return ConfirmationPolicyDecision(True, "multiple_temporal_or_publication_bindings")

    # Dependency edges are already canonical semantic facts.  This clause
    # does not plan or derive anything; it makes the multi-stage v1 trigger
    # explicit when the graph contains multiple external writes.
    dependency_edges = list(getattr(semantic_state, "dependencies", None) or ())
    for objective in list(getattr(semantic_state, "objectives", None) or ()):
        if isinstance(objective, Mapping):
            dependency_edges.extend(objective.get("dependencies") or ())
    if dependency_edges and non_draft_write_count >= 2:
        return ConfirmationPolicyDecision(True, "dependent_external_writes")

    if write_count >= 2:
        return ConfirmationPolicyDecision(True, "multiple_real_writes")

    return ConfirmationPolicyDecision(False, "single_simple_write")


def canonical_snapshot_hash(command: Any, semantic_state: Any, task: Any) -> str:
    """Hash canonical business facts without adding a second fact store."""

    payload = {
        "resolved_semantics": _dump(semantic_state),
        "objectives": [
            _dump(objective)
            for objective in (getattr(task, "objectives", None) or ())
        ],
        "resource_index": [
            _dump(resource)
            for resource in (getattr(task, "resource_index", None) or ())
        ],
        "task_changes": [
            _dump(change)
            for change in (getattr(command, "task_changes", None) or ())
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def confirmation_identity(task: Any) -> str:
    """Return an opaque identity for the current Task confirmation version."""

    task_id = str(getattr(task, "task_id", "") or "")
    version = int(getattr(task, "confirmation_version", 0) or 0)
    snapshot_hash = str(getattr(task, "confirmation_snapshot_hash", "") or "")
    raw = f"{task_id}|{version}|{snapshot_hash}".encode()
    return hashlib.sha256(raw).hexdigest()


def render_confirmation(
    command: Any,
    semantic_state: Any,
    task: Any,
    *,
    confirmation_id: str,
) -> dict[str, Any]:
    """Project canonical facts into a deterministic, public-safe preview."""

    objectives: list[dict[str, Any]] = []
    state_items = list(getattr(semantic_state, "items", None) or ())
    task_objectives = list(getattr(task, "objectives", None) or ())
    objectives_by_id = {
        str(getattr(item, "objective_id", "") or ""): item
        for item in task_objectives
        if getattr(item, "objective_id", None)
    }
    for index, objective in enumerate(task_objectives):
        item = state_items[index] if index < len(state_items) else None
        constraints = dict(getattr(objective, "constraints", None) or {})
        if item is not None:
            constraints = {
                **constraints,
                **dict(getattr(item, "constraints", None) or {}),
            }
        capabilities = _normalized_values(
            getattr(objective, "required_capabilities", None)
            or (getattr(item, "capabilities", None) if item is not None else ())
            or ()
        )
        dependencies = []
        for dependency_id in getattr(objective, "dependencies", None) or ():
            dependency = objectives_by_id.get(str(dependency_id))
            dependency_label = str(
                getattr(dependency, "description", "")
                or getattr(dependency, "intent", "")
                or ""
            ).strip()
            if dependency_label:
                dependencies.append(dependency_label[:300])
        objectives.append({
            "topic": str(
                getattr(objective, "description", "")
                or getattr(objective, "intent", "")
                or ""
            ),
            "desired_outcome": str(
                getattr(objective, "intent", "")
                or getattr(objective, "description", "")
                or ""
            ),
            "outcome": _render_outcome(
                capabilities,
                constraints.get("publication_intent") or (
                    getattr(item, "publication_intent", None) if item is not None else None
                ),
            ),
            "target": _render_target(item, semantic_state, command, index),
            "run_at": constraints.get("run_at") or (
                getattr(item, "run_at", None) if item is not None else None
            ),
            "timezone": constraints.get("timezone"),
            "publication_intent": constraints.get("publication_intent") or (
                getattr(item, "publication_intent", None) if item is not None else None
            ),
            "dependencies": dependencies,
            "has_real_side_effect": bool(set(capabilities) & _WRITE_CAPABILITIES),
        })

    return {
        "confirmation_id": confirmation_id,
        "task_id": str(getattr(task, "task_id", "") or ""),
        "task_version": int(getattr(task, "version", 0) or 0),
        "confirmation_version": int(getattr(task, "confirmation_version", 0) or 0),
        "title": str(
            getattr(task, "goal_summary", None)
            or getattr(task, "goal", None)
            or "请确认这项任务"
        ),
        "objectives": objectives,
        "has_real_side_effect": any(
            bool(item.get("has_real_side_effect")) for item in objectives
        ),
        "available_actions": ["CONFIRM", "MODIFY", "CANCEL"],
    }


def _render_target(item: Any, semantic_state: Any, command: Any, index: int) -> dict[str, Any] | None:
    raw: dict[str, Any] = {}
    if item is not None:
        candidate = getattr(item, "target_reference", None)
        if isinstance(candidate, Mapping):
            raw.update(dict(candidate))
    if index == 0:
        resolved = getattr(semantic_state, "resolved_target", None) or {}
        if isinstance(resolved, Mapping):
            raw = {**dict(resolved), **raw}
        command_target = getattr(command, "target", None)
        if command_target is not None and hasattr(command_target, "model_dump"):
            raw = {**command_target.model_dump(mode="json"), **raw}
    if not raw:
        return None
    target: dict[str, Any] = {}
    for key in ("kind", "type", "resource_kind"):
        if raw.get(key):
            target["kind"] = str(raw[key]).upper()
            break
    for key in ("label", "title", "reference", "name"):
        if raw.get(key):
            target["label"] = str(raw[key])[:300]
            break
    for key in ("resource_id", "id", "target_id"):
        if raw.get(key):
            target["resource_id"] = str(raw[key])
            break
    return target or None


def _render_outcome(capabilities: list[str], publication_intent: Any) -> str:
    """Render an outcome label from already-resolved canonical facts."""

    intent = str(publication_intent or "").strip().upper().replace("-", "_")
    if intent in {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "NOW"}:
        return "立即发布"
    if intent in {
        "SCHEDULED_PUBLISH",
        "SCHEDULE",
        "SCHEDULED",
        "FUTURE_PUBLISH",
        "FUTURE",
    }:
        return "定时发布"
    if "UPDATE_DRAFT" in capabilities or "REVISE_DRAFT" in capabilities:
        return "修改草稿"
    if set(capabilities) & _DRAFT_ONLY_CAPABILITIES:
        return "保存为草稿"
    return ""


def _first_action(capabilities: list[str], command: Any, index: int) -> str:
    for capability in capabilities:
        if capability in _WRITE_CAPABILITIES:
            return capability
    actions = [
        _semantic_action(change)
        for change in (getattr(command, "task_changes", None) or ())
        if _semantic_action(change)
    ]
    return actions[index] if index < len(actions) else ""


def _semantic_action(change: Any) -> str:
    desired = getattr(change, "desired_changes", None) or {}
    if not isinstance(desired, Mapping):
        return ""
    return str(desired.get("semantic_action") or "").strip().upper().replace("-", "_")


def _target_id(change: Any) -> str:
    reference = getattr(change, "target_reference", None) or {}
    if not isinstance(reference, Mapping):
        return ""
    for key in ("resource_id", "id", "target_id", "draft_id", "schedule_id", "post_id"):
        value = str(reference.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalized_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or ():
        normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _dump(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _dump(item)
            for key, item in value.items()
            if str(key) not in _NON_SEMANTIC_HASH_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


__all__ = [
    "ConfirmationPolicyDecision",
    "canonical_snapshot_hash",
    "confirmation_identity",
    "confirmation_policy",
    "render_confirmation",
]
