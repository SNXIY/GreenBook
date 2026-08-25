"""Explicit cross-conversation business-resource admission.

This module is intentionally a narrow read boundary. It only admits a typed
Draft/Schedule/Post identity already present in the structured Command. It
never searches, ranks, or resolves a resource from a label or conversation
recency. The returned candidate is transient and is consumed by the existing
TargetResolver/Objective path.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from greenbook_agent_core.command.models import Command


_RESOURCE_KINDS = {"DRAFT", "SCHEDULE", "POST"}
_RESOURCE_FIELDS = {
    "DRAFT": "draft_id",
    "SCHEDULE": "schedule_id",
    "POST": "post_id",
}
_READ_TOOLS = {
    "DRAFT": ("content.get_draft", "draft_id"),
    "SCHEDULE": ("publication.get_status", "schedule_id"),
    "POST": ("community.get_post", "post_id"),
}


@dataclass(frozen=True, slots=True)
class ExplicitResourceReference:
    kind: str
    resource_id: str


@dataclass(slots=True)
class ExplicitResourceAdmission:
    command: Command
    candidates: list[dict[str, Any]] = field(default_factory=list)
    external_candidates: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="python")
        return result if isinstance(result, Mapping) else None
    return None


def _resource_reference(value: Any) -> ExplicitResourceReference | None:
    item = _mapping(value)
    if item is None:
        return None
    raw_kind = str(item.get("resource_kind") or item.get("kind") or "").strip().upper()
    kind = raw_kind if raw_kind in _RESOURCE_KINDS else ""
    for candidate_kind, resource_field in _RESOURCE_FIELDS.items():
        raw_id = item.get(resource_field)
        if raw_id not in (None, ""):
            if kind and kind != candidate_kind:
                return None
            return ExplicitResourceReference(candidate_kind, str(raw_id).strip())
    raw_id = item.get("resource_id")
    if raw_id not in (None, "") and kind:
        return ExplicitResourceReference(kind, str(raw_id).strip())
    raw_id = item.get("id")
    if raw_id not in (None, "") and kind:
        return ExplicitResourceReference(kind, str(raw_id).strip())
    return None


def _walk_explicit(value: Any, *, depth: int = 0) -> list[ExplicitResourceReference]:
    if depth > 5:
        return []
    item = _mapping(value)
    if item is not None:
        found: list[ExplicitResourceReference] = []
        reference = _resource_reference(item)
        if reference is not None:
            found.append(reference)
        for nested in item.values():
            if isinstance(nested, (Mapping, list, tuple)) or callable(getattr(nested, "model_dump", None)):
                found.extend(_walk_explicit(nested, depth=depth + 1))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for nested in value:
            found.extend(_walk_explicit(nested, depth=depth + 1))
        return found
    return []


def explicit_resource_references(command: Command) -> list[ExplicitResourceReference]:
    """Extract only typed identity fields from the structured command."""

    found = _walk_explicit(command.model_dump(mode="python"))
    result: list[ExplicitResourceReference] = []
    seen: set[tuple[str, str]] = set()
    for reference in found:
        key = (reference.kind, reference.resource_id)
        if not reference.resource_id or key in seen:
            continue
        seen.add(key)
        result.append(reference)
    return result


def _payload_data(result: Any) -> Any:
    if isinstance(result, Mapping):
        return result.get("data")
    return getattr(result, "data", None)


def _result_ok(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("ok"))
    return bool(getattr(result, "ok", False))


def _value(data: Any, *names: str) -> str:
    item = _mapping(data)
    if item is None:
        return ""
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _candidate(reference: ExplicitResourceReference, data: Any) -> dict[str, Any]:
    field = _RESOURCE_FIELDS[reference.kind]
    return {
        "id": reference.resource_id,
        "resource_id": reference.resource_id,
        "resource_kind": reference.kind,
        "kind": reference.kind,
        field: reference.resource_id,
        "task_id": "",
        "objective_id": "",
        "label": _value(data, "title", "name", "label"),
        "title": _value(data, "title", "name", "label"),
        "status": _value(data, "status", "state"),
        "created_at": _value(data, "created_at", "createdAt"),
        "updated_at": _value(data, "updated_at", "updatedAt"),
        "resource_index": [{
            "resource_id": reference.resource_id,
            "resource_kind": reference.kind,
            "objective_id": "",
        }],
        "metadata": {
            "resource_refs": [{
                "resource_id": reference.resource_id,
                "resource_kind": reference.kind,
                "objective_id": "",
            }],
        },
        "lifecycle": "CURRENT",
        "explicit_business_resource": True,
        "ownership_verified": True,
        "source": "JAVA_BUSINESS_TRUTH",
    }


def _returned_id(kind: str, data: Any) -> str:
    aliases = {
        "draft_id": "draftId",
        "schedule_id": "scheduleId",
        "post_id": "postId",
    }
    field = _RESOURCE_FIELDS[kind]
    return _value(data, field, aliases[field], "resource_id", "resourceId", "id")


async def _execute_read(
    mcp: Any,
    tool_name: str,
    *,
    auth: Any,
    session: Any,
    trace_id: str,
    run_id: str,
    **arguments: Any,
) -> Any:
    result = mcp.execute_tool(
        tool_name,
        auth=auth,
        session=session,
        trace_id=trace_id,
        agent_run_id=run_id,
        tool_call_id=str(uuid.uuid4()),
        **arguments,
    )
    return await result if inspect.isawaitable(result) else result


async def _lookup(
    reference: ExplicitResourceReference,
    *,
    mcp: Any,
    auth: Any,
    session: Any,
    trace_id: str,
    run_id: str,
) -> tuple[dict[str, Any] | None, str]:
    tool_name, field = _READ_TOOLS[reference.kind]
    try:
        result = mcp.execute_tool(
            tool_name,
            auth=auth,
            session=session,
            trace_id=trace_id,
            agent_run_id=run_id,
            tool_call_id=str(uuid.uuid4()),
            **{field: reference.resource_id},
        )
        result = await result if inspect.isawaitable(result) else result
    except Exception as exc:  # noqa: BLE001 - admission fails closed
        return None, f"explicit_{reference.kind.lower()}_lookup_failed:{exc}"
    if not _result_ok(result):
        return None, f"explicit_{reference.kind.lower()}_not_owned_or_not_found"
    data = _payload_data(result)
    if _returned_id(reference.kind, data) != reference.resource_id:
        return None, f"explicit_{reference.kind.lower()}_identity_mismatch"
    owner_id = _value(
        data,
        "owner_id",
        "ownerId",
        "author_id",
        "authorId",
        "creator_id",
        "creatorId",
        "user_id",
        "userId",
    )
    if owner_id and owner_id != str(getattr(auth, "user_id", "")):
        return None, f"explicit_{reference.kind.lower()}_not_owned"
    if reference.kind == "DRAFT" and not owner_id:
        # The Java get-by-id endpoint is user-scoped, but a response without
        # an owner field is not sufficient proof for this admission boundary.
        try:
            owned = await _execute_read(
                mcp,
                "content.list_drafts",
                auth=auth,
                session=session,
                trace_id=trace_id,
                run_id=run_id,
            )
            items = _payload_data(owned) or []
            if not _result_ok(owned) or not any(
                _returned_id("DRAFT", item) == reference.resource_id
                for item in items
            ):
                return None, "explicit_draft_not_owned"
        except Exception as exc:  # noqa: BLE001
            return None, f"explicit_draft_ownership_check_failed:{exc}"
    if reference.kind == "SCHEDULE" and not owner_id:
        # ScheduledPublicationResponse intentionally has no owner_id.  Its
        # linked Draft is the Java-owned proof for a schedule mutation.
        draft_id = _value(data, "draft_id", "draftId")
        if not draft_id:
            return None, "explicit_schedule_ownership_unverified"
        try:
            owned_draft = await _execute_read(
                mcp,
                "content.get_draft",
                auth=auth,
                session=session,
                trace_id=trace_id,
                run_id=run_id,
                draft_id=draft_id,
            )
            draft_data = _payload_data(owned_draft)
            if (
                not _result_ok(owned_draft)
                or _returned_id("DRAFT", draft_data) != draft_id
            ):
                return None, "explicit_schedule_not_owned"
            draft_owner = _value(
                draft_data,
                "owner_id",
                "ownerId",
                "user_id",
                "userId",
            )
            if draft_owner and draft_owner != str(getattr(auth, "user_id", "")):
                return None, "explicit_schedule_not_owned"
            if not draft_owner:
                return None, "explicit_schedule_ownership_unverified"
        except Exception as exc:  # noqa: BLE001
            return None, f"explicit_schedule_ownership_check_failed:{exc}"
    if reference.kind == "POST":
        if not owner_id:
            # Public post detail is not sufficient proof of ownership. Ask the
            # user-owned Java projection before admitting a Post mutation.
            try:
                owned = await _execute_read(
                    mcp,
                    "community.list_own_posts",
                    auth=auth,
                    session=session,
                    trace_id=trace_id,
                    run_id=run_id,
                    page=1,
                    size=100,
                )
                items = _payload_data(owned) or []
                if not _result_ok(owned) or not any(
                    _returned_id("POST", item) == reference.resource_id
                    for item in items
                ):
                    return None, "explicit_post_not_owned"
            except Exception as exc:  # noqa: BLE001
                return None, f"explicit_post_ownership_check_failed:{exc}"
    return _candidate(reference, data), ""


def _same_resource(value: Any, reference: ExplicitResourceReference) -> bool:
    item = _mapping(value)
    if item is None:
        return False
    kind = str(item.get("resource_kind") or item.get("kind") or "").upper()
    resource_id = str(item.get("resource_id") or item.get("id") or "")
    return kind == reference.kind and resource_id == reference.resource_id


def _enrich_command(
    command: Command,
    candidates: list[dict[str, Any]],
    *,
    external_candidates: list[dict[str, Any]],
) -> Command:
    """Carry the typed admission into the existing mutation/objective path."""

    if not candidates:
        return command
    next_command = command.model_copy(deep=True)
    parameters = dict(next_command.parameters or {})
    parameters["__explicit_resource_admission"] = candidates
    # This is an admission projection only.  The coordinator may materialize
    # a fresh current-Conversation Task projection for a cross-Conversation
    # mutation before delegating to the unchanged ActionLoop.  It is not a
    # second resource store and never copies the source Conversation state.
    parameters["__external_explicit_resource_admission"] = external_candidates
    next_command.parameters = parameters

    if len(candidates) != 1:
        return next_command
    admitted = candidates[0]
    kind = str(admitted["resource_kind"])
    resource_id = str(admitted["resource_id"])
    field = _RESOURCE_FIELDS[kind]
    changes = []
    for change in next_command.task_changes or ():
        desired = dict(change.desired_changes or {})
        reference = dict(change.target_reference or {})
        action = str(desired.get("semantic_action") or "").upper()
        expected_kind = {
            "UPDATE_DRAFT": "DRAFT",
            "DELETE_DRAFT": "DRAFT",
            "CREATE_SCHEDULE": "DRAFT",
            "SCHEDULE_PUBLISH": "DRAFT",
            "PUBLISH_NOW": "DRAFT",
            "UPDATE_SCHEDULE": "SCHEDULE",
            "CANCEL_SCHEDULE": "SCHEDULE",
            "DELETE_POST": "POST",
        }.get(action, kind)
        existing = _resource_reference(reference) or _resource_reference(desired)
        if existing is None and expected_kind == kind:
            reference.update({
                "kind": kind,
                "resource_kind": kind,
                "id": resource_id,
                "resource_id": resource_id,
                field: resource_id,
                "explicit_business_resource": True,
            })
            desired["resource_target"] = dict(reference)
            change = change.model_copy(update={
                "target_reference": reference,
                "desired_changes": desired,
            })
        changes.append(change)
    next_command.task_changes = changes
    return next_command


async def admit_explicit_resources(
    command: Command,
    *,
    existing_candidates: list[Mapping[str, Any]] | None,
    mcp: Any,
    auth: Any,
    session: Any,
    trace_id: str,
    run_id: str,
) -> ExplicitResourceAdmission:
    references = explicit_resource_references(command)
    if not references:
        return ExplicitResourceAdmission(command=command)
    if mcp is None or auth is None or session is None:
        return ExplicitResourceAdmission(
            command=command,
            error="explicit_resource_lookup_unavailable",
        )

    existing = list(existing_candidates or [])
    admitted: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    for reference in references:
        candidate, error = await _lookup(
            reference,
            mcp=mcp,
            auth=auth,
            session=session,
            trace_id=trace_id,
            run_id=run_id,
        )
        if error or candidate is None:
            return ExplicitResourceAdmission(
                command=command,
                candidates=admitted,
                external_candidates=external,
                error=error or "explicit_resource_lookup_failed",
            )
        admitted.append(candidate)
        if not any(_same_resource(item, reference) for item in existing):
            external.append(candidate)
    enriched = _enrich_command(
        command,
        admitted,
        external_candidates=external,
    )
    return ExplicitResourceAdmission(
        command=enriched,
        candidates=admitted,
        external_candidates=external,
    )


__all__ = [
    "ExplicitResourceAdmission",
    "ExplicitResourceReference",
    "admit_explicit_resources",
    "explicit_resource_references",
]
