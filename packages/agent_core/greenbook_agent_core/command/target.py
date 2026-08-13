"""Single target-resolution facade for Command Runtime.

The facade consumes structured target references and returns one of three
explicit outcomes.  It never silently selects an arbitrary recent object.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import Command, CommandContext, CommandTarget, TargetKind, TargetReferenceType


class TargetResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class TargetCandidate(BaseModel):
    kind: TargetKind = TargetKind.TASK
    id: str
    task_id: str | None = None
    resource_id: str | None = None
    artifact_id: str | None = None
    execution_id: str | None = None
    label: str | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.id

    @property
    def type(self) -> TargetKind:
        """Compatibility spelling for consumers of the old target model."""

        return self.kind


class TargetResolution(BaseModel):
    status: TargetResolutionStatus
    target: TargetCandidate | None = None
    candidates: list[TargetCandidate] = Field(default_factory=list)
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status == TargetResolutionStatus.RESOLVED and self.target is not None

    @property
    def is_ambiguous(self) -> bool:
        return self.status == TargetResolutionStatus.AMBIGUOUS


class Resolved(TargetResolution):
    status: Literal[TargetResolutionStatus.RESOLVED] = TargetResolutionStatus.RESOLVED


class Ambiguous(TargetResolution):
    status: Literal[TargetResolutionStatus.AMBIGUOUS] = TargetResolutionStatus.AMBIGUOUS


class NotFound(TargetResolution):
    status: Literal[TargetResolutionStatus.NOT_FOUND] = TargetResolutionStatus.NOT_FOUND


class TargetResolver:
    """Resolve a Command target against bounded conversation resources."""

    def resolve(self, command: Any, context: Any | None = None) -> TargetResolution:
        canonical = _canonical_command(command)
        requested = canonical.target
        if requested is None:
            return NotFound(reason="command_has_no_target")

        command_context = CommandContext.from_any(context)
        candidates = self._candidates(command_context, requested.kind)

        explicit = requested.explicit_id
        if explicit:
            matches = [item for item in candidates if item.identity == explicit]
            if len(matches) == 1:
                return self._resolved(matches[0], "explicit_identity")
            return NotFound(reason="explicit_identity_not_found")

        if requested.task_id:
            scoped = [item for item in candidates if item.task_id == requested.task_id]
            scoped_result = self._one_or_many(scoped, "task_scope")
            if scoped_result.status != TargetResolutionStatus.NOT_FOUND:
                return scoped_result

        if requested.reference_type == TargetReferenceType.ACTIVE:
            active = self._active_candidate(command_context, requested.kind, candidates)
            if active is not None:
                return self._resolved(active, "active_context_binding")
            return NotFound(reason="active_target_not_found")

        if requested.reference_type == TargetReferenceType.IDENTIFIER:
            matches = [item for item in candidates if item.identity == requested.reference]
            return self._one_or_many(matches, "structured_identifier")

        if requested.reference_type == TargetReferenceType.ORDINAL:
            ordinal = requested.ordinal
            if ordinal is None:
                return NotFound(reason="ordinal_missing")
            ordered = sorted(candidates, key=_timestamp, reverse=True)
            if 1 <= ordinal <= len(ordered):
                return self._resolved(ordered[ordinal - 1], "structured_ordinal")
            return NotFound(reason="ordinal_out_of_range")

        filtered = candidates
        if requested.reference_type == TargetReferenceType.PROPERTY:
            filtered = self._property_matches(requested, candidates)
        elif requested.reference_type == TargetReferenceType.TEMPORAL:
            filtered = self._temporal_matches(requested, candidates)
        elif requested.reference:
            filtered = self._text_matches(requested.reference, candidates)

        if requested.reference_type == TargetReferenceType.NONE and requested.reference:
            # A legacy projection may carry only a semantic reference string.
            # Preserve the safety rule: one candidate is sufficient evidence;
            # multiple candidates require clarification.
            return self._one_or_many(filtered or candidates, "unstructured_reference")

        return self._one_or_many(filtered, "structured_reference")

    @staticmethod
    def _candidates(
        context: CommandContext,
        requested_kind: TargetKind,
    ) -> list[TargetCandidate]:
        values: list[Any] = list(context.targets)
        if context.active_target is not None:
            values.append(context.active_target.model_dump(mode="json"))

        result: list[TargetCandidate] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            candidate = _candidate(value)
            if candidate is None:
                continue
            if requested_kind != TargetKind.TASK and candidate.kind != requested_kind:
                continue
            key = (candidate.kind.value, candidate.identity)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
        return result

    @staticmethod
    def _active_candidate(
        context: CommandContext,
        requested_kind: TargetKind,
        candidates: list[TargetCandidate],
    ) -> TargetCandidate | None:
        active = context.active_target
        if active is not None and (
            requested_kind == TargetKind.TASK or active.kind == requested_kind
        ):
            return _candidate(active.model_dump(mode="json"))
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _property_matches(
        requested: CommandTarget,
        candidates: list[TargetCandidate],
    ) -> list[TargetCandidate]:
        property_name = (requested.property or "").strip()
        expected = (requested.value or requested.reference or "").strip().casefold()
        if not property_name or not expected:
            return []
        matches: list[TargetCandidate] = []
        for item in candidates:
            actual = getattr(item, property_name, None)
            if actual is not None and str(actual).casefold() == expected:
                matches.append(item)
        return matches

    @staticmethod
    def _temporal_matches(
        requested: CommandTarget,
        candidates: list[TargetCandidate],
    ) -> list[TargetCandidate]:
        after = _parse_time(requested.after)
        before = _parse_time(requested.before)
        if after is None and before is None:
            return []
        result: list[TargetCandidate] = []
        for item in candidates:
            timestamp = _timestamp(item)
            if after is not None and timestamp < after:
                continue
            if before is not None and timestamp > before:
                continue
            result.append(item)
        return result

    @staticmethod
    def _text_matches(reference: str, candidates: list[TargetCandidate]) -> list[TargetCandidate]:
        normalized = reference.strip().casefold()
        if not normalized:
            return []
        return [
            item for item in candidates
            if normalized in {
                item.identity.casefold(),
                (item.label or "").casefold(),
                (item.status or "").casefold(),
            }
        ]

    @staticmethod
    def _resolved(candidate: TargetCandidate, reason: str) -> Resolved:
        return Resolved(target=candidate, candidates=[candidate], reason=reason)

    @staticmethod
    def _one_or_many(
        candidates: list[TargetCandidate],
        reason: str,
    ) -> TargetResolution:
        if len(candidates) == 1:
            return Resolved(target=candidates[0], candidates=candidates, reason=reason)
        if len(candidates) > 1:
            return Ambiguous(target=None, candidates=candidates, reason=f"{reason}_ambiguous")
        return NotFound(reason=f"{reason}_not_found")


UnifiedTargetResolver = TargetResolver
Resolver = TargetResolver


def _canonical_command(value: Any) -> Command:
    if isinstance(value, Command):
        return value
    return Command.model_validate(value)


def _candidate(value: Any) -> TargetCandidate | None:
    if isinstance(value, TargetCandidate):
        return value
    if isinstance(value, CommandTarget):
        value = value.model_dump(exclude_none=True)
    elif isinstance(value, Mapping):
        value = dict(value)
    else:
        return None

    kind_raw = (
        value.get("kind")
        or value.get("resource_kind")
        or value.get("type")
        or TargetKind.TASK.value
    )
    try:
        kind = TargetKind(str(kind_raw).upper())
    except ValueError:
        kind = TargetKind.TASK
    identifier = (
        value.get("id")
        or value.get("target_id")
        or value.get("resource_id")
        or value.get("draft_id")
        or value.get("schedule_id")
        or value.get("post_id")
        or value.get("execution_id")
        or value.get("task_id")
    )
    if not identifier:
        return None
    return TargetCandidate(
        kind=kind,
        id=str(identifier),
        task_id=_string(value.get("task_id")),
        resource_id=_string(value.get("resource_id") or identifier),
        artifact_id=_string(value.get("artifact_id")),
        execution_id=_string(value.get("execution_id")),
        label=_string(value.get("label") or value.get("title") or value.get("goal")),
        status=_string(value.get("status")),
        created_at=_string(value.get("created_at")),
        updated_at=_string(value.get("updated_at")),
        metadata=dict(value),
    )


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def _timestamp(value: TargetCandidate) -> datetime:
    return _parse_time(value.updated_at) or _parse_time(value.created_at) or datetime.min.replace(
        tzinfo=UTC
    )


__all__ = [
    "Ambiguous",
    "NotFound",
    "Resolved",
    "Resolver",
    "TargetCandidate",
    "TargetResolution",
    "TargetResolutionStatus",
    "TargetResolver",
    "UnifiedTargetResolver",
]
