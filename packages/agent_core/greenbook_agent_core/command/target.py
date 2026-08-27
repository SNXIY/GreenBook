"""Single target-resolution facade for Command Runtime.

The facade consumes structured target references and returns one of three
explicit outcomes.  It never silently selects an arbitrary recent object.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .models import (
    Command,
    CommandContext,
    CommandTarget,
    TargetKind,
    TargetReferenceType,
)

# Operation scope is a domain constraint, not a user-text classifier.  It is
# intentionally narrow: only operations whose legal resource kind is already
# part of the canonical command contract constrain a weak reference.  A
# generic REVISE/PUBLISH_NOW may address either editable drafts or posts in the
# current product, while resource-specific mutations stay typed.
_OPERATION_ALLOWED_KINDS: dict[str, frozenset[TargetKind]] = {
    "UPDATE_DRAFT": frozenset({TargetKind.DRAFT}),
    "DELETE_DRAFT": frozenset({TargetKind.DRAFT}),
    "UPDATE_SCHEDULE": frozenset({TargetKind.SCHEDULE}),
    # Creating a schedule consumes an existing Draft; the newly-created
    # Schedule is the operation output, not the user's input target.
    "CREATE_SCHEDULE": frozenset({TargetKind.DRAFT}),
    # The canonical semantic operation emitted by the Interpreter for the
    # same request family is SCHEDULE_PUBLISH.  Keep both compatibility
    # spellings in the resolver scope contract.
    "SCHEDULE_PUBLISH": frozenset({TargetKind.DRAFT}),
    "CANCEL_SCHEDULE": frozenset({TargetKind.SCHEDULE}),
    "DELETE_POST": frozenset({TargetKind.POST}),
    "REVISE": frozenset({TargetKind.DRAFT, TargetKind.POST}),
    "PUBLISH_NOW": frozenset({TargetKind.DRAFT, TargetKind.POST}),
}


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

    def resolve_task_delta(
        self,
        delta: Any,
        candidates: Sequence[Mapping[str, Any]],
        *,
        active_task_id: str = "",
        conversation_focus_task_id: str = "",
        user_input: str = "",
    ) -> TargetResolution:
        """Resolve a TaskDelta reference to one Task/Goal owner.

        TaskDelta references use the same safe three-state resolution contract
        as top-level Commands, but a Goal is represented as a candidate with
        its owning ``task_id``.  This keeps reference interpretation in the
        resolver; TaskManager only receives a concrete owner after this
        method returns ``RESOLVED``.
        """

        reference = getattr(delta, "target_reference", None) or {}
        if not isinstance(reference, Mapping):
            return NotFound(reason="delta_reference_invalid")
        operation = str(getattr(delta, "operation", "") or "").upper()
        desired = getattr(delta, "desired_changes", None) or {}
        semantic_action = str(
            desired.get("semantic_action")
            or desired.get("semantic_operation")
            or ""
        ).upper()
        # Providers use UPDATE_GOAL as the desired-state envelope for a
        # resource mutation as well.  Keep Objective-first resolution for
        # existing Objective projections; the typed-resource fallback below
        # handles snapshots that expose only the owning Task.
        resource_mutation = semantic_action in {
            "UPDATE_DRAFT",
            "DELETE_DRAFT",
            "CREATE_SCHEDULE",
            "SCHEDULE_PUBLISH",
            "UPDATE_SCHEDULE",
            "MANAGE_SCHEDULE",
            "CANCEL_SCHEDULE",
            "DELETE_POST",
            "PUBLISH_NOW",
        }
        goal_operation = operation in {"UPDATE_GOAL", "CANCEL_GOAL"}
        goal_id = _normalized_reference(
            reference.get("goal_id")
            or reference.get("objective_id")
            or reference.get("target_objective_id")
            or (
                reference.get("id")
                if _normalized_reference(
                    reference.get("resource_kind") or reference.get("kind")
                ).upper() not in {"DRAFT", "SCHEDULE", "POST"}
                else ""
            )
        )
        task_id = _normalized_reference(reference.get("task_id"))
        reference_type = _normalized_reference(reference.get("reference_type")).upper()
        label = _normalized_reference(
            reference.get("label")
            or reference.get("description")
            or reference.get("name")
        )

        values = [dict(item) for item in candidates if isinstance(item, Mapping)]
        if is_failed_objective_retry(delta, reference):
            return self._resolve_failed_objective_retry(
                values,
                reference=reference,
                goal_id=goal_id,
                task_id=task_id,
                label=label,
                user_input=user_input,
            )
        resource_id, resource_kind = _resource_reference(reference)
        # A natural cross-resource reference can name the source business
        # object rather than the object being mutated.  For example, users
        # say "这篇消息队列草稿" while the requested mutation is
        # UPDATE_SCHEDULE.  The provider may therefore carry ``kind=DRAFT``
        # as reference evidence even though the canonical action target must
        # be a Schedule.  A provider label is not a physical resource id;
        # when it has no explicit id, let the action contract select the
        # target kind and derive the concrete resource from the same scoped
        # Objective/Task lineage.  This preserves the existing 0/1/N
        # cardinality boundary and never converts a Draft id into a Schedule
        # id.
        allowed_kinds = _OPERATION_ALLOWED_KINDS.get(semantic_action, frozenset())
        if not resource_id and len(allowed_kinds) == 1:
            resource_kind = next(iter(allowed_kinds)).value
        # A provider may copy a label from the bounded context even when the
        # user supplied only an anaphoric reference such as "that one".  That
        # extra specificity is not grounding evidence.  Remove only the
        # unsupported label and let the existing 0/1/N resolver decide from
        # the canonical candidate set; this preserves a unique candidate and
        # keeps multiple candidates ambiguous.
        if label and user_input.strip() and not any(
            (goal_id, task_id, resource_id)
        ):
            # The coordinator supplies both the owning Task row and its
            # Objective rows.  They are two projections of one logical owner,
            # so grounding frequency must be measured over the rows that this
            # branch will actually resolve, otherwise every explicit topic is
            # incorrectly counted twice.
            grounding_values = values
            if goal_operation:
                objective_values = [item for item in values if _is_objective_candidate(item)]
                if resource_kind:
                    typed_objectives = [
                        item
                        for item in objective_values
                        if _candidate_has_resource_kind(item, resource_kind)
                    ]
                    if typed_objectives:
                        objective_values = typed_objectives
                if objective_values:
                    grounding_values = objective_values
            else:
                task_values = [item for item in values if not _is_objective_candidate(item)]
                if task_values:
                    grounding_values = task_values
            grounded = _reference_grounding_candidates(
                reference,
                user_input=user_input,
                candidates=grounding_values,
            )
            # If a label token is present in the current utterance, the user
            # supplied explicit subject evidence even when no current
            # candidate owns it.  Preserve the label so the normal resolver
            # returns NOT_FOUND.  Only discard provider-only specificity (the
            # G02 shape: generic "that one" plus a guessed label).
            normalized_label = _normalized_label(label).casefold()
            user_text = " ".join(str(user_input or "").split()).casefold()
            label_grounded_in_turn = bool(normalized_label and normalized_label in user_text)
            if not grounded and not label_grounded_in_turn:
                label = ""
                reference = {
                    key: value
                    for key, value in reference.items()
                    if key not in {"label", "reference", "description", "name"}
                }
        if not resource_kind:
            allowed_kinds = _OPERATION_ALLOWED_KINDS.get(semantic_action, frozenset())
            if len(allowed_kinds) == 1:
                resource_kind = next(iter(allowed_kinds)).value
        if resource_id:
            # A typed draft/schedule/post id is strong evidence for the
            # owning Task.  Resolve it against durable ResourceBindings before
            # considering labels, focus, or recency.  Use Task candidates
            # rather than every Goal in the Task so one resource shared by a
            # Task's GoalTree does not manufacture an ambiguity.
            task_values = [item for item in values if not _is_objective_candidate(item)]
            matches = _resource_owner_matches(
                task_values,
                resource_id=resource_id,
                resource_kind=resource_kind,
            )
            return self._delta_resolution(
                matches,
                reason="resource_reference",
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
        if goal_operation:
            # A Goal mutation may not use the active Task as an implicit Goal
            # target.  It must carry a resolvable Goal id, a strong label, an
            # ordinal ("第三篇"), a recency marker ("刚刚那篇") or a time
            # window ("下午那篇") — otherwise there is nothing to ground on.
            has_reference = bool(
                goal_id
                or label
                or _as_int(reference.get("ordinal")) is not None
                or reference_type in {"ACTIVE", "RECENT", "LATEST"}
                or _is_recent_label(label)
            )
            values = [item for item in values if _is_objective_candidate(item)]
            if task_id:
                values = [item for item in values if str(item.get("task_id")) == task_id]
            if goal_id:
                matches = [
                    item for item in values
                    if str(item.get("objective_id") or item.get("goal_id")) == goal_id
                ]
            elif not has_reference:
                # Conversation focus is only weak evidence.  It must not
                # turn an unqualified multi-Goal mutation into a selection;
                # preserve every candidate so the caller can distinguish
                # NOT_FOUND (zero) from AMBIGUOUS (more than one).
                matches = values
            else:
                _resource_id, explicit_resource_kind = _resource_reference(reference)
                if explicit_resource_kind and not (
                    not resource_id
                    and len(allowed_kinds) == 1
                    and explicit_resource_kind != next(iter(allowed_kinds)).value
                ):
                    resource_kind = explicit_resource_kind
                if resource_kind:
                    values = [
                        item for item in values
                        if _candidate_has_resource_kind(item, resource_kind)
                    ]
                    # "那篇草稿" names a typed resource class, not a
                    # title. Keep every matching Objective so ambiguity is
                    # surfaced instead of becoming NOT_FOUND.
                    if not _normalized_label(label):
                        matches = values
                    else:
                        matches = _resource_label_matches(
                            values,
                            label,
                            resource_kind,
                            user_input=user_input,
                        )
                        if not matches:
                            matches = _resolve_goal_by_reference(
                                values,
                                label,
                                reference,
                                reference_type,
                            )
                else:
                    matches = _resolve_goal_by_reference(
                        values,
                        label,
                        reference,
                        reference_type,
                    )
            if not matches and resource_mutation:
                # Some durable snapshots expose the Task/resource binding but
                # no flattened Objective row.  A typed, user-grounded label
                # may still resolve one owning Task; preserve the same
                # cardinality boundary and let the binding layer recover the
                # predecessor Objective from that concrete resource.
                task_values = [
                    dict(item)
                    for item in candidates
                    if isinstance(item, Mapping) and not _is_objective_candidate(item)
                ]
                if resource_kind:
                    # Task-level fallback must obey the same typed ownership
                    # boundary as Objective resolution.  A failed sibling
                    # may mention the user's topic while owning only a Draft;
                    # it cannot become an UPDATE_SCHEDULE candidate merely
                    # because its Task label contains the same words.
                    task_values = [
                        item
                        for item in task_values
                        if _candidate_has_resource_kind(item, resource_kind)
                    ]
                # A durable snapshot may expose only the owning Task row while
                # the provider's label names the concrete resource in a
                # longer sentence.  If the current utterance contains a
                # unique discriminator (for example, the topic token), use
                # that bounded evidence to select the owner.  Zero matches
                # still falls through to the normal resolver, and multiple
                # matches remain ambiguous; this never ranks by recency.
                grounded_task_matches = _reference_grounding_candidates(
                    reference,
                    user_input=user_input,
                    candidates=task_values,
                )
                task_matches = grounded_task_matches or _resolve_task_by_reference(
                    task_values,
                    label,
                    reference,
                    reference_type,
                    resource_kind=resource_kind,
                )
                return self._delta_resolution(
                    task_matches,
                    reason="typed_resource_task_reference",
                    resource_kind=resource_kind,
                    label=label,
                )
            return self._delta_resolution(
                matches,
                reason="goal_reference",
                resource_kind=resource_kind,
                label=label,
            )

        # Task-level mutations may use an explicit task id, a unique task
        # label, the active Task, or deterministic references: ordinal
        # ("第三篇"), most-recent ("刚刚那篇") and run-time ("下午那篇").
        task_values = [item for item in values if not _is_objective_candidate(item)]
        if task_id:
            matches = [item for item in task_values if str(item.get("task_id")) == task_id]
        else:
            matches = _resolve_task_by_reference(
                task_values,
                label,
                reference,
                reference_type,
                resource_kind=resource_kind,
                active_task_id=active_task_id,
                conversation_focus_task_id=conversation_focus_task_id,
            )
        return self._delta_resolution(
            matches,
            reason="task_reference",
            resource_kind=resource_kind,
            label=label,
        )

    def _resolve_failed_objective_retry(
        self,
        values: Sequence[Mapping[str, Any]],
        *,
        reference: Mapping[str, Any],
        goal_id: str,
        task_id: str,
        label: str,
        user_input: str = "",
    ) -> TargetResolution:
        """Resolve a user retry against one terminal FAILED Objective.

        This branch is deliberately narrower than ordinary follow-up
        resolution.  A user retry is a new logical Task, so ACTIVE/RECENT/
        LATEST and task-level resource fallback are not valid substitutes for
        a failed Objective identity.
        """

        objective_values = [
            item for item in values
            if item.get("objective_id")
        ]

        # A provider may enrich a generic FAILED reference with a label from
        # conversation context (for example, choosing the most recent failed
        # Objective).  That label is not user grounding unless the current
        # utterance contains evidence that can identify it.  Preserve the
        # existing three-state resolver contract by discarding unsupported
        # provider specificity before matching candidates.
        grounded_candidates = _reference_grounding_candidates(
            reference,
            user_input=user_input,
            candidates=objective_values,
        )
        grounding_insufficient = bool(user_input.strip()) and bool(label) and not grounded_candidates
        resolution_reason = "failed_objective_reference"
        if grounding_insufficient:
            goal_id = ""
            task_id = ""
            label = ""
            reference = {
                key: value
                for key, value in reference.items()
                if key in {"kind", "reference_type"}
            }
            resolution_reason = "failed_objective_reference_grounding"
        elif user_input.strip() and label and len(grounded_candidates) == 1:
            # A provider label may be a longer task sentence than the bounded
            # Objective label (for example, it may include "写一篇...短帖").
            # Once the current turn supplies one deterministic discriminator,
            # carry the candidate identity into the existing resolver instead
            # of requiring an exact full-sentence label match.
            grounded_objective_id = str(
                grounded_candidates[0].get("objective_id")
                or grounded_candidates[0].get("goal_id")
                or grounded_candidates[0].get("id")
                or ""
            )
            if grounded_objective_id and not goal_id and not task_id:
                goal_id = grounded_objective_id
                label = ""

        def matches_reference(item: Mapping[str, Any]) -> bool:
            if task_id and str(item.get("task_id") or "") != task_id:
                return False
            objective_id = str(item.get("objective_id") or "")
            if goal_id and objective_id != goal_id:
                return False
            if label:
                wanted = label.casefold()
                candidate_labels = [
                    _normalized_reference(item.get(key)).casefold()
                    for key in ("label", "task_label", "description", "intent")
                    if _normalized_reference(item.get(key))
                ]
                if not any(wanted in candidate_label for candidate_label in candidate_labels):
                    return False
            resource_id, resource_kind = _resource_reference(reference)
            return not resource_id or _failed_objective_owns_resource(
                item,
                resource_id,
                resource_kind,
            )

        referenced = [item for item in objective_values if matches_reference(item)]
        unknown_referenced = [
            item for item in referenced
            if _has_unreconciled_execution(item)
        ]
        safe_referenced = [
            item for item in referenced
            if str(item.get("status") or "").upper() == "FAILED"
            and not _has_unreconciled_execution(item)
        ]
        if safe_referenced:
            return self._delta_resolution(
                list(safe_referenced),
                reason=resolution_reason,
            )
        if unknown_referenced:
            return NotFound(reason="retry_requires_reconciliation")

        # No explicit identity means exactly one FAILED Objective is required.
        # A completed, cancelled, superseded, or waiting Objective never enters
        # this set and therefore cannot be accidentally reopened.
        failed = [
            item for item in objective_values
            if str(item.get("status") or "").upper() == "FAILED"
            and not _has_unreconciled_execution(item)
        ]
        if task_id:
            failed = [item for item in failed if str(item.get("task_id") or "") == task_id]
        if goal_id:
            failed = [item for item in failed if str(item.get("objective_id") or "") == goal_id]
        if label:
            wanted = label.casefold()
            failed = [
                item for item in failed
                if any(
                    wanted in _normalized_reference(item.get(key)).casefold()
                    for key in ("label", "task_label", "description", "intent")
                    if _normalized_reference(item.get(key))
                )
            ]
        resource_id, resource_kind = _resource_reference(reference)
        if resource_id:
            failed = [
                item for item in failed
                if _failed_objective_owns_resource(item, resource_id, resource_kind)
            ]
        if failed:
            return self._delta_resolution(failed, reason=resolution_reason)

        if any(_has_unreconciled_execution(item) for item in objective_values):
            return NotFound(reason="retry_requires_reconciliation")
        return NotFound(reason="failed_objective_not_found")

    @staticmethod
    def _delta_resolution(
        matches: list[Mapping[str, Any]],
        *,
        reason: str,
        resource_kind: str = "",
        label: str = "",
        resource_id: str = "",
    ) -> TargetResolution:
        candidates: list[TargetCandidate] = []
        for item in matches:
            candidate = _candidate(item)
            if candidate is None:
                continue
            bound_resource_id = ""
            if resource_id and _refs_contain(
                _owner_refs(item), resource_id, resource_kind
            ):
                bound_resource_id = resource_id
            if not bound_resource_id:
                bound_resource_id = _resource_id_for_reference(
                    item,
                    resource_kind=resource_kind,
                    label=label,
                )
            if not bound_resource_id:
                # Terminal mutation Objectives may carry the Task's complete
                # resource index while their per-resource objective owner is
                # the historical predecessor.  If that bounded index has
                # exactly one resource of the requested kind, it is safe to
                # use it for historical-owner deduplication; multiple
                # resources remain unresolved/ambiguous.
                bound_resource_id = _unambiguous_resource_id(item, resource_kind)
            if bound_resource_id:
                candidate.resource_id = bound_resource_id
            candidates.append(candidate)
        candidates = [item for item in candidates if item is not None]
        # A conversation snapshot may contain the original Objective and
        # terminal mutation Objectives that all carry the same verified
        # resource binding.  They are historical owners of one physical
        # resource, not three user-selectable targets.  Collapse only when a
        # concrete resource identity is present and keep task scope in the
        # key; distinct resources or distinct Tasks must remain ambiguous.
        unique: dict[tuple[str, str, str], TargetCandidate] = {}
        for candidate in candidates:
            kind = str(getattr(candidate.kind, "value", candidate.kind) or "").upper()
            resource_id = str(candidate.resource_id or "")
            scope_id = str(candidate.task_id or candidate.identity or "")
            key = (kind, scope_id, resource_id) if resource_id else (
                kind,
                scope_id,
                str(candidate.identity or ""),
            )
            unique.setdefault(key, candidate)
        candidates = list(unique.values())
        if len(candidates) == 1:
            return Resolved(target=candidates[0], candidates=candidates, reason=reason)
        if len(candidates) > 1:
            return Ambiguous(
                candidates=candidates,
                reason=f"{reason}_ambiguous",
            )
        return NotFound(reason=f"{reason}_not_found")

    def resolve(self, command: Any, context: Any | None = None) -> TargetResolution:
        canonical = _canonical_command(command)
        requested = canonical.target
        if requested is None:
            return NotFound(reason="command_has_no_target")

        command_context = CommandContext.from_any(context)
        weak_reference = _is_weak_target_reference(requested)
        candidates = self._candidates(
            command_context,
            requested.kind,
            include_typed=weak_reference,
        )

        # FAILED is a historical Objective reference, not an ordinary target
        # status.  Resolve it through the same narrow helper used by
        # TaskDelta, before explicit-id handling can accidentally bind a
        # completed/current resource or fall back to active candidates.
        if requested.reference_type == TargetReferenceType.FAILED:
            reference = requested.model_dump(mode="python", exclude_none=True)
            # A failed target may be serialized as a DRAFT/POST resource or a
            # TASK/Objective.  Historical Objective candidates are always
            # searched across the bounded context; target kind is not allowed
            # to hide the status-scoped evidence.
            candidates = self._candidates(
                command_context,
                TargetKind.TASK,
                include_typed=True,
            )
            kind = _normalized_reference(reference.get("kind")).upper()
            raw_candidates = [
                dict(candidate.metadata)
                for candidate in candidates
                if isinstance(candidate.metadata, Mapping)
            ]
            # Assembled turn context normally projects a Task with nested
            # Objectives, while TaskDelta resolution receives the flattened
            # Objective view.  Reuse the same status-scoped resolver for both
            # shapes; do not let a resource-only candidate bypass FAILED
            # filtering or fall back to the active Task.
            for task in tuple(raw_candidates):
                task_id_value = str(task.get("task_id") or task.get("id") or "")
                task_resources = list(task.get("resource_index") or ())
                for objective in task.get("objectives") or ():
                    if not isinstance(objective, Mapping):
                        continue
                    objective_id = str(objective.get("objective_id") or objective.get("id") or "")
                    if not objective_id:
                        continue
                    flattened = dict(objective)
                    flattened.setdefault("id", objective_id)
                    flattened.setdefault("objective_id", objective_id)
                    flattened.setdefault("task_id", task_id_value)
                    flattened.setdefault("kind", "TASK")
                    flattened.setdefault(
                        "label",
                        objective.get("description")
                        or objective.get("intent")
                        or task.get("goal")
                        or "",
                    )
                    flattened.setdefault("resource_index", task_resources)
                    raw_candidates.append(flattened)
            explicit_id = _normalized_reference(reference.get("id"))
            objective_ids = {
                str(item.get("objective_id") or "")
                for item in raw_candidates
                if isinstance(item, Mapping)
            }
            goal_id = _normalized_reference(
                reference.get("objective_id")
                or reference.get("target_objective_id")
                or (
                    explicit_id
                    if explicit_id in objective_ids
                    or kind not in {"TASK", "DRAFT", "SCHEDULE", "POST"}
                    else ""
                )
            )
            task_id = _normalized_reference(reference.get("task_id"))
            label = _normalized_reference(
                reference.get("label") or reference.get("reference")
            )
            return self._resolve_failed_objective_retry(
                raw_candidates,
                reference=reference,
                goal_id=goal_id,
                task_id=task_id,
                label=label,
                user_input=str(getattr(canonical, "raw_input", "") or ""),
            )

        candidates = self._apply_operation_scope(
            canonical,
            requested,
            candidates,
            weak_reference=weak_reference,
        )

        explicit = requested.explicit_id
        if explicit:
            matches = [item for item in candidates if item.identity == explicit]
            if len(matches) == 1:
                return self._resolved(matches[0], "explicit_identity")
            # A TASK target may carry a resource id (DraftA/ScheduleA) rather than
            # a Task id; resolve to the Task that owns that resource (e.g. cancel
            # "刚刚那篇" = the Objective whose Draft/Schedule was just touched).
            if requested.kind == TargetKind.TASK:
                raw = list(command_context.targets)
                if command_context.active_target is not None:
                    raw.append(command_context.active_target.model_dump(mode="json"))
                owners = _resource_owner_matches(
                    raw, resource_id=explicit, resource_kind=""
                )
                owner_candidates = [
                    c for c in (_candidate(o) for o in owners) if c is not None
                ]
                if len(owner_candidates) == 1:
                    return self._resolved(owner_candidates[0], "resource_owner")
                if len(owner_candidates) > 1:
                    return Ambiguous(
                        candidates=owner_candidates, reason="resource_owner_ambiguous"
                    )
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
            # ACTIVE is a weak conversational reference, not proof that no
            # target exists.  When the context contains several candidates and
            # no focus/active binding disambiguates them, preserve the full
            # candidate set and require clarification.
            if candidates:
                return self._one_or_many(candidates, "active_context_binding")
            return NotFound(reason="active_target_not_found")

        if requested.reference_type == TargetReferenceType.IDENTIFIER:
            matches = [item for item in candidates if item.identity == requested.reference]
            return self._one_or_many(matches, "structured_identifier")

        if requested.reference_type == TargetReferenceType.ORDINAL:
            ordinal = requested.ordinal
            if ordinal is None:
                return NotFound(reason="ordinal_missing")
            # "第一篇" means creation order, never recency; an edited task must
            # not move in the ordinal sequence.
            ordered = sorted(
                enumerate(candidates),
                key=lambda pair: (
                    _parse_time(pair[1].created_at) or datetime.min.replace(tzinfo=UTC),
                    pair[0],
                ),
            )
            if 1 <= ordinal <= len(ordered):
                return self._resolved(ordered[ordinal - 1][1], "structured_ordinal")
            return NotFound(reason="ordinal_out_of_range")

        filtered = candidates
        if requested.reference_type == TargetReferenceType.PROPERTY:
            filtered = self._property_matches(requested, candidates, command_context.timezone)
        elif requested.reference_type == TargetReferenceType.TEMPORAL:
            filtered = self._temporal_matches(requested, candidates, command_context.timezone)
        elif requested.reference or requested.label:
            filtered = self._text_matches(
                requested.reference or requested.label or "", candidates
            )

        if requested.reference_type == TargetReferenceType.NONE and (
            requested.reference or requested.label
        ):
            # A legacy projection may carry only a semantic reference string.
            # Preserve the safety rule: one candidate is sufficient evidence;
            # multiple candidates require clarification.
            return self._one_or_many(filtered or candidates, "unstructured_reference")

        return self._one_or_many(filtered, "structured_reference")

    @staticmethod
    def _candidates(
        context: CommandContext,
        requested_kind: TargetKind,
        *,
        include_typed: bool = False,
    ) -> list[TargetCandidate]:
        values: list[Any] = list(context.targets)
        # Some callers provide Tasks and typed resources in separate context
        # fields.  Candidate discovery must still expose both to the canonical
        # resolver; it is not allowed to turn a missing ``targets`` projection
        # into NOT_FOUND.
        values.extend(
            {**dict(item), "kind": TargetKind.TASK.value}
            for item in context.active_tasks
            if isinstance(item, Mapping)
        )
        if context.active_target is not None:
            values.append(context.active_target.model_dump(mode="json"))

        result: list[TargetCandidate] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            candidate = _candidate(value)
            if candidate is None:
                continue
            # A weak TASK-kind reference is a neutral kind hint (for example
            # ``刚刚那篇``).  Keep all typed candidates until operation scope
            # and reference evidence filter them.  Strong/typed references
            # remain kind-isolated, preserving (resource_id, resource_kind).
            if not include_typed and candidate.kind != requested_kind:
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
        # Active context is weak evidence.  It may confirm a sole candidate,
        # but it must never collapse a set of equally valid targets.
        if len(candidates) != 1:
            return None
        active = context.active_target
        if active is not None:
            active_candidate = _candidate(active.model_dump(mode="json"))
            if active_candidate is not None:
                for candidate in candidates:
                    if candidate.kind == active_candidate.kind and candidate.identity == active_candidate.identity:
                        return candidate
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _apply_operation_scope(
        command: Command,
        requested: CommandTarget,
        candidates: list[TargetCandidate],
        *,
        weak_reference: bool,
    ) -> list[TargetCandidate]:
        """Filter only by typed domain scope; never rank or pick a target."""

        allowed = _allowed_kinds_for_operation(command)
        if not allowed:
            return candidates
        # An explicit identity is already the strongest evidence.  Keep it in
        # the requested kind and let the normal identity branch decide whether
        # it exists; operation scope must not reinterpret a concrete Task id as
        # a resource id.
        if requested.explicit_id or requested.task_id:
            return candidates
        # A non-TASK kind is already explicit reference evidence.  Do not
        # reinterpret it through a different operation scope here; downstream
        # policy can reject an illegal typed operation, while resolution keeps
        # the user's typed identity visible.  Scope filtering is needed for a
        # neutral/default TASK kind, where it is the only domain evidence.
        if weak_reference:
            return [candidate for candidate in candidates if candidate.kind in allowed]
        return candidates

    @staticmethod
    def _property_matches(
        requested: CommandTarget,
        candidates: list[TargetCandidate],
        timezone: str = "",
    ) -> list[TargetCandidate]:
        property_name = (requested.property or "").strip()
        expected = (requested.value or requested.reference or "").strip()
        if not property_name or not expected:
            return []
        # A deterministic reference may carry a coarse time word ("下午那篇")
        # that only a run_at hour-window can disambiguate.
        if property_name == "run_at":
            window = _time_window(expected)
            if window is None:
                return []
            return [
                item for item in candidates
                if _matches_run_at_window(
                    _candidate_run_at(item.metadata), window, timezone
                )
            ]
        # A topic token ("Java 那篇") matches candidates whose label contains
        # the token; exact equality would miss a distinctive title.
        if property_name == "label":
            normalized = expected.casefold()
            return [
                item for item in candidates
                if normalized and normalized in (item.label or "").casefold()
            ]
        matches: list[TargetCandidate] = []
        for item in candidates:
            actual = getattr(item, property_name, None)
            if actual is not None and str(actual).casefold() == expected.casefold():
                matches.append(item)
        return matches

    @staticmethod
    def _temporal_matches(
        requested: CommandTarget,
        candidates: list[TargetCandidate],
        timezone: str = "",
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


def _is_weak_target_reference(requested: CommandTarget) -> bool:
    """Return whether ``kind=TASK`` is only a neutral kind hint.

    The structured schema historically defaults ``kind`` to TASK.  That
    default must not hide typed Draft/Post/Schedule candidates for a weak
    conversational reference.  Explicit ids and task-relative ids remain
    task-scoped and therefore do not use the broad candidate pool.
    """

    return requested.kind == TargetKind.TASK and not requested.explicit_id


def _allowed_kinds_for_operation(command: Command) -> frozenset[TargetKind] | None:
    operations: list[str] = []
    semantic = str(command.semantic_operation or "").strip().upper()
    if semantic:
        operations.append(semantic)
    for delta in command.task_changes or ():
        desired = getattr(delta, "desired_changes", None) or {}
        if isinstance(desired, Mapping):
            value = str(desired.get("semantic_action") or "").strip().upper()
            if value:
                operations.append(value)
    operations = list(dict.fromkeys(operations))
    scoped = [
        _OPERATION_ALLOWED_KINDS[operation]
        for operation in operations
        if operation in _OPERATION_ALLOWED_KINDS
    ]
    if not scoped:
        return None
    first = scoped[0]
    return first if all(value == first for value in scoped[1:]) else None


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
        # "ARTIFACT" is not a TargetKind.  Derive the kind from the resource
        # type when present; otherwise this is not a resolvable target and
        # must be skipped rather than silently treated as a TASK (which would
        # fabricate duplicate TASK candidates and break single-match binds).
        resource_kind = value.get("resource_type") or value.get("artifact_type") or ""
        try:
            kind = TargetKind(str(resource_kind).upper())
        except (ValueError, KeyError):
            return None
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
        label=_string(
            value.get("label")
            or value.get("semantic_label")
            or value.get("title")
            or value.get("goal")
        ),
        status=_string(value.get("status")),
        created_at=_string(value.get("created_at")),
        updated_at=_string(value.get("updated_at")),
        metadata=dict(value),
    )


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _normalized_reference(value: Any) -> str:
    return " ".join(str(value or "").split())


def _resource_reference(reference: Mapping[str, Any]) -> tuple[str, str]:
    """Extract a strong resource reference without confusing Goal ids.

    ``id`` becomes a business-resource identifier only when an explicit
    resource kind is supplied.  This protects the existing Task/Goal target
    grammar, where a bare ``id`` can legitimately name a Goal.
    """

    kind = _normalized_reference(
        reference.get("resource_kind") or reference.get("kind")
    ).upper()
    field_kinds = (
        ("draft_id", "DRAFT"),
        ("schedule_id", "SCHEDULE"),
        ("post_id", "POST"),
    )
    for field, implied_kind in field_kinds:
        value = _normalized_reference(reference.get(field))
        if value:
            return value, kind or implied_kind
    resource_id = _normalized_reference(reference.get("resource_id"))
    if resource_id:
        return resource_id, kind
    if kind in {"DRAFT", "SCHEDULE", "POST"}:
        return _normalized_reference(reference.get("id")), kind
    return "", ""


def _resource_owner_matches(
    values: Sequence[Mapping[str, Any]],
    *,
    resource_id: str,
    resource_kind: str,
) -> list[Mapping[str, Any]]:
    """Find Task candidates whose durable resource index contains the id.

    The assembled context carries a Task's durable resources as
    ``resource_index`` (each entry has resource_id / resource_kind / task_id);
    ``metadata.resource_refs`` is the same shape used by TaskDelta tests.
    Scan both so one canonical resource->Task ownership lookup works against
    either representation.
    """

    matches: list[Mapping[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if _refs_contain(_owner_refs(value), resource_id, resource_kind):
            matches.append(value)
    return matches


def _is_objective_candidate(value: Mapping[str, Any]) -> bool:
    """Recognize both legacy Goal and canonical Objective projections."""

    return bool(value.get("goal_id") or value.get("objective_id"))


def _owner_refs(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    for source in (
        value.get("resource_index"),
        value.get("resource_refs"),
    ):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            refs.extend(item for item in source if isinstance(item, Mapping))
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        for source in (
            metadata.get("resource_refs"),
            metadata.get("resource_index"),
        ):
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                refs.extend(item for item in source if isinstance(item, Mapping))
    owner_id = _normalized_reference(
        value.get("objective_id") or value.get("goal_id")
    )
    if owner_id:
        owned = [
            ref for ref in refs
            if not _normalized_reference(ref.get("objective_id"))
            or _normalized_reference(ref.get("objective_id")) == owner_id
        ]
        # Some lightweight callers omit per-resource owner metadata.  Keep
        # their existing bounded candidate behavior rather than turning a
        # single objective resource into NOT_FOUND.
        if owned or any(_normalized_reference(ref.get("objective_id")) for ref in refs):
            refs = owned
    return refs


def _refs_contain(
    refs: Sequence[Mapping[str, Any]],
    resource_id: str,
    resource_kind: str,
) -> bool:
    for ref in refs:
        ref_id = _normalized_reference(ref.get("resource_id") or ref.get("id"))
        ref_kind = _normalized_reference(
            ref.get("resource_kind") or ref.get("kind")
        ).upper()
        if ref_id == resource_id and (
            not resource_kind or ref_kind == resource_kind
        ):
            return True
    return False


def is_failed_objective_retry(
    delta: Any,
    reference: Mapping[str, Any],
) -> bool:
    """Recognize the explicit structured marker for a user-triggered retry."""

    source = getattr(delta, "source_reference", None) or {}
    desired = getattr(delta, "desired_changes", None) or {}
    if not isinstance(source, Mapping):
        source = {}
    if not isinstance(desired, Mapping):
        desired = {}
    marker_values = (
        source.get("kind"),
        source.get("type"),
        reference.get("kind"),
        reference.get("reference_type"),
        desired.get("kind"),
    )
    markers = {
        str(value or "").strip().upper()
        for value in marker_values
    }
    return bool(
        source.get("user_triggered_retry")
        or source.get("retry_of_objective_id")
        or desired.get("user_triggered_retry")
        or "FAILED_OBJECTIVE_RETRY" in markers
        or "FAILED_OBJECTIVE" in markers
        or str(reference.get("status") or "").upper() == "FAILED"
    )


def _has_unreconciled_execution(value: Mapping[str, Any]) -> bool:
    """Keep RESULT_UNKNOWN/processing work outside user retry resolution."""

    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    statuses = list(value.get("execution_statuses") or ())
    statuses.extend(metadata.get("execution_statuses") or ())
    status_values = {
        str(item or "").strip().upper()
        for item in statuses
    }
    return bool(status_values.intersection({
        "RESULT_UNKNOWN",
        "VERIFYING_RESULT",
        "WAITING_EXTERNAL",
        "SUBMITTED",
        "QUEUED",
        "RUNNING",
        "PROCESSING",
        "IN_PROGRESS",
    }))


def _failed_objective_owns_resource(
    value: Mapping[str, Any],
    resource_id: str,
    resource_kind: str,
) -> bool:
    """Match a resource only when it belongs to this Objective."""

    objective_id = str(value.get("objective_id") or "")
    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    related_ids = {
        str(item)
        for item in (
            value.get("related_resource_ids")
            or metadata.get("related_resource_ids")
            or ()
        )
        if item
    }
    if str(resource_id) in related_ids:
        return True
    for ref in _owner_refs(value):
        ref_id = _normalized_reference(ref.get("resource_id") or ref.get("id"))
        ref_kind = _normalized_reference(
            ref.get("resource_kind") or ref.get("kind")
        ).upper()
        ref_owner = str(ref.get("objective_id") or "")
        if ref_id == str(resource_id) and (
            not resource_kind or ref_kind == resource_kind
        ) and (not ref_owner or ref_owner == objective_id):
            return True
    return False


def _unique_label_matches(
    values: Sequence[Mapping[str, Any]],
    label: str,
) -> list[Mapping[str, Any]]:
    normalized = _normalized_label(label).casefold()
    exact = [
        item for item in values
        if _normalized_reference(
            item.get("label") or item.get("goal") or item.get("description")
        ).casefold() == normalized.casefold()
    ]
    if exact:
        return exact
    return [
        item for item in values
        if normalized
        and normalized in _normalized_reference(
            item.get("label") or item.get("goal") or item.get("description")
        ).casefold()
    ]


# Conversational suffixes a model may append to a citation label
# ("Java 那篇", "MySQL 那篇文章") that never appear in the goal text.
_REFERENCE_NOISE = (
    "那篇", "这篇", "那个", "那个任务", "那篇文章", "这篇文章",
    "的帖子", "帖子", "草稿", "发布计划", "排程", "定时任务",
    "内容", "任务", "的",
)


def _normalized_label(label: str) -> str:
    """Strip conversational noise so a citation matches the goal text.

    "Java 那篇" -> "Java"; "为什么学了很" stays intact (real content).
    Only suffixes that are pure referencing particles are removed; content
    words are never touched.
    """
    text = " ".join((label or "").split())
    changed = True
    while changed and text:
        changed = False
        lowered = text.casefold()
        for noise in _REFERENCE_NOISE:
            if lowered.endswith(noise.casefold()):
                text = text[: -len(noise)].strip()
                changed = True
                break
    return text


_GROUNDING_TOKEN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}"
)


def _grounding_tokens(value: Any) -> set[str]:
    """Return language-neutral identity tokens from one semantic label."""

    return {
        token.casefold()
        for token in _GROUNDING_TOKEN.findall(str(value or ""))
        if token.strip()
    }


def _reference_evidence_tokens(value: Any) -> set[str]:
    """Return exact and bounded CJK spans usable as reference evidence.

    A provider label may contain a longer business description than the
    user's natural reference (for example ``消息队列可靠性实践`` versus
    ``消息队列草稿``). Treating a whole CJK run as one token loses that
    grounded subject. Bounded bigrams recover only shared spans; candidate
    frequency and the existing 0/1/N boundary still decide whether they are
    identity evidence.
    """

    text = str(value or "")
    tokens = _grounding_tokens(text)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.update(
            run[index : index + 2].casefold()
            for index in range(len(run) - 1)
        )
    return tokens


def _candidate_grounding_labels(value: Mapping[str, Any]) -> set[str]:
    return {
        label
        for key in ("label", "task_label", "semantic_label", "title", "goal", "description", "intent")
        for label in _reference_evidence_tokens(value.get(key))
    }


def _reference_grounding_candidates(
    reference: Mapping[str, Any],
    *,
    user_input: str,
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return candidates supported by current-turn evidence, if any.

    The helper deliberately returns an empty list for an unsupported provider
    identity.  The caller then removes that identity and lets the normal
    0/1/>1 cardinality boundary decide.  It never ranks candidates.
    """

    raw = " ".join(str(user_input or "").split()).casefold()
    values = [item for item in candidates if isinstance(item, Mapping)]
    if not raw:
        return values

    explicit_values = (
        reference.get("objective_id"),
        reference.get("target_objective_id"),
        reference.get("task_id"),
        reference.get("resource_id"),
        reference.get("draft_id"),
        reference.get("schedule_id"),
        reference.get("post_id"),
        reference.get("id"),
    )
    explicit = [
        str(value).strip().casefold()
        for value in explicit_values
        if str(value or "").strip()
    ]
    if explicit:
        supported = {
            value
            for value in explicit
            if value in raw
        }
        return [
            item
            for item in values
            if any(
                str(item.get(key) or "").strip().casefold() in supported
                for key in (
                    "objective_id",
                    "target_objective_id",
                    "task_id",
                    "resource_id",
                    "draft_id",
                    "schedule_id",
                    "post_id",
                    "id",
                )
            )
        ]

    label = _normalized_label(
        str(
            reference.get("label")
            or reference.get("reference")
            or reference.get("description")
            or reference.get("name")
            or ""
        )
    )
    if not label:
        # reference_type=FAILED alone is the intended generic scope.
        return values
    normalized_label = label.casefold()
    if normalized_label in raw:
        exact = [
            item
            for item in values
            if any(
                normalized_label in _normalized_reference(item.get(key)).casefold()
                for key in ("label", "task_label", "semantic_label", "title", "goal", "description", "intent")
            )
        ]
        if exact:
            return exact

    provider_tokens = _reference_evidence_tokens(label)
    if not provider_tokens:
        return []
    token_frequency: dict[str, int] = {}
    candidate_tokens: dict[int, set[str]] = {}
    for index, candidate in enumerate(values):
        tokens = _candidate_grounding_labels(candidate)
        candidate_tokens[index] = tokens
        for token in tokens:
            token_frequency[token] = token_frequency.get(token, 0) + 1

    # A token shared by all failed candidates (for example a common topic or
    # status word) is not identity evidence.  A token that distinguishes the
    # provider's candidate and appears in the user's turn is.
    discriminators = {
        token
        for token in provider_tokens
        if token_frequency.get(token, 0) == 1
    }
    grounded = {token for token in discriminators if token in raw}
    if not grounded:
        return []
    return [
        values[index]
        for index, tokens in candidate_tokens.items()
        if tokens.intersection(grounded)
    ]


def _candidate_has_resource_kind(
    value: Mapping[str, Any],
    resource_kind: str,
) -> bool:
    """Return whether a task owns at least one resource of ``resource_kind``.

    TaskDelta resolution receives Task/Objective candidates rather than the
    raw resource collection. The existing resource index is the bounded
    ownership evidence for a typed generic reference such as "那篇草稿".
    """

    wanted = _normalized_reference(resource_kind).upper()
    if wanted not in {"DRAFT", "SCHEDULE", "POST"}:
        return True
    direct = _normalized_reference(
        value.get("resource_kind") or value.get("resource_type")
    ).upper()
    if direct == wanted:
        return True
    refs = _owner_refs(value)
    return any(
        isinstance(ref, Mapping)
        and _normalized_reference(
            ref.get("resource_kind") or ref.get("resource_type") or ref.get("kind")
        ).upper() == wanted
        for ref in refs
    )


def _resource_label_matches(
    values: Sequence[Mapping[str, Any]],
    label: str,
    resource_kind: str,
    *,
    user_input: str = "",
) -> list[Mapping[str, Any]]:
    """Match a typed resource reference against its owner's resource index.

    Goal candidates intentionally carry the Objective label at the top level,
    while the user-visible title of an existing Draft/Schedule/Post lives in
    the durable resource index.  Resource-scoped follow-up mutations must use
    that verified resource label without treating the label as a canonical id.
    """

    wanted = _normalized_reference(resource_kind).upper()
    normalized = _normalized_label(label).casefold()
    if not wanted or not normalized:
        return []
    requested_tokens = _grounding_tokens(normalized)
    matches: list[Mapping[str, Any]] = []
    evidence_tokens_by_value: list[set[str]] = []
    evidence_values: list[Mapping[str, Any]] = []
    owner_keys_by_value: list[set[tuple[str, str]]] = []
    for value in values:
        owner_refs = _owner_refs(value)
        typed_owner_refs = [
            ref
            for ref in owner_refs
            if _normalized_reference(
                ref.get("resource_kind")
                or ref.get("resource_type")
                or ref.get("kind")
            ).upper() == wanted
        ]
        # An Objective projection can inherit the Task's complete resource
        # index.  For a typed mutation, only its own ResourceRefs are valid;
        # a sibling Objective that merely shares the Task must not become a
        # second target because it happens to mention the same resource kind.
        if not typed_owner_refs:
            continue
        owner_task_id = _normalized_reference(
            value.get("task_id") or value.get("id")
        )
        owner_keys = {
            (
                owner_task_id,
                _normalized_reference(ref.get("resource_id") or ref.get("id")),
            )
            for ref in typed_owner_refs
            if _normalized_reference(ref.get("resource_id") or ref.get("id"))
        }
        if not owner_keys:
            owner_keys = {(owner_task_id, "")}
        resource_display_values = [
            _normalized_reference(
                ref.get("label") or ref.get("title") or ref.get("name")
            )
            for ref in typed_owner_refs
            if _normalized_reference(
                ref.get("label") or ref.get("title") or ref.get("name")
            )
        ]
        resource_match = False
        for ref in typed_owner_refs:
            if (
                not isinstance(ref, Mapping)
                or _normalized_reference(
                    ref.get("resource_kind")
                    or ref.get("resource_type")
                    or ref.get("kind")
                ).upper() != wanted
            ):
                continue
            resource_label = _normalized_reference(
                ref.get("label") or ref.get("title") or ref.get("name")
            ).casefold()
            if resource_label and (
                resource_label == normalized or normalized in resource_label
            ):
                resource_match = True
                break
        if resource_match:
            evidence_tokens_by_value.append(
                _reference_evidence_tokens(" ".join(resource_display_values))
            )
            evidence_values.append(value)
            owner_keys_by_value.append(owner_keys)
            matches.append(value)
            continue

        # A Task/Objective may own a real resource whose current display
        # title is generated and does not contain the semantic label used by
        # the follow-up request (for example, a Java draft titled with a
        # generated topic).  The durable Objective constraints still carry
        # the bounded user-intent evidence.  Use only those semantic fields
        # to find the owner; the concrete resource identity remains sourced
        # from the owner's resource index below.
        # Resource display text is primary, while the Objective's structured
        # constraints remain bounded semantic evidence when a provider title
        # is generated and no longer contains the user's topic.  Keep both
        # sources; frequency/discriminator checks below still enforce the
        # existing 0/1/N ambiguity boundary.
        evidence: list[str] = list(resource_display_values)
        for key in ("label", "task_label", "semantic_label", "title", "goal", "description", "intent"):
            candidate = value.get(key)
            if candidate:
                evidence.append(str(candidate))
        constraints = value.get("constraints")
        if isinstance(constraints, Mapping):
            requirements = constraints.get("requirements")
            if isinstance(requirements, Sequence) and not isinstance(requirements, (str, bytes)):
                evidence.extend(str(item) for item in requirements if item)
            elif requirements:
                evidence.append(str(requirements))
            for key in ("title", "topic", "subject", "intent"):
                candidate = constraints.get(key)
                if candidate:
                    evidence.append(str(candidate))
        metadata = value.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("label", "task_label", "semantic_label", "title", "goal", "description", "intent"):
                candidate = metadata.get(key)
                if candidate:
                    evidence.append(str(candidate))
        evidence_tokens_by_value.append(_reference_evidence_tokens(" ".join(evidence)))
        evidence_values.append(value)
        owner_keys_by_value.append(owner_keys)
    token_frequency: dict[str, int] = {}
    for evidence_tokens in evidence_tokens_by_value:
        for token in evidence_tokens:
            token_frequency[token] = token_frequency.get(token, 0) + 1
    token_owner_keys: dict[str, set[tuple[str, str]]] = {}
    for evidence_tokens, owner_keys in zip(
        evidence_tokens_by_value,
        owner_keys_by_value,
    ):
        for token in evidence_tokens:
            token_owner_keys.setdefault(token, set()).update(owner_keys)
    for value, evidence_tokens in zip(evidence_values, evidence_tokens_by_value):
        overlap = requested_tokens.intersection(evidence_tokens)
        if not overlap:
            continue
        grounded_requested = requested_tokens.intersection(
            _reference_evidence_tokens(user_input)
        )
        unique_grounded = {
            token
            for token in grounded_requested
            if len(token_owner_keys.get(token, set())) == 1
        }
        if unique_grounded:
            if not overlap.intersection(unique_grounded):
                continue
            matches.append(value)
            continue
        # A token shared by every owner (for example ``article`` or Java in
        # two Java drafts) is valid ambiguity evidence.  A token unique to an
        # owner is the discriminator and must be present when one exists, so
        # ``Java article`` cannot also bind an ``Agent article`` owner while a
        # plain ``Java`` reference still yields both Java candidates.
        discriminators = {
            token for token in requested_tokens
            if token_frequency.get(token, 0) == 1
        }
        if not discriminators or overlap.intersection(discriminators):
            matches.append(value)
    return matches


def _resource_id_for_reference(
    value: Mapping[str, Any],
    *,
    resource_kind: str,
    label: str,
) -> str:
    """Return the one owned resource selected by a typed semantic reference.

    The resolver returns the Objective/Task owner so lifecycle projection can
    remain owner-scoped.  Mutation execution additionally needs the concrete
    business resource id.  Derive it only from the same bounded resource
    index used for resolution; never use recency or an arbitrary first item.
    """

    wanted = _normalized_reference(resource_kind).upper()
    if wanted not in {"DRAFT", "SCHEDULE", "POST"}:
        return ""
    refs: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in _owner_refs(value):
        kind = _normalized_reference(
            ref.get("resource_kind")
            or ref.get("resource_type")
            or ref.get("kind")
        ).upper()
        if kind != wanted:
            continue
        resource_id = _normalized_reference(ref.get("resource_id") or ref.get("id"))
        key = (kind, resource_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    if not refs:
        return ""
    normalized = _normalized_label(label).casefold()
    if normalized:
        matching_refs = [
            ref for ref in refs
            if (
                (resource_label := _normalized_reference(
                    ref.get("label") or ref.get("title") or ref.get("name")
                ).casefold())
                and (resource_label == normalized or normalized in resource_label)
            )
        ]
        if len(matching_refs) == 1:
            return _normalized_reference(
                matching_refs[0].get("resource_id") or matching_refs[0].get("id")
            )
    if len(refs) == 1:
        return _normalized_reference(refs[0].get("resource_id") or refs[0].get("id"))
    return ""


def _unambiguous_resource_id(
    value: Mapping[str, Any],
    resource_kind: str,
) -> str:
    """Return a sole typed resource id from the full bounded resource index."""

    wanted = _normalized_reference(resource_kind).upper()
    if wanted not in {"DRAFT", "SCHEDULE", "POST"}:
        return ""
    refs: list[Any] = []
    for key in ("resource_index", "resource_refs", "resources"):
        source = value.get(key)
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            refs.extend(source)
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("resource_index", "resource_refs"):
            source = metadata.get(key)
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                refs.extend(source)
    ids = {
        _normalized_reference(ref.get("resource_id") or ref.get("id"))
        for ref in refs
        if isinstance(ref, Mapping)
        and _normalized_reference(
            ref.get("resource_kind") or ref.get("resource_type") or ref.get("kind")
        ).upper() == wanted
        and _normalized_reference(ref.get("resource_id") or ref.get("id"))
    }
    return next(iter(ids)) if len(ids) == 1 else ""


# ── deterministic multi-turn reference resolution ──────────────────────────
# Covers the conversational citations users actually use in a follow-up turn:
#   * "Java 那篇"            — label substring (unique only)
#   * "第三篇" / "第一篇"     — creation ordinal
#   * "下午那篇"              — publication time window
#   * "刚刚那篇" / "最新那篇"  — persisted conversation focus, never DB recency
# The model may express these as ordinal/temporal fields or as a free-text
# label; both are normalized here.  Nothing is ever selected arbitrarily:
# the resolution stays three-state (one / ambiguous / none).

_CN_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _parse_ordinal_label(label: str) -> int | None:
    """Parse "第三篇", "第3篇", "第 2 个", "第一篇" into 1-based ordinal."""
    text = (label or "").strip().casefold()
    if not text:
        return None
    import re

    match = re.search(r"第\s*([0-9０-９]+|[一二两三四五六七八九十])\s*(?:篇|个|条|项|个任务|任务)?", text)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit() or all("0" <= ch <= "9" for ch in token):
        try:
            return int(token)
        except ValueError:
            return None
    if token in _CN_NUMERALS:
        return _CN_NUMERALS[token]
    return None


def _is_recent_label(label: str) -> bool:
    text = (label or "").strip().casefold()
    return any(
        word in text
        for word in (
            "刚刚", "刚才", "最新", "最后", "最近", "刚",
            "just now", "latest", "most recent", "recent",
        )
    )


def _time_window(label: str) -> tuple[int, int] | None:
    """Map a coarse time word to a run_at hour window, e.g. 下午 -> (12,18)."""
    text = (label or "").strip().casefold()
    if "晚上" in text or "傍晚" in text:
        return 18, 24
    if "下午" in text:
        return 12, 18
    if "上午" in text or "早上" in text:
        return 6, 12
    if "凌晨" in text:
        return 0, 6
    if "中午" in text:
        return 11, 13
    return None


def _candidate_run_at(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("run_at"):
        return str(metadata["run_at"])
    constraints = value.get("constraints")
    if isinstance(constraints, Mapping) and constraints.get("run_at"):
        return str(constraints["run_at"])
    if isinstance(constraints, list):
        for item in constraints:
            if isinstance(item, Mapping) and item.get("run_at"):
                return str(item["run_at"])
    return str(value.get("run_at") or "")


def _candidate_created(value: Mapping[str, Any]) -> str:
    return str(value.get("created_at") or value.get("metadata", {}).get("created_at") or "")


def _matches_run_at_window(run_at: str, window: tuple[int, int], timezone: str = "") -> bool:
    parsed = _parse_time(run_at)
    if parsed is None or not isinstance(window, tuple):
        return False
    if timezone:
        try:
            local = parsed.astimezone(ZoneInfo(timezone))
        except (KeyError, ValueError, TypeError):
            local = parsed
    else:
        local = parsed
    return window[0] <= local.hour < window[1]


def _resolve_goal_by_reference(
    values: Sequence[Mapping[str, Any]],
    label: str,
    reference: Mapping[str, Any],
    reference_type: str,
) -> list[Mapping[str, Any]]:
    ordered_values = list(values)
    if _is_recent_label(label) or reference_type in {"ACTIVE", "RECENT", "LATEST"}:
        # A weak reference without focus is safe only when the candidate set
        # itself is unique.  Conversation focus is only diagnostic evidence;
        # it must not collapse an ambiguous candidate set into a selection.
        # Returning all equally valid candidates lets the caller produce
        # AMBIGUOUS and retain the set; returning [] would incorrectly
        # collapse ambiguity into NOT_FOUND.
        return ordered_values
    ordinal = _parse_ordinal_label(label) or _as_int(reference.get("ordinal"))
    if ordinal is not None:
        # Ordinal means creation order ("第三篇"), never recency: a task that
        # was edited later must not move in the ordinal sequence.  Tie-break
        # by the stable input index (repository order).
        ordered = sorted(
            enumerate(ordered_values),
            key=lambda pair: (
                _parse_time(_candidate_created(pair[1])) or datetime.min.replace(tzinfo=UTC),
                pair[0],
            ),
        )
        if 1 <= ordinal <= len(ordered):
            return [ordered[ordinal - 1][1]]
        return []
    window = _time_window(label)
    if window is not None:
        return [
            item for item in values
            if _matches_run_at_window(_candidate_run_at(item), window)
        ]
    return _unique_label_matches(values, label)


def _resolve_task_by_reference(
    values: Sequence[Mapping[str, Any]],
    label: str,
    reference: Mapping[str, Any],
    reference_type: str,
    *,
    resource_kind: str = "",
    active_task_id: str = "",
    conversation_focus_task_id: str = "",
) -> list[Mapping[str, Any]]:
    if not values:
        return []
    # ``ACTIVE`` is a weak conversational cue.  It must not override an
    # explicit label such as "Java 那篇" merely because an upstream model also
    # classified the turn as active-context.  Explicit title/topic/ordinal
    # resolution below remains higher priority.
    ordinal = _parse_ordinal_label(label) or _as_int(reference.get("ordinal"))
    if ordinal is not None:
        # Ordinal means creation order ("第一篇"), never recency.
        ordered = sorted(
            enumerate(values),
            key=lambda pair: (
                _parse_time(_candidate_created(pair[1])) or datetime.min.replace(tzinfo=UTC),
                pair[0],
            ),
        )
        if 1 <= ordinal <= len(ordered):
            return [ordered[ordinal - 1][1]]
        return []
    if _is_recent_label(label) or reference_type in {"ACTIVE", "RECENT", "LATEST"}:
        # Multiple weak candidates must become AMBIGUOUS, not NOT_FOUND and
        # never a newest-updated-resource or active-task guess.
        if resource_kind:
            return [
                item for item in values
                if _candidate_has_resource_kind(item, resource_kind)
            ]
        return list(values)
    window = _time_window(label)
    if window is not None:
        return [
            item for item in values
            if _matches_run_at_window(_candidate_run_at(item), window)
        ]
    if label:
        normalized = _normalized_label(label)
        if resource_kind:
            values = [
                item for item in values
                if _candidate_has_resource_kind(item, resource_kind)
            ]
            # A kind-only conversational reference such as "那篇草稿" is
            # evidence for the resource class, not a title. Preserve every
            # candidate of that class so the three-state boundary can return
            # RESOLVED / AMBIGUOUS / NOT_FOUND without recency fallback.
            if not normalized:
                return list(values)
        matches = _unique_label_matches(values, label)
        if len(matches) == 1:
            return matches
        if not matches:
            # One more attempt: the model may cite a goal-level description
            # while the candidate list carries task-level goals.
            for item in values:
                goals = item.get("goals") if isinstance(item.get("goals"), list) else []
                if not goals:
                    goals = item.get("objectives") if isinstance(item.get("objectives"), list) else []
                if any(
                    normalized and normalized in _normalized_reference(
                        str(g.get("description") or g.get("label") or g.get("intent") or "")
                    ).casefold()
                    for g in goals
                ):
                    return [item]
        return matches
    return list(values) if len(values) == 1 else []


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
