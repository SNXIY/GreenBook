"""Fast Path routing gate.

Decides FAST / QUERY / CHAT / CLARIFY / COMPLEX for one validated Command.
All decisions are made from structured Command / TaskDelta / SemanticAction /
TargetResolution results.  No user text is matched against keywords, so there
is no ``if "取消" ...``-style rule: the model must have already expressed the
action as a canonical semantic action or capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..command.models import Command, CommandType, TaskDelta
from ..command.models import ResolvedSemanticState
from ..command.target import (
    TargetResolution,
    TargetResolutionStatus,
)
from .models import FastPathDecision, TurnRoute

# Canonical explicit writes that need no Agent reasoning (still durable-verified).
FAST_WRITE_ACTIONS = frozenset({
    "UPDATE_DRAFT",
    "DELETE_DRAFT",
    "DELETE_POST",
    "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
})

# Canonical single reads.
FAST_READ_ACTIONS = frozenset({
    "GET_DRAFT",
    "LIST_DRAFTS",
    "GET_POST",
    "LIST_OWN_POSTS",
    "GET_SCHEDULE",
    "LIST_COMMENTS",
    "GET_POST_PERFORMANCE",
    "GET_ACCOUNT_SUMMARY",
})

# Capability name -> canonical semantic action (reverse of CapabilityRegistry
# semantic_mapping).  Used to recognize actions the model emitted only as
# required capabilities.
_CAPABILITY_TO_ACTION: dict[str, str] = {
    # The provider's public capability vocabulary uses SEARCH_COMMUNITY while
    # the ActionLoop's canonical semantic action is SEARCH_POSTS.  Preserve
    # the capability-only QUERY envelope when the provider omits the more
    # specific semantic_operation marker; otherwise the gate falls through to
    # no_action_chat and never admits the read.
    "SEARCH_COMMUNITY": "SEARCH_POSTS",
    "MANAGE_DRAFT": "UPDATE_DRAFT",
    "DELETE_DRAFT": "DELETE_DRAFT",
    "DELETE_POST": "DELETE_POST",
    "MANAGE_SCHEDULE": "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE": "CANCEL_SCHEDULE",
    "PUBLISH_NOW": "PUBLISH_NOW",
    "GET_DRAFT": "GET_DRAFT",
    "LIST_DRAFTS": "LIST_DRAFTS",
    "GET_POST_DETAIL": "GET_POST",
    "LIST_OWN_POSTS": "LIST_OWN_POSTS",
    "GET_SCHEDULE_STATUS": "GET_SCHEDULE",
    "LIST_COMMENTS": "LIST_COMMENTS",
    "ANALYZE_PERFORMANCE": "ANALYZE_PERFORMANCE",
}

# Semantic actions that, on their own, still require dynamic reasoning/replan
# and therefore must stay on the Complex Path.
_NON_FAST_ACTIONS = frozenset({
    "SEARCH_POSTS",
    "CREATE_DRAFT",
    "CREATE_SCHEDULE",
    "REPLY_COMMENT",
    "ANALYZE_CONTENT_PATTERNS",
    "VALIDATE_QUALITY",
})

_SCHEDULE_ACTIONS = frozenset({
    "UPDATE_SCHEDULE",
})

_TARGETED_ACTIONS = frozenset({
    "UPDATE_DRAFT",
    "DELETE_DRAFT",
    "DELETE_POST",
    "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
    "GET_DRAFT",
    "GET_POST",
    "GET_SCHEDULE",
    "LIST_COMMENTS",
    "GET_POST_PERFORMANCE",
})

_NON_ACTIONABLE_QUERY_OPERATIONS = frozenset({
    "",
    "QUERY",
    "CHAT",
    "CONFIRM",
})


def is_non_actionable_query(
    command: Command,
    *,
    semantic_state: ResolvedSemanticState | None = None,
) -> bool:
    """Return whether structured evidence contains no actionable request.

    Providers sometimes mark a conversational acknowledgement as ambiguous
    even though the same structured envelope contains no operation,
    capability, target, reference, or mutation.  That flag is candidate
    evidence, not permission to create a durable human wait.  Keep this
    boundary entirely structural: an actionable clause remains actionable.
    """

    if command.type != CommandType.QUERY or command.is_broad_destructive:
        return False
    if command.target is not None or command.references:
        return False
    if command.required_capabilities or command.parameters or command.constraints:
        return False
    if str(command.semantic_operation or "").strip().upper() not in _NON_ACTIONABLE_QUERY_OPERATIONS:
        return False
    for delta in command.task_changes or ():
        operation = str(getattr(delta, "operation", "") or "").strip().upper()
        desired = getattr(delta, "desired_changes", None) or {}
        target_reference = getattr(delta, "target_reference", None) or {}
        if operation == "NO_CHANGE" and not desired and not target_reference:
            continue
        return False
    if command.items:
        return False
    if semantic_state is not None:
        if semantic_state.capabilities or semantic_state.items:
            return False
        if str(semantic_state.semantic_operation or "").strip().upper() not in _NON_ACTIONABLE_QUERY_OPERATIONS:
            return False
        if (
            semantic_state.target_reference
            or semantic_state.resolved_target
            or semantic_state.target_candidates
            or semantic_state.dependencies
            or semantic_state.publication_intent
        ):
            return False
    return True


class FastPathGate:
    """Route one validated Command to a Fast Path or the Complex Path."""

    def decide(
        self,
        command: Command,
        *,
        target_resolution: TargetResolution | None = None,
        run_at: str | None = None,
        semantic_state: ResolvedSemanticState | None = None,
    ) -> FastPathDecision:
        """Return the routing decision for a validated Command."""

        if command.is_broad_destructive:
            return self._decision(
                TurnRoute.COMPLEX,
                reason="broad_destructive_requires_complex_path",
            )

        # Provider clarification flags are evidence only.  When the complete
        # structured request is non-actionable, conversational closure belongs
        # on the CHAT path and must not create a WAITING_USER state.
        if is_non_actionable_query(command, semantic_state=semantic_state):
            return self._decision(TurnRoute.CHAT, reason="no_action_chat")

        if semantic_state is not None and semantic_state.clarification_required:
            return self._decision(
                TurnRoute.CLARIFY,
                reason=semantic_state.clarification_reason or "needs_clarification",
            )
        if command.needs_clarification:
            return self._decision(TurnRoute.CLARIFY, reason="needs_clarification")

        # Structural multi-target/task pipelines are never eligible for a
        # single-action shortcut, even when the model emitted only one
        # aggregate capability.  The business-item/task shape is authoritative
        # here; no language-specific phrase detection is needed.
        create_tasks = sum(
            1 for delta in (command.task_changes or ())
            if str(getattr(delta, "operation", "")).upper() == "CREATE_TASK"
        )
        item_count = len(command.items or ())
        # Providers may preserve multiple business deliverables in the
        # structured entity envelope while returning one aggregate item.  A
        # single-action shortcut would execute only the first Objective and
        # leave the remaining Objectives pending.  Entity cardinality is
        # already structured model output, so this guard does not inspect raw
        # user text or introduce another planner.
        entity_collections = (
            value for value in (command.entities or {}).values()
            if isinstance(value, (list, tuple))
        )
        has_multiple_entities = any(len(values) > 1 for values in entity_collections)
        if create_tasks > 1 or item_count > 1 or has_multiple_entities or any(
            len(getattr(item, "capabilities", ()) or ()) > 1
            for item in (command.items or ())
        ):
            return self._decision(
                TurnRoute.COMPLEX,
                reason="structured_multi_objective_request",
            )

        if command.requires_target and not self._target_ok(command, target_resolution):
            return self._decision(
                TurnRoute.CLARIFY,
                reason=self._target_reason(target_resolution),
            )

        actions = self._extract_actions(command, semantic_state=semantic_state)
        if (
            command.type == CommandType.CREATE
            and "GENERATE_CONTENT" in actions
            and not self._has_explicit_content_request(command, semantic_state)
        ):
            # A provider can over-classify an ordinary knowledge/advice
            # request as content generation.  A physical Draft requires a
            # structured creation commitment (title or publication/draft
            # intent); topic-only evidence is not sufficient authorization for
            # a write.  Keep the request on the existing chat path until the
            # semantic contract contains that commitment.
            return self._decision(
                TurnRoute.CHAT,
                reason="unqualified_content_request",
            )
        if not actions:
            if command.type == CommandType.QUERY:
                return self._decision(TurnRoute.CHAT, reason="no_action_chat")
            return self._decision(TurnRoute.COMPLEX, reason="no_action_but_mutation")

        # Multiple desired-state mutations span independent Tasks or goals;
        # each must keep its own verification, so they stay on the Complex Path.
        if len(command.task_changes or []) > 1:
            return self._decision(
                TurnRoute.COMPLEX,
                semantic_actions=sorted(actions),
                reason="multiple_task_deltas",
            )
        if len(command.required_capabilities or []) > 1:
            return self._decision(
                TurnRoute.COMPLEX,
                semantic_actions=sorted(actions),
                reason="multiple_required_capabilities",
            )

        if len(actions) > 1:
            return self._decision(
                TurnRoute.COMPLEX,
                semantic_actions=sorted(actions),
                reason="multiple_semantic_actions",
            )

        action = next(iter(actions))
        if action in FAST_WRITE_ACTIONS:
            if command.type not in {CommandType.MODIFY, CommandType.CANCEL}:
                return self._decision(
                    TurnRoute.COMPLEX,
                    semantic_actions=[action],
                    reason="write_action_without_modify_command",
                )
            if action in _TARGETED_ACTIONS and not self._target_ok(command, target_resolution):
                return self._decision(
                    TurnRoute.CLARIFY,
                    semantic_actions=[action],
                    reason=self._target_reason(target_resolution),
                )
            if action in _SCHEDULE_ACTIONS and not run_at:
                return self._decision(
                    TurnRoute.CLARIFY,
                    semantic_actions=[action],
                    reason="schedule_time_unresolved",
                )
            if not self._params_complete(command, action):
                return self._decision(
                    TurnRoute.CLARIFY,
                    semantic_actions=[action],
                    reason="write_parameters_incomplete",
                )
            if self._has_artifact_dependency(command):
                return self._decision(
                    TurnRoute.COMPLEX,
                    semantic_actions=[action],
                    reason="artifact_dependency_requires_complex_path",
                )
            return self._decision(
                TurnRoute.FAST,
                semantic_actions=[action],
                reason="single_explicit_write",
            )

        if action in FAST_READ_ACTIONS:
            if command.type != CommandType.QUERY:
                return self._decision(
                    TurnRoute.COMPLEX,
                    semantic_actions=[action],
                    reason="read_action_without_query_command",
                )
            if action in _TARGETED_ACTIONS and not self._target_ok(command, target_resolution):
                return self._decision(
                    TurnRoute.CLARIFY,
                    semantic_actions=[action],
                    reason=self._target_reason(target_resolution),
                )
            return self._decision(
                TurnRoute.QUERY,
                semantic_actions=[action],
                reason="single_read",
            )

        # Any other semantic action (search, create-draft, schedule-create,
        # reply, analyze, multi-step pipeline) stays on the Complex Path.
        return self._decision(
            TurnRoute.COMPLEX,
            semantic_actions=[action],
            reason="action_requires_reasoning",
        )

    # ── structured action extraction ────────────────────────────────

    @staticmethod
    def _extract_actions(
        command: Command,
        *,
        semantic_state: ResolvedSemanticState | None = None,
    ) -> set[str]:
        """Collect canonical semantic actions from structured output only."""

        if semantic_state is not None:
            actions: set[str] = set()
            operation = str(semantic_state.semantic_operation or "").strip().upper()
            if operation and operation not in {"QUERY", "CHAT"} and (
                command.type != CommandType.QUERY
                or operation in FAST_READ_ACTIONS | _NON_FAST_ACTIONS
            ):
                actions.add(operation)
            for capability in semantic_state.capabilities:
                capability_name = str(capability).strip().upper()
                # SEARCH_COMMUNITY is an explicit read only when the
                # structured provider operation says SEARCH (or the already
                # canonical SEARCH_POSTS action).  A capability-only QUERY
                # fixture is still ordinary chat; mapping it here would
                # bypass the coordinator's no-action chat invariant.
                mapped = _CAPABILITY_TO_ACTION.get(capability_name)
                if capability_name == "SEARCH_COMMUNITY" and operation not in {
                    "SEARCH",
                    "SEARCH_POSTS",
                }:
                    mapped = None
                if mapped:
                    actions.add(mapped)
                elif capability_name in FAST_WRITE_ACTIONS | FAST_READ_ACTIONS:
                    actions.add(capability_name)
            for item in semantic_state.items:
                for capability in item.capabilities:
                    capability_name = str(capability).strip().upper()
                    mapped = _CAPABILITY_TO_ACTION.get(capability_name)
                    if capability_name == "SEARCH_COMMUNITY" and operation not in {
                        "SEARCH",
                        "SEARCH_POSTS",
                    }:
                        mapped = None
                    if mapped:
                        actions.add(mapped)
            return actions

        actions: set[str] = set()
        operation = str(command.semantic_operation or "").strip().upper()
        if operation and operation not in {"QUERY", "CHAT"} and (
            command.type != CommandType.QUERY
            or operation in FAST_READ_ACTIONS | _NON_FAST_ACTIONS
        ):
            actions.add(operation)
        for delta in command.task_changes or ():
            for action in FastPathGate._delta_actions(delta):
                actions.add(action)
        for capability in command.required_capabilities or ():
            capability_name = str(capability).strip().upper()
            mapped = _CAPABILITY_TO_ACTION.get(capability_name)
            if capability_name == "SEARCH_COMMUNITY" and operation not in {
                "SEARCH",
                "SEARCH_POSTS",
            }:
                mapped = None
            if mapped:
                actions.add(mapped)
        # A required capability that is itself an action name is evidence too.
        for capability in command.required_capabilities or ():
            name = str(capability).strip().upper()
            if name in FAST_WRITE_ACTIONS or name in FAST_READ_ACTIONS:
                actions.add(name)
        return actions

    @staticmethod
    def _has_explicit_content_request(
        command: Command,
        semantic_state: ResolvedSemanticState | None,
    ) -> bool:
        """Return whether structured facts authorize creating a Draft.

        This is intentionally a contract check, not a user-text classifier.
        A title or an explicit publication/draft intent is durable evidence of
        a requested business artifact; a topic-only GENERATE_CONTENT marker is
        also emitted for ordinary advice questions and must not cross the write
        boundary.
        """

        def has_intent(value: Any) -> bool:
            if not isinstance(value, Mapping):
                return False
            return bool(
                str(
                    value.get("publication_intent")
                    or value.get("publication_mode")
                    or value.get("content_state")
                    or ""
                ).strip()
            )

        entities = command.entities if isinstance(command.entities, Mapping) else {}
        if any(str(entities.get(key) or "").strip() for key in ("title", "draft_title")):
            return True
        if has_intent(command.constraints):
            return True
        for item in command.items or ():
            if str(getattr(item, "title", "") or "").strip():
                return True
            if has_intent(getattr(item, "constraints", None)):
                return True
        if semantic_state is not None:
            if has_intent(semantic_state.constraints):
                return True
            if any(
                str(getattr(item, "title", "") or "").strip()
                or has_intent(getattr(item, "constraints", None))
                for item in semantic_state.items
            ):
                return True
        return False

    @staticmethod
    def _delta_actions(delta: TaskDelta) -> list[str]:
        desired = delta.desired_changes if isinstance(delta.desired_changes, Mapping) else {}
        action = str(desired.get("semantic_action") or "").strip().upper()
        if action:
            return [action]
        return []

    # ── eligibility guards ──────────────────────────────────────────

    @staticmethod
    def _target_ok(
        command: Command,
        resolution: TargetResolution | None,
    ) -> bool:
        if not command.requires_target:
            return True
        if resolution is None:
            # Fall back to the resolution already attached to the Command by
            # the interpreter.
            return command.target_resolution == TargetResolutionStatus.RESOLVED.value
        return resolution.is_resolved

    @staticmethod
    def _target_reason(resolution: TargetResolution | None) -> str:
        if resolution is not None and resolution.is_ambiguous:
            return "ambiguous_target"
        return "target_unresolved"

    @staticmethod
    def _params_complete(command: Command, action: str) -> bool:
        if action in {"UPDATE_DRAFT", "DELETE_DRAFT", "DELETE_POST", "CANCEL_SCHEDULE", "PUBLISH_NOW"}:
            # A targeted write carries the resource id through the resolved
            # target; the mutation fields (title/content/run_at) come from the
            # structured Command.  A targeted action with a resolved target is
            # considered parameter-complete for routing; the Tool schema is the
            # final validator inside the durable execution.
            return True
        return True

    @staticmethod
    def _has_artifact_dependency(command: Command) -> bool:
        for delta in command.task_changes or ():
            if getattr(delta, "dependency_reference", None) or getattr(
                delta, "source_reference", None
            ):
                return True
        references = command.references or ()
        return any(
            isinstance(ref, Mapping) and ref.get("kind") in {"ARTIFACT", "artifact"}
            for ref in references
        )

    @staticmethod
    def _decision(
        route: TurnRoute,
        *,
        semantic_actions: list[str] | None = None,
        reason: str,
    ) -> FastPathDecision:
        return FastPathDecision(
            route=route,
            semantic_actions=list(semantic_actions or []),
            reason=reason,
        )


__all__ = [
    "FAST_READ_ACTIONS",
    "FAST_WRITE_ACTIONS",
    "FastPathGate",
    "TurnRoute",
]
