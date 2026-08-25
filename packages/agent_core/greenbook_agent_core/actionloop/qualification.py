"""Thin write-admission boundary for the Objective-driven ActionLoop.

ActionLoop owns action selection.  Interpreter, TargetResolver, and
TemporalResolver own semantic facts.  This module only checks the exact
selected write immediately before durable submission and returns ALLOW/BLOCK.
It must not produce an action list or recover a target/time from language.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..task.objective_reducer import mutation_objective_details


_WRITE_ACTIONS = frozenset(
    {
        "CREATE_DRAFT",
        "UPDATE_DRAFT",
        "DELETE_DRAFT",
        "DELETE_POST",
        "CREATE_SCHEDULE",
        "UPDATE_SCHEDULE",
        "CANCEL_SCHEDULE",
        "PUBLISH_NOW",
    }
)
_TARGET_ARGUMENT = {
    "UPDATE_DRAFT": ("draft_id", "DRAFT"),
    "DELETE_DRAFT": ("draft_id", "DRAFT"),
    "UPDATE_SCHEDULE": ("schedule_id", "SCHEDULE"),
    "CANCEL_SCHEDULE": ("schedule_id", "SCHEDULE"),
    "DELETE_POST": ("post_id", "POST"),
}
_NONTERMINAL_EXECUTION_STATUSES = {
    "SUBMITTED",
    "RUNNING",
    "PENDING",
    "QUEUED",
    "RESULT_UNKNOWN",
    "FAILED_RETRYABLE",
    "RETRYABLE",
    "RETRYING",
}
_IMMEDIATE_PUBLICATION = {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "IMMEDIATE"}
_SCHEDULED_PUBLICATION = {
    "SCHEDULED_PUBLISH",
    "SCHEDULE",
    "SCHEDULED",
    "FUTURE",
    "FUTURE_PUBLISH",
}


@dataclass(frozen=True)
class ActionGuardResult:
    """Typed result returned by the final write-admission check."""

    allowed: bool
    code: str = ""
    reason: str = ""


def guard_action(
    action: str,
    objective: Any,
    current_state: Any,
    *,
    command: Any | None = None,
    arguments: Mapping[str, Any] | None = None,
    mutation_plan_selected: bool = False,
) -> ActionGuardResult:
    """Allow or block one already-selected write.

    ``command`` and ``mutation_plan_selected`` remain accepted for call-site
    compatibility, but are deliberately not used to select or repair a
    target.  The ActionLoop must pass the exact typed mutation arguments.
    """

    del command, mutation_plan_selected
    normalized = str(action or "").upper()
    if normalized not in _WRITE_ACTIONS:
        return ActionGuardResult(True)

    args = dict(arguments or {})
    constraints = _canonical_constraints(objective)
    strict = bool(
        getattr(objective, "required_capabilities", None)
        or getattr(objective, "related_resource_ids", None)
    )

    if _has_inflight_execution(current_state, objective):
        return ActionGuardResult(
            False,
            "WRITE_IN_FLIGHT",
            "a previous write is still in flight",
        )

    target_spec = _TARGET_ARGUMENT.get(normalized)
    if target_spec is not None:
        target_argument, expected_kind = target_spec
        target_id = str(args.get(target_argument) or "")
        if not target_id:
            # Legacy Objective envelopes did not carry typed target arguments;
            # preserve their historical path while strict business Objectives
            # fail closed before durable submission.
            if strict:
                return ActionGuardResult(
                    False,
                    "STALE_TARGET",
                    "write target identity is missing",
                )
        else:
            target_result = _check_exact_target(
                objective,
                current_state,
                target_id=target_id,
                expected_kind=expected_kind,
                strict=strict,
            )
            if target_result is not None:
                return target_result

    if normalized in {"CREATE_SCHEDULE", "UPDATE_SCHEDULE"}:
        canonical_run_at = _canonical_run_at(objective, current_state)
        if not canonical_run_at:
            return ActionGuardResult(
                False,
                "TEMPORAL_NOT_RESOLVED",
                "schedule write has no canonical run_at",
            )
        supplied_run_at = str(args.get("run_at") or "")
        if strict and not supplied_run_at:
            return ActionGuardResult(
                False,
                "TEMPORAL_NOT_RESOLVED",
                "schedule write has no exact canonical run_at binding",
            )
        if supplied_run_at and not _same_instant(supplied_run_at, canonical_run_at):
            return ActionGuardResult(
                False,
                "TEMPORAL_MISMATCH",
                "schedule write does not use the Objective canonical run_at",
            )

    if normalized == "PUBLISH_NOW":
        intent = _canonical_publication_intent(objective, current_state)
        if intent in _SCHEDULED_PUBLICATION or intent == "DRAFT_ONLY":
            return ActionGuardResult(
                False,
                "PUBLICATION_NOT_IMMEDIATE",
                "publish-now requires the canonical immediate publication intent",
            )
        if strict and intent not in _IMMEDIATE_PUBLICATION:
            return ActionGuardResult(
                False,
                "PUBLICATION_NOT_IMMEDIATE",
                "publish-now has no canonical immediate publication intent",
            )
        temporal_kind = _canonical_temporal_kind(objective, current_state)
        if temporal_kind in {"FUTURE", "SCHEDULED", "UNRESOLVED"}:
            return ActionGuardResult(
                False,
                "PUBLICATION_NOT_IMMEDIATE",
                "publish-now cannot consume a future or unresolved temporal fact",
            )

    if normalized == "DELETE_POST" and _approval_state(objective, current_state) in {
        "PENDING",
        "REQUIRED",
        "REJECTED",
    }:
        return ActionGuardResult(
            False,
            "APPROVAL_REQUIRED",
            "delete requires the existing HITL boundary",
        )

    if _duplicate_verified(current_state):
        return ActionGuardResult(
            False,
            "ALREADY_VERIFIED",
            "the exact write is already verified",
        )

    duplicate_kind = {
        "CREATE_DRAFT": "DRAFT",
        "CREATE_SCHEDULE": "SCHEDULE",
        "PUBLISH_NOW": "POST",
    }.get(normalized)
    if duplicate_kind and _owned_resource_exists(objective, current_state, duplicate_kind):
        return ActionGuardResult(
            False,
            "ALREADY_VERIFIED",
            "the Objective already owns the verified result",
        )

    return ActionGuardResult(True)


def _canonical_constraints(objective: Any) -> Mapping[str, Any]:
    value = getattr(objective, "constraints", None) if objective is not None else None
    return value if isinstance(value, Mapping) else {}


def _canonical_run_at(objective: Any, current_state: Any) -> str:
    constraints = _canonical_constraints(objective)
    value = constraints.get("run_at")
    if value in (None, "") and isinstance(current_state, Mapping):
        value = current_state.get("run_at")
    if value in (None, ""):
        return ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo is None:
        return ""
    return text


def _canonical_temporal_kind(objective: Any, current_state: Any) -> str:
    constraints = _canonical_constraints(objective)
    value = constraints.get("temporal_kind")
    if value in (None, "") and isinstance(current_state, Mapping):
        value = current_state.get("temporal_kind") or current_state.get("temporal_state")
    return str(value or "").upper()


def _canonical_publication_intent(objective: Any, current_state: Any) -> str:
    constraints = _canonical_constraints(objective)
    value = constraints.get("publication_intent")
    if value in (None, "") and isinstance(current_state, Mapping):
        value = current_state.get("publication_intent")
    return str(value or "").upper()


def _approval_state(objective: Any, current_state: Any) -> str:
    constraints = _canonical_constraints(objective)
    value = constraints.get("approval_state")
    if isinstance(current_state, Mapping):
        value = current_state.get("approval_state") or value
    return str(value or "NOT_REQUESTED").upper()


def _check_exact_target(
    objective: Any,
    current_state: Any,
    *,
    target_id: str,
    expected_kind: str,
    strict: bool,
) -> ActionGuardResult | None:
    rows = _resource_rows(current_state)
    matching = [
        row
        for row in rows
        if str(row.get("resource_id") or row.get("id") or "") == target_id
    ]
    if not matching:
        return ActionGuardResult(
            False,
            "STALE_TARGET",
            "write target is not present in the canonical ResourceBinding",
        )
    kinds = {
        str(row.get("resource_kind") or row.get("kind") or "").upper()
        for row in matching
    }
    if kinds and expected_kind not in kinds:
        return ActionGuardResult(
            False,
            "TARGET_KIND_MISMATCH",
            "write target kind is not legal for the action",
        )
    if not strict:
        return None
    objective_id = str(getattr(objective, "objective_id", "") or "")
    owned_ids = {
        str(value)
        for value in (getattr(objective, "related_resource_ids", None) or ())
        if str(value)
    }
    if owned_ids and target_id not in owned_ids:
        return ActionGuardResult(
            False,
            "OWNERSHIP_MISMATCH",
            "target/resource is not bound to the selected Objective",
        )
    owners = {
        str(row.get("objective_id") or "")
        for row in matching
        if row.get("objective_id") is not None
    }
    if owners and objective_id not in owners:
        # A user-triggered cross-turn mutation is a new Objective, while its
        # verified ResourceBinding intentionally remains owned by the
        # predecessor Objective.  The mutation admission path has already
        # grounded both identities; accept that narrow predecessor relation
        # without allowing an unrelated sibling Objective to borrow the
        # resource.
        predecessor_id = str(
            _canonical_constraints(objective).get("target_objective_id") or ""
        )
        if predecessor_id and predecessor_id in owners:
            return None
        if _same_mutation_lineage_owner(
            current_state,
            objective,
            owners=owners,
            target_id=target_id,
        ):
            return None
        return ActionGuardResult(
            False,
            "OWNERSHIP_MISMATCH",
            "target/resource is owned by another Objective",
        )
    return None


def _same_mutation_lineage_owner(
    current_state: Any,
    objective: Any,
    *,
    owners: set[str],
    target_id: str,
) -> bool:
    """Allow a later mutation to use the prior mutation's binding.

    A cross-turn mutation Objective is distinct from the Objective that
    created the resource.  The durable ResourceBinding may therefore still
    name an earlier mutation Objective.  The admission is safe only when the
    earlier owner is on the same target-objective/resource/domain lineage;
    an unrelated sibling cannot borrow the resource.
    """

    task = (
        current_state.get("task")
        if isinstance(current_state, Mapping)
        else current_state
    )
    if task is None:
        return False
    current = mutation_objective_details(objective)
    target_objective_id = str(current.get("target_objective_id") or "")
    resource_id = str(target_id or "")
    domain = str(current.get("domain") or "").upper()
    if not target_objective_id or not resource_id or not domain:
        return False

    for candidate in getattr(task, "objectives", ()) or ():
        details = mutation_objective_details(candidate)
        if details["objective_id"] not in owners:
            continue
        candidate_resources = {
            str(value)
            for value in (getattr(candidate, "related_resource_ids", ()) or ())
            if str(value)
        }
        if resource_id not in candidate_resources:
            continue
        if details["domain"] != domain:
            continue
        if details["target_objective_id"] == target_objective_id:
            return True
    return False


def _has_inflight_execution(current_state: Any, objective: Any | None = None) -> bool:
    task = current_state if not isinstance(current_state, Mapping) else current_state.get("task")
    refs = list(getattr(task, "execution_refs", ()) or ())
    objective_id = str(getattr(objective, "objective_id", "") or "")
    if objective_id:
        owned = [
            item
            for item in refs
            if str(getattr(item, "goal_id", "") or "") == objective_id
        ]
        if owned:
            refs = owned
        elif any(not str(getattr(item, "goal_id", "") or "") for item in refs):
            # Legacy unowned execution refs cannot be safely assigned to a
            # sibling Objective, so retain the existing fail-closed behavior.
            refs = [
                item
                for item in refs
                if not str(getattr(item, "goal_id", "") or "")
            ]
        else:
            refs = []
    return any(
        str(getattr(item, "status", "") or "").upper()
        in _NONTERMINAL_EXECUTION_STATUSES
        for item in refs
    )


def _duplicate_verified(current_state: Any) -> bool:
    return bool(
        isinstance(current_state, Mapping)
        and current_state.get("duplicate_verified") is True
    )


def _owned_resource_exists(objective: Any, current_state: Any, kind: str) -> bool:
    rows = _resource_rows(current_state)
    owned_ids = {
        str(value)
        for value in (getattr(objective, "related_resource_ids", None) or ())
        if str(value)
    }
    strict = bool(getattr(objective, "required_capabilities", None) or owned_ids)
    for row in rows:
        row_kind = str(row.get("resource_kind") or row.get("kind") or "").upper()
        resource_id = str(row.get("resource_id") or row.get("id") or "")
        if row_kind != kind or not resource_id:
            continue
        if not strict:
            return True
        if resource_id in owned_ids:
            return True
        objective_id = str(getattr(objective, "objective_id", "") or "")
        if objective_id and str(row.get("objective_id") or "") == objective_id:
            return True
    return False


def _resource_rows(current_state: Any) -> list[dict[str, Any]]:
    state = dict(current_state) if isinstance(current_state, Mapping) else {}
    task = current_state if not isinstance(current_state, Mapping) else state.get("task")
    projected_rows = [
        dict(row)
        for row in (state.get("resources") or ())
        if isinstance(row, Mapping)
    ]
    durable_rows = [
        dict(row)
        if isinstance(row, Mapping)
        else {
            "resource_id": getattr(row, "resource_id", ""),
            "resource_kind": getattr(row, "resource_kind", ""),
            "objective_id": getattr(row, "objective_id", None),
        }
        for row in (getattr(task, "resource_index", ()) or ())
    ]
    if not durable_rows:
        return projected_rows
    by_id = {
        str(row.get("resource_id") or row.get("id") or ""): row
        for row in durable_rows
        if str(row.get("resource_id") or row.get("id") or "")
    }
    for row in projected_rows:
        resource_id = str(row.get("resource_id") or row.get("id") or "")
        if not resource_id or resource_id not in by_id:
            durable_rows.append(row)
    return durable_rows


def _same_instant(left: str, right: str) -> bool:
    try:
        def parse(value: str) -> datetime:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        left_value = parse(left)
        right_value = parse(right)
        if left_value.tzinfo is None or right_value.tzinfo is None:
            return False
        return left_value.astimezone(UTC) == right_value.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return False


__all__ = ["ActionGuardResult", "guard_action"]
