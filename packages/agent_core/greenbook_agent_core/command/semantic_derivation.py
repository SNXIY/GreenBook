"""Small compatibility projection for the structured semantic boundary.

The CommandInterpreter is the only open-language semantic authority.  This
module intentionally does not infer an operation, capability, publication
requirement, temporal meaning, or target from another field.  It only
normalizes values that are already present in the structured candidate so
legacy consumers can keep receiving the existing Command shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import Command, CommandItem

_PUBLICATION_ALIASES = {
    "DRAFT": "DRAFT_ONLY",
    "SAVE_DRAFT": "DRAFT_ONLY",
    "DO_NOT_PUBLISH": "DRAFT_ONLY",
    "NO_PUBLISH": "DRAFT_ONLY",
    "IMMEDIATE": "IMMEDIATE_PUBLISH",
    "PUBLISH": "IMMEDIATE_PUBLISH",
    "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
    "NOW": "IMMEDIATE_PUBLISH",
    "FUTURE": "SCHEDULED_PUBLISH",
    "SCHEDULE": "SCHEDULED_PUBLISH",
    "SCHEDULED": "SCHEDULED_PUBLISH",
    "SCHEDULE_PUBLISH": "SCHEDULED_PUBLISH",
    "PUBLISH_LATER": "SCHEDULED_PUBLISH",
}


@dataclass(frozen=True, slots=True)
class DerivedSemanticFacts:
    """Normalized copies of already structured semantic facts."""

    semantic_operation: str
    required_capabilities: tuple[str, ...]
    publication_intent: str
    first_action: str
    request_complexity: str
    needs_clarification: bool
    items: tuple[CommandItem, ...]


def derive_semantic_facts(
    command: Command,
    *,
    resolved_target_kind: str = "",
) -> DerivedSemanticFacts:
    """Project explicit candidate fields without semantic inference.

    ``resolved_target_kind`` is retained only for API compatibility.  Target
    resolution belongs to TargetResolver and must not change this projection.
    In particular, capabilities, task deltas, temporal text, and target kind
    are never used to invent a publication intent or operation.
    """

    del resolved_target_kind
    items = tuple(_project_item(item) for item in (command.items or ()))
    top_constraints = _project_constraints(command.constraints)
    publication_intent = _explicit_intent(top_constraints)
    item_intents = {
        intent
        for item in items
        if (intent := _explicit_intent(item.constraints))
    }
    if not publication_intent:
        if len(item_intents) == 1:
            publication_intent = next(iter(item_intents))
        elif len(item_intents) > 1:
            publication_intent = "MIXED"

    return DerivedSemanticFacts(
        semantic_operation=_normalize(command.semantic_operation),
        required_capabilities=tuple(_normalize_values(command.required_capabilities)),
        publication_intent=publication_intent,
        first_action=_normalize(command.first_action),
        request_complexity=_normalize(command.request_complexity) or "SIMPLE",
        needs_clarification=bool(command.needs_clarification),
        items=items,
    )


def apply_semantic_derivation(
    command: Command,
    *,
    resolved_target_kind: str = "",
) -> Command:
    """Return a compatibility-normalized Command.

    The historical function name is kept for callers and persisted imports.
    It is now a one-way formatting/projection boundary: no operation,
    capability, publication requirement, or clarification state is inferred.
    """

    facts = derive_semantic_facts(
        command,
        resolved_target_kind=resolved_target_kind,
    )
    constraints = _project_constraints(command.constraints)
    if facts.publication_intent and facts.publication_intent != "MIXED":
        constraints.setdefault("publication_intent", facts.publication_intent)
    return command.model_copy(update={
        "semantic_operation": facts.semantic_operation,
        "required_capabilities": list(facts.required_capabilities),
        "first_action": facts.first_action,
        "request_complexity": facts.request_complexity,
        "needs_clarification": facts.needs_clarification,
        "constraints": constraints,
        "items": list(facts.items),
    })


def _project_item(item: CommandItem) -> CommandItem:
    return item.model_copy(update={
        "operation": _normalize(item.operation) or "CREATE",
        "capabilities": _normalize_values(item.capabilities),
        "constraints": _project_constraints(item.constraints),
    })


def _project_constraints(value: Any) -> dict[str, Any]:
    result = dict(value) if isinstance(value, Mapping) else {}
    intent = _explicit_intent(result)
    if intent:
        result["publication_intent"] = intent
    return result


def _explicit_intent(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("publication_intent", "publication_mode", "content_state"):
        intent = _canonical_intent(value.get(key))
        if intent:
            return intent
    if value.get("publish_now") is True:
        return "IMMEDIATE_PUBLISH"
    if value.get("schedule") is True:
        return "SCHEDULED_PUBLISH"
    if value.get("publish") is True:
        return "SCHEDULED_PUBLISH"
    if value.get("publish") is False or value.get("schedule") is False:
        return "DRAFT_ONLY"
    return ""


def _canonical_intent(value: Any) -> str:
    normalized = _normalize(value)
    return _PUBLICATION_ALIASES.get(normalized, normalized)


def _normalize_values(values: Iterable[Any] | Any) -> list[str]:
    result: list[str] = []
    iterable = (
        values
        if isinstance(values, Iterable) and not isinstance(values, (str, bytes))
        else (values,)
    )
    for value in iterable:
        normalized = _normalize(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


__all__ = [
    "DerivedSemanticFacts",
    "apply_semantic_derivation",
    "derive_semantic_facts",
]
