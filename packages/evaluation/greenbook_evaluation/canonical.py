"""Canonical product-semantic contract used by Semantic Evaluation.

The production command and resolved-state models remain the source of facts.
This module only projects those facts into stable product-facing dimensions; it
does not classify user text or select an execution action.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SEARCH_ACTIONS = {
    "SEARCH",
    "SEARCH_POSTS",
    "SEARCH_PUBLIC_POSTS",
    "FIND_POSTS",
}
_QUERY_ACTIONS = {"QUERY", "LIST_DRAFTS", "GET_DRAFT", "GET_SCHEDULE"}
_CREATE_ACTIONS = {"CREATE", "CREATE_DRAFT", "GENERATE_CONTENT"}
_CREATE_ACTIONS |= {
    "CREATE_CONTENT_AND_SCHEDULE",
    "CREATE_CONTENT_AND_SCHEDULE_PUBLISH",
}
_REVISE_ACTIONS = {"MODIFY", "UPDATE", "UPDATE_DRAFT", "REVISE"}
_SCHEDULE_ACTIONS = {
    "SCHEDULE",
    "SCHEDULE_PUBLISH",
    "CREATE_SCHEDULE",
    "PUBLISH_LATER",
}
_CANCEL_ACTIONS = {"CANCEL", "CANCEL_SCHEDULE"}
_DELETE_ACTIONS = {"DELETE", "DELETE_POST", "DELETE_DRAFT"}
_CLARIFY_ACTIONS = {"UNKNOWN", "INCOMPLETE", "CLARIFY", "INVALID"}


def canonical_semantic_result(
    state: Mapping[str, Any],
    command: Any | None = None,
) -> dict[str, Any]:
    """Project a production ``ResolvedSemanticState`` into product facts."""

    operation = _upper(state.get("operation") or getattr(command, "type", ""))
    semantic_operation = _upper(
        state.get("semantic_operation")
        or getattr(command, "semantic_operation", "")
        or _task_delta_semantic_operation(command)
    )
    publication_intent = _publication_alias(state.get("publication_intent"))
    temporal_kind_raw = _upper(state.get("temporal_kind") or "NONE")
    temporal_resolved_raw = bool(state.get("temporal_resolved"))
    clarification = bool(state.get("clarification_required"))
    objectives = list(state.get("objectives") or ())
    items = list(state.get("items") or ())
    item_count = max(len(objectives), len(items))
    item_intents = {
        _publication_alias(item.get("publication_intent"))
        for item in items
        if isinstance(item, Mapping) and item.get("publication_intent")
    }
    item_intent_count = sum(
        1
        for item in items
        if isinstance(item, Mapping) and item.get("publication_intent")
    )
    item_temporals_all = {
        _upper(item.get("temporal_kind"))
        for item in items
        if isinstance(item, Mapping) and item.get("temporal_kind")
    }
    item_temporals = {value for value in item_temporals_all if value != "NONE"}
    temporal_items = [
        item
        for item in items
        if isinstance(item, Mapping)
        and _upper(item.get("temporal_kind")) in {"NOW", "FUTURE"}
    ]
    item_temporal_resolved = bool(temporal_items) and all(
        bool(item.get("temporal_resolved") or item.get("run_at"))
        for item in temporal_items
    )
    if item_temporal_resolved:
        temporal_resolved_raw = True

    action_family = _action_family(
        operation=operation,
        semantic_operation=semantic_operation,
        publication_intent=publication_intent,
        item_count=item_count,
        clarification=clarification,
    )
    publication_mode = _publication_mode(
        action_family=action_family,
        publication_intent=publication_intent,
        temporal_kind=temporal_kind_raw,
        temporal_resolved=temporal_resolved_raw,
        item_intents=item_intents,
        item_temporals=item_temporals,
        item_count=item_count,
        item_intent_count=item_intent_count,
    )
    temporal_kind = _temporal_kind(
        action_family=action_family,
        raw_kind=temporal_kind_raw,
        publication_mode=publication_mode,
        resolved=temporal_resolved_raw,
        item_temporals=item_temporals_all,
        item_count=item_count,
        item_intent_count=item_intent_count,
    )
    temporal_resolved = temporal_resolved_raw or temporal_kind == "NOW"
    target_state = _target_state(
        state=state,
        command=command,
        action_family=action_family,
        clarification=clarification,
    )
    objective_count = _objective_count(
        action_family=action_family,
        item_count=item_count,
        clarification=clarification,
    )
    return {
        "action_family": action_family,
        "publication_mode": publication_mode,
        "temporal_kind": temporal_kind,
        "temporal_resolved": temporal_resolved,
        "target_state": target_state,
        "clarification_required": clarification,
        "objective_count": objective_count,
        "task_expectation": "CLARIFY" if clarification else "READY",
    }


def semantic_values_equal(expected: Any, actual: Any, *, field: str = "") -> bool:
    """Compare canonical values with only explicit representation aliases."""

    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    if field == "objective_count":
        try:
            return int(expected) == int(actual)
        except (TypeError, ValueError):
            return expected == actual
    if isinstance(expected, str) and isinstance(actual, str):
        return _normalize_value(field, expected) == _normalize_value(field, actual)
    return expected == actual


def semantic_mapping_matches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> bool:
    return all(
        semantic_values_equal(value, actual.get(key), field=key)
        for key, value in expected.items()
    )


def _action_family(
    *,
    operation: str,
    semantic_operation: str,
    publication_intent: str,
    item_count: int,
    clarification: bool,
) -> str:
    if item_count > 1:
        return "MULTI_OBJECTIVE"
    if semantic_operation in _SEARCH_ACTIONS:
        return "SEARCH"
    if semantic_operation in {"SEARCH_CREATE", "RESEARCH_CREATE"}:
        return "SEARCH_CREATE"
    if semantic_operation in _QUERY_ACTIONS:
        return "QUERY"
    if semantic_operation in {"PUBLISH_NOW", "IMMEDIATE_PUBLISH", "PUBLISH"}:
        return "PUBLISH_NOW"
    if semantic_operation in _SCHEDULE_ACTIONS:
        return "SCHEDULE"
    if semantic_operation == "UPDATE_SCHEDULE":
        return "UPDATE_SCHEDULE"
    if semantic_operation in _CANCEL_ACTIONS:
        return "CANCEL"
    if semantic_operation in _DELETE_ACTIONS:
        return "DELETE"
    if semantic_operation in _REVISE_ACTIONS:
        return "REVISE"
    if semantic_operation in _CREATE_ACTIONS:
        return "CREATE"
    if operation in _CLARIFY_ACTIONS or (
        clarification
        and clarification_candidate(operation, semantic_operation, publication_intent)
    ):
        return "CLARIFY"
    if publication_intent in {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "NOW"}:
        return "PUBLISH_NOW"
    if publication_intent in {"SCHEDULED_PUBLISH", "SCHEDULE", "SCHEDULED", "FUTURE"}:
        return "SCHEDULE"
    if operation == "QUERY":
        return "QUERY"
    if operation == "CREATE":
        return "CREATE"
    if operation == "MODIFY":
        return "REVISE"
    if operation == "CANCEL":
        return "CANCEL"
    return operation or "UNKNOWN"


def _publication_mode(
    *,
    action_family: str,
    publication_intent: str,
    temporal_kind: str,
    temporal_resolved: bool,
    item_intents: set[str],
    item_temporals: set[str],
    item_count: int,
    item_intent_count: int,
) -> str:
    if action_family == "MULTI_OBJECTIVE":
        if item_intent_count < item_count and item_intents:
            return "MIXED"
        if item_intents == {"DRAFT_ONLY"}:
            return "DRAFT_ONLY"
        if item_intents == {"IMMEDIATE_PUBLISH"}:
            return "IMMEDIATE"
        if item_intents == {"SCHEDULED_PUBLISH"}:
            if temporal_kind == "UNRESOLVED" or not temporal_resolved:
                return "UNRESOLVED"
            return "SCHEDULED"
        if len(item_intents) > 1 or len(item_temporals) > 1:
            return "MIXED"
        if item_intents or "FUTURE" in item_temporals:
            if temporal_kind == "UNRESOLVED" or not temporal_resolved:
                return "UNRESOLVED"
            return "SCHEDULED"
        return "NONE"
    if publication_intent == "MIXED" or len(item_intents) > 1:
        return "MIXED"
    if publication_intent in {"DRAFT_ONLY", "DRAFT"}:
        return "DRAFT_ONLY"
    if len(item_intents) == 1:
        item_intent = next(iter(item_intents))
        if item_intent in {"DRAFT_ONLY", "DRAFT"}:
            return "DRAFT_ONLY"
        if item_intent in {
            "SCHEDULED_PUBLISH",
            "SCHEDULE",
            "SCHEDULED",
            "FUTURE",
        }:
            if temporal_kind == "UNRESOLVED" or not temporal_resolved:
                return "UNRESOLVED"
            return "SCHEDULED"
    if action_family == "PUBLISH_NOW" or publication_intent in {
        "IMMEDIATE_PUBLISH",
        "PUBLISH_NOW",
        "NOW",
    }:
        return "IMMEDIATE"
    if action_family in {"SCHEDULE", "UPDATE_SCHEDULE"} or publication_intent in {
        "SCHEDULED_PUBLISH",
        "SCHEDULE",
        "SCHEDULED",
        "FUTURE",
    }:
        if temporal_kind == "UNRESOLVED" or not temporal_resolved:
            return "UNRESOLVED"
        return "SCHEDULED"
    return "NONE"


def _temporal_kind(
    *,
    action_family: str,
    raw_kind: str,
    publication_mode: str,
    resolved: bool,
    item_temporals: set[str],
    item_count: int,
    item_intent_count: int,
) -> str:
    meaningful_temporals = {value for value in item_temporals if value != "NONE"}
    if (
        "NONE" in item_temporals
        and meaningful_temporals
        and item_intent_count < item_count
    ):
        return "MIXED"
    if raw_kind == "MIXED" or len(meaningful_temporals) > 1:
        return "MIXED"
    if action_family == "PUBLISH_NOW" or raw_kind == "NOW":
        return "NOW"
    if raw_kind == "UNRESOLVED" or publication_mode == "UNRESOLVED":
        # In a mixed multi-objective request, an unresolved future item is
        # still a FUTURE semantic requirement.  UNRESOLVED is reserved for a
        # single scheduled outcome whose required time cannot be grounded.
        if (
            action_family == "MULTI_OBJECTIVE"
            and publication_mode == "MIXED"
            and meaningful_temporals == {"UNRESOLVED"}
        ):
            return "FUTURE"
        return "UNRESOLVED"
    if raw_kind == "FUTURE" or publication_mode == "SCHEDULED":
        return "FUTURE" if resolved else "UNRESOLVED"
    return "NONE"


def _target_state(
    *,
    state: Mapping[str, Any],
    command: Any | None,
    action_family: str,
    clarification: bool,
) -> str:
    target_status = _upper(getattr(command, "target_resolution", ""))
    reason = _upper(state.get("clarification_reason"))
    if target_status == "AMBIGUOUS" or "AMBIGUOUS" in reason:
        return "AMBIGUOUS"
    if target_status in {"NOT_FOUND", "UNRESOLVED"} or "TARGET_UNRESOLVED" in reason:
        # Keep target resolution's three-state contract visible at the
        # product-semantic boundary.  Temporal unresolved remains orthogonal;
        # this branch is only the target resolver's zero-candidate outcome.
        return "NOT_FOUND"
    resolved_target = state.get("resolved_target") or {}
    target = getattr(command, "target", None)
    if resolved_target or target is not None:
        reference_type = _upper(getattr(target, "reference_type", ""))
        if reference_type in {"ACTIVE", "RECENT", "LATEST", "PROPERTY", "TEMPORAL"}:
            return "RESOLVED"
        if target is not None and (
            getattr(target, "explicit_id", None)
            or getattr(target, "ordinal", None)
        ):
            return "RESOLVED"
        return "RESOLVED"
    if action_family in {
        "PUBLISH_NOW",
        "SCHEDULE",
        "UPDATE_SCHEDULE",
        "CANCEL",
        "DELETE",
        "REVISE",
    } and clarification:
        return "MISSING"
    return "NONE"


def _task_delta_semantic_operation(command: Any | None) -> str:
    """Read the canonical business mutation from an existing TaskDelta."""

    for delta in getattr(command, "task_changes", ()) or ():
        desired = getattr(delta, "desired_changes", None)
        if desired is None and isinstance(delta, Mapping):
            desired = delta.get("desired_changes")
        if isinstance(desired, Mapping):
            value = desired.get("semantic_action") or desired.get("semantic_operation")
            if value:
                return _upper(value)
    return ""


def _objective_count(*, action_family: str, item_count: int, clarification: bool) -> int | None:
    if action_family in {"UNKNOWN", "CLARIFY", "INVALID"} and clarification:
        return None
    if action_family == "MULTI_OBJECTIVE":
        return max(item_count, 1)
    if action_family in {"QUERY", "SEARCH", "CREATE", "REVISE", "PUBLISH_NOW", "SCHEDULE", "UPDATE_SCHEDULE", "CANCEL", "DELETE", "SEARCH_CREATE"}:
        return 1
    return None


def clarification_candidate(
    operation: str,
    semantic_operation: str,
    publication_intent: str,
) -> bool:
    """Recognize an intentionally incomplete command without parsing text."""

    return (
        not semantic_operation
        and operation in {"QUERY", "UNKNOWN", ""}
        and not publication_intent
    )


def _normalize_value(field: str, value: str) -> str:
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "publication_mode": {
            "NONE": "NONE",
            "DRAFT": "DRAFT_ONLY",
            "IMMEDIATE_PUBLISH": "IMMEDIATE",
            "PUBLISH_NOW": "IMMEDIATE",
            "SCHEDULED_PUBLISH": "SCHEDULED",
            "SCHEDULE": "SCHEDULED",
            "SCHEDULED": "SCHEDULED",
            "FUTURE": "SCHEDULED",
            "MIXED": "MIXED",
            "UNRESOLVED": "UNRESOLVED",
        },
        "temporal_kind": {
            "NOW": "NOW",
            "FUTURE": "FUTURE",
            "NONE": "NONE",
            "MIXED": "MIXED",
            "UNRESOLVED": "UNRESOLVED",
        },
        "action_family": {
            "UNKNOWN": "CLARIFY",
            "INCOMPLETE": "CLARIFY",
            "INVALID": "INVALID",
        },
    }
    return aliases.get(field, {}).get(normalized, normalized)


def _upper(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().upper().replace("-", "_")


def _publication_alias(value: Any) -> str:
    normalized = _upper(value)
    return {
        "DRAFT": "DRAFT_ONLY",
        "SAVE_DRAFT": "DRAFT_ONLY",
        "IMMEDIATE": "IMMEDIATE_PUBLISH",
        "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
        "NOW": "IMMEDIATE_PUBLISH",
        "SCHEDULE": "SCHEDULED_PUBLISH",
        "SCHEDULED": "SCHEDULED_PUBLISH",
        "SCHEDULED_PUBLISH": "SCHEDULED_PUBLISH",
        "FUTURE": "SCHEDULED_PUBLISH",
    }.get(normalized, normalized)


__all__ = [
    "canonical_semantic_result",
    "semantic_mapping_matches",
    "semantic_values_equal",
]
