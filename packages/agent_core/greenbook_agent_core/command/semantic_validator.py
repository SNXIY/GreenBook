"""Deterministic cross-field validation for Interpreter semantic candidates.

The Interpreter remains responsible for producing a candidate.  This module
only proves that the candidate's already-structured fields do not contradict
one another.  It never reads user text, calls an LLM, resolves a target, or
parses time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .models import Command, StructuredCommandOutput


class SemanticValidationError(BaseModel):
    """One bounded cross-field semantic contradiction."""

    code: str
    path: str
    reason: str


class SemanticValidationResult(BaseModel):
    """Validation result for one structured semantic candidate."""

    valid: bool
    errors: list[SemanticValidationError] = Field(default_factory=list)


_SCHEDULED_INTENTS = {
    "FUTURE",
    "FUTURE_PUBLISH",
    "PUBLISH_LATER",
    "SCHEDULE",
    "SCHEDULED",
    "SCHEDULED_PUBLISH",
    "UNRESOLVED",
}
_IMMEDIATE_INTENTS = {"IMMEDIATE", "IMMEDIATE_PUBLISH", "NOW", "PUBLISH_NOW"}
_SCHEDULED_CAPABILITIES = {
    "CREATE_SCHEDULE",
    "FUTURE_PUBLISH",
    "MANAGE_SCHEDULE",
    "SCHEDULE",
    "SCHEDULE_PUBLISH",
}
_IMMEDIATE_CAPABILITIES = {"IMMEDIATE_PUBLISH", "PUBLISH_NOW"}
_IMMEDIATE_OPERATIONS = {"IMMEDIATE_PUBLISH", "PUBLISH", "PUBLISH_NOW"}
_SCHEDULED_OPERATIONS = {
    "CREATE_SCHEDULE",
    "PUBLISH_LATER",
    "SCHEDULE",
    "SCHEDULE_PUBLISH",
    "UPDATE_SCHEDULE",
}
_SEARCH_OPERATIONS = {
    "FIND_POSTS",
    "SEARCH",
    "SEARCH_POSTS",
    "SEARCH_PUBLIC_POSTS",
}
_SEARCH_CAPABILITIES = {
    "FIND_POSTS",
    "SEARCH_COMMUNITY",
    "SEARCH_POSTS",
    "SEARCH_PUBLIC_POSTS",
}
_TEMPORAL_KEYS = (
    "run_at",
    "publish_at",
    "scheduled_at",
    "publish_time",
    "temporal_text",
    "temporal_kind",
)


def validate_semantic_candidate(
    candidate: Command | StructuredCommandOutput,
) -> SemanticValidationResult:
    """Validate only explicit semantic field combinations.

    ``Command`` is the normal integration input.  ``StructuredCommandOutput``
    is accepted so the same pure contract can be tested at the LLM schema
    boundary without constructing targets or invoking any resolver.
    """

    errors: list[SemanticValidationError] = []
    scheduled_intent = _scheduled_intent(candidate)
    immediate_intent = _immediate_intent(candidate)
    semantic_operation = _normalize(_value(candidate, "semantic_operation"))
    capabilities = _capabilities(candidate)
    immediate_operation = semantic_operation in _IMMEDIATE_OPERATIONS
    scheduled_operation = semantic_operation in _SCHEDULED_OPERATIONS
    immediate_capability = bool(capabilities & _IMMEDIATE_CAPABILITIES)
    scheduled_capability = bool(capabilities & _SCHEDULED_CAPABILITIES)
    unresolved = _unresolved_future(candidate, scheduled_intent)
    now_temporal = _has_now_temporal(candidate)

    # Rule 1/3: an explicit future/scheduled intent cannot be represented by
    # an immediate operation or capability.  The check is deliberately based
    # on structured evidence, not on the original user message.
    if scheduled_intent and (immediate_operation or immediate_capability) and not _mixed_item_publication(candidate):
        errors.append(SemanticValidationError(
            code="SEMANTIC_PUBLICATION_CONFLICT",
            path="semantic_operation" if immediate_operation else "required_capabilities",
            reason=(
                "scheduled/future publication intent conflicts with immediate "
                "publication operation or capability"
            ),
        ))

    if scheduled_intent and now_temporal:
        errors.append(SemanticValidationError(
            code="SEMANTIC_TEMPORAL_CONFLICT",
            path="temporal_kind",
            reason="scheduled/future publication cannot carry NOW temporal semantics",
        ))

    # Rule 2: an unresolved future request must remain a clarifying outcome;
    # it may not carry NOW semantics.
    if unresolved:
        if not bool(_value(candidate, "needs_clarification")):
            errors.append(SemanticValidationError(
                code="SEMANTIC_TEMPORAL_CONFLICT",
                path="needs_clarification",
                reason="unresolved future publication requires clarification",
            ))
        if immediate_operation or immediate_capability or now_temporal:
            errors.append(SemanticValidationError(
                code="SEMANTIC_TEMPORAL_CONFLICT",
                path="temporal_kind" if now_temporal else "semantic_operation",
                reason="unresolved future publication cannot carry NOW semantics",
            ))

    # Rule 4: keep the small operation/capability contract coherent.  This is
    # intentionally not a complete operation matrix.
    if immediate_operation and not immediate_capability:
        errors.append(SemanticValidationError(
            code="SEMANTIC_CAPABILITY_CONFLICT",
            path="required_capabilities",
            reason="immediate publication operation requires PUBLISH_NOW capability",
        ))
    if immediate_capability and not immediate_operation and not immediate_intent:
        errors.append(SemanticValidationError(
            code="SEMANTIC_CAPABILITY_CONFLICT",
            path="semantic_operation",
            reason="PUBLISH_NOW capability requires immediate publication semantics",
        ))
    if scheduled_operation and not scheduled_capability:
        errors.append(SemanticValidationError(
            code="SEMANTIC_CAPABILITY_CONFLICT",
            path="required_capabilities",
            reason="scheduled publication operation requires schedule capability",
        ))

    # Rule 5: a pure search envelope cannot carry publication temporal
    # evidence.  SEARCH_CREATE and other connected workflows are excluded by
    # requiring the operation and the complete capability set to be search-only.
    if (
        (
            _is_search_only(candidate, semantic_operation, capabilities)
            or _is_search_publication_conflict(candidate, semantic_operation, capabilities)
        )
        and (_has_publication_evidence(candidate) or _has_temporal_evidence(candidate))
    ):
        errors.append(SemanticValidationError(
            code="SEMANTIC_TEMPORAL_CONFLICT",
            path="items" if _has_item_temporal_evidence(candidate) else "constraints",
            reason="search-only semantic state cannot carry publication temporal evidence",
        ))

    return SemanticValidationResult(valid=not errors, errors=errors)


def _scheduled_intent(candidate: Any) -> bool:
    values = [_intent_from_mapping(_mapping_value(candidate, "constraints"))]
    values.extend(
        _intent_from_mapping(_mapping_value(candidate, key))
        for key in ("parameters", "entities")
    )
    values.append(_intent_from_value(_value(candidate, "semantic_operation")))
    values.extend(
        _intent_from_value(_value(delta, "desired_changes"))
        for delta in _sequence_value(candidate, "task_changes")
    )
    values.extend(
        _intent_from_mapping(_mapping_value(item, "constraints"))
        for item in _items(candidate)
    )
    if any(value in _SCHEDULED_INTENTS for value in values):
        return True
    return bool(_capabilities(candidate) & _SCHEDULED_CAPABILITIES)


def _immediate_intent(candidate: Any) -> bool:
    values = [_intent_from_mapping(_mapping_value(candidate, "constraints"))]
    values.append(_intent_from_value(_value(candidate, "semantic_operation")))
    values.extend(
        _intent_from_value(_value(delta, "desired_changes"))
        for delta in _sequence_value(candidate, "task_changes")
    )
    values.extend(
        _intent_from_mapping(_mapping_value(item, "constraints"))
        for item in _items(candidate)
    )
    return any(value in _IMMEDIATE_INTENTS for value in values)


def _mixed_item_publication(candidate: Any) -> bool:
    """Allow aggregate publication capabilities when ownership is per item."""

    modes: set[str] = set()
    items = list(_items(candidate))
    if len(items) < 2:
        return False
    for item in items:
        constraints = _mapping_value(item, "constraints")
        intent = _intent_from_mapping(constraints)
        capabilities = _item_capabilities(item)
        scheduled = intent == "SCHEDULED_PUBLISH" or bool(
            capabilities & _SCHEDULED_CAPABILITIES
        )
        immediate = intent == "IMMEDIATE_PUBLISH" or bool(
            capabilities & _IMMEDIATE_CAPABILITIES
        )
        if scheduled and immediate:
            return False
        if scheduled:
            modes.add("SCHEDULED")
        if immediate:
            modes.add("IMMEDIATE")
    return modes == {"SCHEDULED", "IMMEDIATE"}


def _unresolved_future(candidate: Any, scheduled_intent: bool) -> bool:
    if not scheduled_intent:
        return False
    if bool(_value(candidate, "needs_clarification")):
        return True
    top_constraints = _mapping_value(candidate, "constraints")
    if _normalize(top_constraints.get("temporal_kind")) == "UNRESOLVED":
        return True
    for item in _items(candidate):
        constraints = _mapping_value(item, "constraints")
        if _normalize(constraints.get("temporal_kind")) == "UNRESOLVED":
            return True
    return False


def _is_search_only(
    candidate: Any,
    semantic_operation: str,
    capabilities: set[str],
) -> bool:
    if semantic_operation not in _SEARCH_OPERATIONS:
        return False
    all_capabilities = set(capabilities)
    for item in _items(candidate):
        all_capabilities.update(_item_capabilities(item))
    if all_capabilities - _SEARCH_CAPABILITIES:
        return False
    return not (_scheduled_intent(candidate) or _immediate_intent(candidate))


def _is_search_publication_conflict(
    candidate: Any,
    semantic_operation: str,
    capabilities: set[str],
) -> bool:
    """Treat publication fields as pollution when SEARCH remains the action."""

    if semantic_operation not in _SEARCH_OPERATIONS:
        return False
    all_capabilities = set(capabilities)
    for item in _items(candidate):
        all_capabilities.update(_item_capabilities(item))
    non_publication = all_capabilities - (_SCHEDULED_CAPABILITIES | _IMMEDIATE_CAPABILITIES)
    return bool(non_publication) and non_publication <= _SEARCH_CAPABILITIES


def _has_publication_evidence(candidate: Any) -> bool:
    if _intent_from_mapping(_mapping_value(candidate, "constraints")):
        return True
    if _capabilities(candidate) & (_SCHEDULED_CAPABILITIES | _IMMEDIATE_CAPABILITIES):
        return True
    return any(
        _intent_from_mapping(_mapping_value(item, "constraints"))
        or _item_capabilities(item) & (_SCHEDULED_CAPABILITIES | _IMMEDIATE_CAPABILITIES)
        for item in _items(candidate)
    )


def _has_temporal_evidence(candidate: Any) -> bool:
    if _has_mapping_temporal_evidence(_mapping_value(candidate, "constraints")):
        return True
    if any(_has_mapping_temporal_evidence(_mapping_value(delta, "desired_changes")) for delta in _sequence_value(candidate, "task_changes")):
        return True
    return _has_item_temporal_evidence(candidate)


def _has_item_temporal_evidence(candidate: Any) -> bool:
    return any(
        _has_mapping_temporal_evidence(_mapping_value(item, "constraints"))
        or any(str(_value(item, key) or "").strip() for key in ("temporal_text", "run_at"))
        for item in _items(candidate)
    )


def _has_now_temporal(candidate: Any) -> bool:
    containers = [_mapping_value(candidate, "constraints")]
    containers.extend(_mapping_value(item, "constraints") for item in _items(candidate))
    return any(
        str(container.get("temporal_kind") or "").strip().upper().replace("-", "_") == "NOW"
        for container in containers
        if isinstance(container, Mapping)
    )


def _has_mapping_temporal_evidence(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in _TEMPORAL_KEYS:
        raw = value.get(key)
        if key == "temporal_kind" and _normalize(raw) in {"", "NONE"}:
            continue
        if str(raw or "").strip():
            return True
    return False


def _intent_from_mapping(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    raw = value.get("publication_intent") or value.get("publication_mode") or value.get("content_state")
    normalized = _normalize(raw)
    if normalized in _SCHEDULED_INTENTS:
        return "SCHEDULED_PUBLISH"
    if normalized in _IMMEDIATE_INTENTS:
        return "IMMEDIATE_PUBLISH"
    if value.get("publish_now") is True:
        return "IMMEDIATE_PUBLISH"
    if value.get("schedule") is True or value.get("publish") is True:
        return "SCHEDULED_PUBLISH"
    if any(str(value.get(key) or "").strip() for key in ("run_at", "publish_at", "scheduled_at", "publish_time")):
        return "SCHEDULED_PUBLISH"
    return ""


def _intent_from_value(value: Any) -> str:
    if isinstance(value, Mapping):
        intent = _intent_from_mapping(value)
        if intent:
            return intent
        value = value.get("semantic_action") or value.get("semantic_operation") or value.get("operation")
    normalized = _normalize(value)
    if normalized in _SCHEDULED_OPERATIONS or normalized in _SCHEDULED_INTENTS:
        return "SCHEDULED_PUBLISH"
    if normalized in _IMMEDIATE_OPERATIONS or normalized in _IMMEDIATE_INTENTS:
        return "IMMEDIATE_PUBLISH"
    return ""


def _capabilities(candidate: Any) -> set[str]:
    return {
        _normalize(value)
        for value in (_sequence_value(candidate, "required_capabilities") or ())
        if _normalize(value)
    }


def _item_capabilities(item: Any) -> set[str]:
    return {
        _normalize(value)
        for value in (_sequence_value(item, "capabilities") or ())
        if _normalize(value)
    }


def _items(candidate: Any) -> list[Any]:
    return list(_sequence_value(candidate, "items") or ())


def _sequence_value(value: Any, key: str) -> Sequence[Any]:
    result = _value(value, key)
    return result if isinstance(result, Sequence) and not isinstance(result, (str, bytes)) else ()


def _mapping_value(value: Any, key: str) -> Mapping[str, Any]:
    result = _value(value, key)
    return result if isinstance(result, Mapping) else {}


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_")


__all__ = [
    "SemanticValidationError",
    "SemanticValidationResult",
    "validate_semantic_candidate",
]
