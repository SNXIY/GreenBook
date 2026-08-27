"""Deterministic Objective state reducer.

The single source of truth for Objective satisfaction and lifecycle.  ActionLoop,
resume, and ContextAssembler all use this.  Terminal state is driven only by real
Resources / Operations / Verification — never an LLM claim.

State machine (arrows are prose, not escapes):
    PENDING -> IN_PROGRESS -> COMPLETED
    PENDING -> WAITING -> IN_PROGRESS
    PENDING -> FAILED
Explicit related-resource/artifact/operation bindings are preferred;
``expected_resource_kind`` is only a compatibility fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Any

from .models import Objective, ObjectiveStatus, TaskRevision, TaskRevisionType

_NONTERMINAL = {
    "SUBMITTED",
    "RUNNING",
    "PENDING",
    "QUEUED",
    "WAITING_EXTERNAL",
    "RESULT_UNKNOWN",
    "PROCESSING",
    "UNKNOWN",
    "IN_PROGRESS",
    "WAITING",
    # Control pauses are still live execution facts.  They must not be
    # treated as settled predecessors merely because no worker holds a lease.
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "PAUSED",
}
_ACTIVE = {"RUNNING", "PROCESSING", "IN_PROGRESS"}
_WAITING = {
    "SUBMITTED",
    "QUEUED",
    "PENDING",
    "WAITING_EXTERNAL",
    "RESULT_UNKNOWN",
    "UNKNOWN",
    "WAITING",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "PAUSED",
}
_TERMINAL_FAILED = {"FAILED", "ERROR"}
_TERMINAL_OBJECTIVE_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}

# Cross-turn mutation metadata lives on the existing Objective.constraints
# projection.  It is deliberately small: the resource binding remains on the
# Objective/TaskResourceRef and the durable OperationLedger remains the
# execution/idempotency authority.
_MUTATION_NONTERMINAL_EXECUTION_STATUSES = {
    "SUBMITTED",
    "RUNNING",
    "PENDING",
    "QUEUED",
    "WAITING_EXTERNAL",
    "RESULT_UNKNOWN",
    "PROCESSING",
    "UNKNOWN",
    "IN_PROGRESS",
    "WAITING",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
}


def mutation_domain(action: str) -> str:
    """Return the narrow conflict domain for one canonical semantic action.

    Publication scheduling and immediate publication intentionally share one
    domain: they are alternative publication states for the same resource.
    Draft edits remain independent from publication changes.
    """

    normalized = str(action or "").strip().upper()
    if normalized in {
        "CREATE_SCHEDULE",
        "UPDATE_SCHEDULE",
        "CANCEL_SCHEDULE",
        "PUBLISH_NOW",
    }:
        return "PUBLICATION"
    if normalized in {"UPDATE_DRAFT", "DELETE_DRAFT"}:
        return "DRAFT"
    if normalized in {"DELETE_POST"}:
        return "PUBLICATION"
    if normalized in {"REPLY_COMMENT"}:
        return "COMMENT"
    return normalized or "UNKNOWN"


def _canonical_mutation_value(value: Any) -> Any:
    """Make desired-state metadata deterministic without changing business data."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_mutation_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_mutation_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def mutation_expected_state(action: str, desired: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project only the expected business value used for conflict comparison."""

    values = dict(desired or {})
    normalized = str(action or values.get("semantic_action") or "").upper()
    temporal = values.get("temporal_constraint")
    temporal = dict(temporal) if isinstance(temporal, Mapping) else {}
    if normalized in {"CREATE_SCHEDULE", "UPDATE_SCHEDULE"}:
        run_at = (
            values.get("run_at")
            or values.get("publish_at")
            or values.get("scheduled_at")
            or temporal.get("run_at")
            or temporal.get("publish_at")
            or temporal.get("scheduled_at")
        )
        result: dict[str, Any] = {"publication": "SCHEDULED"}
        if run_at not in (None, ""):
            result["run_at"] = run_at
        timezone = values.get("timezone") or temporal.get("timezone")
        if timezone not in (None, ""):
            result["timezone"] = timezone
        return _canonical_mutation_value(result)
    if normalized == "CANCEL_SCHEDULE":
        return {"publication": "CANCELLED"}
    if normalized == "PUBLISH_NOW":
        return {"publication": "PUBLISHED"}
    if normalized == "UPDATE_DRAFT":
        result = {
            key: values[key]
            for key in ("title", "content")
            if key in values and values[key] not in (None, "")
        }
        # Keep an explicit non-empty update deterministic even when a legacy
        # caller uses another mutation field.
        if not result:
            result = {
                str(key): value
                for key, value in values.items()
                if key not in {
                    "semantic_action",
                    "objective_id",
                    "mutation_objective_id",
                    "resource_id",
                    "draft_id",
                }
                and value not in (None, "")
            }
        return _canonical_mutation_value(result)
    if normalized in {"DELETE_DRAFT", "DELETE_POST"}:
        return {"lifecycle": "DELETED"}
    return _canonical_mutation_value(
        {
            str(key): value
            for key, value in values.items()
            if key not in {"semantic_action", "objective_id", "mutation_objective_id"}
            and value not in (None, "")
        }
    )


def mutation_details(
    action: str,
    desired: Mapping[str, Any] | None,
    resource_id: str = "",
) -> dict[str, Any]:
    """Return durable conflict metadata derived from canonical mutation facts."""

    domain = mutation_domain(action)
    desired_values = dict(desired or {})
    target_objective_id = str(
        desired_values.get("target_objective_id")
        or desired_values.get("objective_id")
        or ""
    )
    return {
        "mutation_domain": domain,
        "mutation_expected_state": mutation_expected_state(action, desired),
        # This is an identity key, not a second target/resource index.  The
        # resource itself remains the existing Objective ResourceBinding.
        "mutation_conflict_key": f"{str(resource_id or '')}:{domain}",
        "target_objective_id": target_objective_id,
    }


def mutation_objective_details(objective: Any) -> dict[str, Any]:
    """Read conflict metadata from an Objective, with legacy derivation."""

    constraints = dict(getattr(objective, "constraints", None) or {})
    action = str(
        constraints.get("semantic_action")
        or getattr(objective, "intent", "")
        or ""
    ).upper()
    resource_ids = [str(item) for item in (getattr(objective, "related_resource_ids", ()) or ()) if item]
    resource_id = resource_ids[0] if resource_ids else str(
        constraints.get("resource_id")
        or constraints.get("draft_id")
        or constraints.get("schedule_id")
        or constraints.get("post_id")
        or ""
    )
    derived = mutation_details(action, constraints, resource_id)
    domain = str(constraints.get("mutation_domain") or derived["mutation_domain"]).upper()
    expected = constraints.get("mutation_expected_state")
    if not isinstance(expected, Mapping):
        expected = derived["mutation_expected_state"]
    return {
        "objective_id": str(getattr(objective, "objective_id", "") or ""),
        "action": action,
        "resource_id": resource_id,
        "domain": domain,
        "expected_state": _canonical_mutation_value(expected),
        "conflict_key": f"{resource_id}:{domain}",
        "target_objective_id": str(
            constraints.get("target_objective_id")
            or constraints.get("objective_id")
            or ""
        ),
        "mutation_identity": str(constraints.get("mutation_identity") or ""),
        "status": str(constraints.get("mutation_status") or "ACTIVE").upper(),
    }


def mutation_objective_is_superseded(objective: Any) -> bool:
    return str(
        (getattr(objective, "constraints", None) or {}).get("mutation_status", "")
        or ""
    ).upper() == "SUPERSEDED"


def mutation_execution_state(task: Any, objective: Any) -> str:
    """Classify the durable phase of one logical mutation conservatively."""

    if mutation_objective_is_superseded(objective):
        return "SUPERSEDED"
    details = mutation_objective_details(objective)
    # TaskExecutionRef.goal_id is the existing durable Objective-to-execution
    # correlation.  It closes the window where Runtime submission succeeded
    # but the audit revision has not yet been appended.
    owned_refs = [
        item
        for item in (getattr(task, "execution_refs", ()) or ())
        if str(getattr(item, "goal_id", "") or "") == details["objective_id"]
    ]
    owned_statuses = {
        str(getattr(ref, "status", "") or "").upper()
        for ref in owned_refs
    }
    if owned_statuses & _MUTATION_NONTERMINAL_EXECUTION_STATUSES:
        return "INFLIGHT"
    if "COMPLETED" in owned_statuses:
        return "COMPLETED" if getattr(objective, "related_operations", None) else "UNKNOWN"
    if owned_statuses & {"FAILED", "CANCELLED"}:
        return "TERMINAL"
    matched_submission = False
    for revision in reversed(getattr(task, "revisions", ()) or ()):
        payload = dict(getattr(revision, "payload", None) or {})
        if payload.get("kind") != "ACTION_LOOP_MUTATION_SUBMISSION":
            continue
        payload_objective = str(payload.get("objective_id") or "")
        payload_resource = str(payload.get("resource_id") or "")
        payload_domain = str(payload.get("mutation_domain") or "").upper()
        if payload_objective:
            if payload_objective != details["objective_id"]:
                continue
        elif payload_resource != details["resource_id"]:
            continue
        if payload_domain and payload_domain != details["domain"]:
            continue
        if not payload_domain and str(payload.get("action") or "").upper() != details["action"]:
            continue
        matched_submission = True
        execution_id = str(payload.get("execution_id") or "")
        ref = next(
            (
                item for item in (getattr(task, "execution_refs", ()) or ())
                if str(getattr(item, "execution_id", "") or "") == execution_id
            ),
            None,
        )
        if ref is None:
            # A submission correlation without a TaskExecutionRef is a commit
            # gap.  It is safer to wait/reconcile than to supersede and write.
            return "UNKNOWN"
        status = str(getattr(ref, "status", "") or "").upper()
        if status in _MUTATION_NONTERMINAL_EXECUTION_STATUSES:
            return "INFLIGHT"
        if status == "COMPLETED":
            return "COMPLETED" if getattr(objective, "related_operations", None) else "UNKNOWN"
        if status in {"FAILED", "CANCELLED"}:
            return "TERMINAL"
        return "UNKNOWN"

    if matched_submission:
        return "UNKNOWN"
    status = str(getattr(objective, "status", "") or "").upper()
    if status == "PENDING":
        return "PENDING"
    if status in {"IN_PROGRESS", "WAITING"}:
        return "INFLIGHT"
    if status == "COMPLETED":
        return "COMPLETED" if getattr(objective, "related_operations", None) else "UNKNOWN"
    if status in {"FAILED", "CANCELLED"}:
        return "TERMINAL"
    return "UNKNOWN"


def mutation_conflicts(left: Any, right_details: Mapping[str, Any]) -> bool:
    """True when two unresolved logical mutations cannot coexist."""

    left_details = mutation_objective_details(left)
    same_lineage = bool(
        left_details["target_objective_id"]
        and left_details["target_objective_id"]
        == str(right_details.get("target_objective_id") or "")
    )
    same_resource = bool(
        left_details["resource_id"]
        and left_details["resource_id"] == str(right_details.get("resource_id") or "")
    )
    return bool(
        (same_resource or (left_details["domain"] == "PUBLICATION" and same_lineage))
        and left_details["domain"] == str(right_details.get("domain") or "").upper()
        and left_details["expected_state"] != right_details.get("expected_state")
    )


def mutation_is_superseded(task: Any, change: Any) -> bool:
    """Return whether a persisted mutation change has been made non-runnable."""

    desired = dict(getattr(change, "desired_changes", None) or {})
    objective_id = str(
        desired.get("objective_id")
        or (getattr(change, "target_reference", None) or {}).get("objective_id")
        or ""
    )
    identity = str(desired.get("mutation_identity") or "")
    for objective in getattr(task, "objectives", ()) or ():
        constraints = dict(getattr(objective, "constraints", None) or {})
        if objective_id and str(getattr(objective, "objective_id", "")) == objective_id:
            return mutation_objective_is_superseded(objective)
        if identity and str(constraints.get("mutation_identity") or "") == identity:
            return mutation_objective_is_superseded(objective)
    return False


def supersede_mutation_objective(
    task: Any,
    old: Any,
    *,
    new_objective_id: str,
    resource_id: str,
    new_details: Mapping[str, Any],
) -> None:
    """Mark one not-yet-submitted mutation non-runnable and audit the winner."""

    if mutation_objective_is_superseded(old):
        return
    constraints = dict(getattr(old, "constraints", None) or {})
    constraints["mutation_status"] = "SUPERSEDED"
    constraints["superseded_by"] = str(new_objective_id or "")
    old.constraints = constraints
    # Keep the logical work terminal and explicitly distinct from a failed
    # execution.  The existing marker remains the durable compatibility/audit
    # guard; the enum makes ordinary Objective projections safe by default.
    old.status = ObjectiveStatus.SUPERSEDED
    old_id = str(getattr(old, "objective_id", "") or "")
    payload = {
        "kind": "MUTATION_SUPERSEDED",
        "old_objective_id": old_id,
        "new_objective_id": str(new_objective_id or ""),
        "resource_id": str(resource_id or ""),
        "mutation_domain": str(new_details.get("mutation_domain") or "").upper(),
        "old_expected_state": dict(mutation_objective_details(old)["expected_state"]),
        "new_expected_state": dict(new_details.get("mutation_expected_state") or {}),
    }
    if any(
        dict(getattr(revision, "payload", None) or {}) == payload
        for revision in (getattr(task, "revisions", ()) or ())
    ):
        return
    task.revisions.append(
        TaskRevision(
            task_id=str(getattr(task, "task_id", "") or ""),
            type=TaskRevisionType.MODIFY_GOAL,
            payload=payload,
            previous_version=int(getattr(task, "version", 0) or 0),
        )
    )


def _resources(task: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in getattr(task, "resource_index", ()) or ()]


def _resource_ids(task: Any) -> dict[str, set[str]]:
    """Map resource_kind -> set of resource ids present (verified facts)."""
    mapping: dict[str, set[str]] = {}
    for resource in _resources(task):
        kind = str(resource.get("resource_kind", "")).upper()
        rid = str(resource.get("resource_id") or "")
        if kind and rid:
            mapping.setdefault(kind, set()).add(rid)
    return mapping


# CREATE/PRODUCE capabilities are satisfied when their verified resulting
# resource exists (GENERATE_CONTENT -> DRAFT, SCHEDULE_PUBLISH -> SCHEDULE).
_CAPABILITY_RESOURCE_KIND = {
    "GENERATE_CONTENT": "DRAFT",
    "CREATE_DRAFT": "DRAFT",
    "SCHEDULE_PUBLISH": "SCHEDULE",
    "PUBLISH_NOW": "POST",
    "SEARCH_COMMUNITY": "SEARCH_RESULT",
    "GET_POST_DETAIL": "POST",
    "LIST_OWN_POSTS": "POST",
}
# MUTATION capabilities are NOT satisfied by resource existence (the resource
# already exists before the mutation).  They complete only when the objective
# has an explicit verified operation / resource binding (the requested
# postcondition, e.g. title updated or schedule cancelled).
_MUTATION_CAPABILITIES = {
    "MANAGE_DRAFT", "MANAGE_SCHEDULE", "CANCEL_SCHEDULE", "DELETE_DRAFT", "DELETE_POST", "REPLY_USER",
}


def _capability_resource_present(
    resource_by_kind: dict[str, set[str]], capability: str,
    objective: Any | None = None,
) -> bool:
    """True when the Objective satisfies a required capability.

    CREATE caps need the verified resulting resource.  MUTATION caps need an
    explicit verified operation/resource binding — mere existence of the old
    resource is NOT the postcondition.

    Resource check is Objective-scoped when the Objective has owned resources:
    only resources bound to THIS Objective count (no cross-objective pollution).
    Falls back to task-global only when the Objective has no ownership binding
    yet (single-objective compatibility).
    """
    cap = str(capability).upper()
    if cap == "ANSWER_FROM_KNOWLEDGE":
        # This capability produces a read artifact, not a business resource.
        # The artifact is attached only after a successful canonical tool
        # response and is scoped to the owning Objective.
        return bool(getattr(objective, "related_artifact_ids", None) or [])
    if cap in _MUTATION_CAPABILITIES:
        # Completion is a verified postcondition (operation/resource binding on
        # the Objective), not "a resource of this kind already exists".
        if objective is not None:
            return bool(getattr(objective, "related_operations", None) or [])
        return False
    kind = _CAPABILITY_RESOURCE_KIND.get(cap, "")
    if not kind:
        return False
    # New business Objectives (required_capabilities present) are STRICT: only
    # resources owned by THIS Objective count.  Empty ownership means empty, and
    # we NEVER fall back to task-global (that would let one Objective's resource
    # satisfy another).  The task-global fallback is only for legacy
    # compatibility Objectives (required_capabilities empty).
    strict = bool(getattr(objective, "required_capabilities", None))
    owned = set(getattr(objective, "related_resource_ids", ()) or ()) if objective is not None else set()
    if strict or owned:
        return any(rid in owned for rid in resource_by_kind.get(kind, set()))
    return bool(resource_by_kind.get(kind))


def _execution_statuses(task: Any) -> list[str]:
    return [
        str(getattr(e, "status", "")).upper()
        for e in getattr(task, "execution_refs", ()) or ()
    ]


def _execution_refs_for_objective(task: Any, objective: Objective) -> list[Any]:
    """Return only execution facts owned by ``objective``.

    ``TaskExecutionRef.goal_id`` is the existing durable correlation field.
    New Objective tasks store the initiating Objective id there; legacy
    single-objective tasks may omit it.  A multi-objective task with an
    unowned ref is deliberately treated as ambiguous instead of projecting
    that ref onto every sibling.
    """

    objective_id = str(getattr(objective, "objective_id", "") or "")
    refs = list(getattr(task, "execution_refs", ()) or ())
    owned = [
        ref
        for ref in refs
        if str(getattr(ref, "goal_id", "") or "") == objective_id
    ]
    if owned:
        return owned
    if len(list(getattr(task, "objectives", ()) or ())) == 1:
        return refs
    return []


def is_objective_satisfied(task: Any, objective: Objective) -> bool:
    """True when the objective has a verified binding (or expected resource kind).

    Explicit related resource ids win; a durable ``expected_resource_kind`` is
    the compatibility fallback.
    """
    resource_by_kind = _resource_ids(task)
    # A zero-row discovery is a verified result even when a legacy/new task
    # projection omitted required_capabilities. The marker is written only
    # after a successful authoritative SEARCH_POSTS/LIST_OWN_POSTS response;
    # it creates no resource identity and cannot satisfy a write capability.
    discovery = (getattr(objective, "constraints", None) or {}).get("discovery_result")
    if (
        isinstance(discovery, Mapping)
        and str(discovery.get("status") or "").upper() == "EMPTY"
        and str(discovery.get("action") or "").upper() in {"SEARCH_POSTS", "LIST_OWN_POSTS"}
    ):
        return True
    # A business Objective may require MULTIPLE capabilities (GENERATE_CONTENT +
    # SCHEDULE_PUBLISH).  It is satisfied only when EVERY required capability's
    # resource is present and owned by THIS Objective (no cross-objective
    # pollution).  This takes precedence over the loose explicit-ids check.
    required_caps = list(getattr(objective, "required_capabilities", None) or ())
    if required_caps:
        missing = [
            c for c in required_caps
            if not _capability_resource_present(resource_by_kind, c, objective=objective)
        ]
        return not missing
    # Search candidates and detail resources are inputs to a grounded answer,
    # not the final outcome.  Check the synthesis artifact before the generic
    # explicit-resource compatibility fallback below.
    requirement = str(getattr(objective, "result_requirement", "") or "").upper()
    if requirement == "GROUNDED_SYNTHESIS":
        return bool(getattr(objective, "related_artifact_ids", None) or [])
    explicit = [rid for rid in (getattr(objective, "related_resource_ids", None) or [])]
    if explicit:
        # Every explicitly bound resource must still exist as a verified fact.
        return any(
            rid in ids for ids in resource_by_kind.values() for rid in explicit
        )
    kind = str(getattr(objective, "expected_resource_kind", "") or "").upper()
    return bool(kind and resource_by_kind.get(kind))


def has_nonterminal_execution(task: Any) -> bool:
    return any(status in _NONTERMINAL for status in _execution_statuses(task))


def has_active_execution(task: Any) -> bool:
    return any(status in _ACTIVE for status in _execution_statuses(task))


def has_waiting_execution(task: Any) -> bool:
    return any(status in _WAITING for status in _execution_statuses(task))


def has_failed_execution(task: Any) -> bool:
    return any(status in _TERMINAL_FAILED for status in _execution_statuses(task))


def is_context_isolated_task(task: Any) -> bool:
    """Keep orphaned or approval-paused work out of current-turn context.

    A model-facing context may include a Task marked ``RUNNING`` only when
    durable execution facts still show live work.  Approval-gated executions
    are handled by the explicit approval surface, not generic target/retry
    selection.  This also isolates historical approval cards without
    approving, rejecting, or deleting the underlying business operation.

    Lightweight compatibility test doubles may not expose execution fields;
    those retain the historical builder contract.
    """

    if not hasattr(task, "active_execution_id") and not hasattr(task, "execution_refs"):
        return False
    task_status = str(
        getattr(getattr(task, "status", None), "value", getattr(task, "status", ""))
        or ""
    ).upper()
    if task_status not in {"RUNNING", "IN_PROGRESS"}:
        return False

    execution_refs = list(getattr(task, "execution_refs", ()) or ())
    # A freshly assembled Task may be RUNNING before its first execution ref
    # is attached.  Historical residue has durable execution evidence; with
    # no ref there is nothing stale to classify or hide.
    if not execution_refs:
        return False

    execution_statuses = _execution_statuses(task)
    if any(status in {"WAITING_APPROVAL", "WAITING_HUMAN"} for status in execution_statuses):
        return True

    # Critical stale shape: a non-terminal Task with no active execution and
    # no other live execution reference.  Do not infer COMPLETED/FAILED;
    # isolate it from current context instead.
    return not getattr(task, "active_execution_id", None) and not has_nonterminal_execution(task)


def bind_related(
    task: Any,
    *,
    objective_id: str,
    resource_id: str | None = None,
    resource_kind: str = "",
    artifact_id: str | None = None,
    operation_id: str | None = None,
) -> None:
    """Deterministically bind a real result to the matching objective (task-scoped)."""
    for objective in getattr(task, "objectives", ()) or ():
        if str(getattr(objective, "objective_id", "")) != objective_id:
            continue
        if resource_id and str(resource_id) not in (objective.related_resource_ids or []):
            objective.related_resource_ids.append(str(resource_id))
        if artifact_id and str(artifact_id) not in (objective.related_artifact_ids or []):
            objective.related_artifact_ids.append(str(artifact_id))
        if operation_id and str(operation_id) not in (objective.related_operations or []):
            objective.related_operations.append(str(operation_id))
        objective.expected_resource_kind = objective.expected_resource_kind or resource_kind


def objective_for_resource(task: Any, resource_kind: str) -> Objective | None:
    """Return the pending Objective this resource kind satisfies (task-scoped)."""
    normalized = (resource_kind or "").upper()
    if not normalized:
        return None
    for objective in getattr(task, "objectives", ()) or ():
        status = str(getattr(objective, "status", "")).upper()
        if status in _TERMINAL_OBJECTIVE_STATUSES:
            continue
        if str(getattr(objective, "expected_resource_kind", "") or "").upper() == normalized:
            return objective
    # Fall back to the first pending objective without a satisfied kind.
    for objective in getattr(task, "objectives", ()) or ():
        if str(getattr(objective, "status", "")).upper() in {"PENDING", "IN_PROGRESS", "WAITING"}:
            return objective
    return None


class ObjectiveStateReducer:
    """Recompute each Objective's status from verified Task facts."""

    def reduce(self, task: Any) -> list[Objective]:
        objectives = list(getattr(task, "objectives", ()) or ())
        now = datetime.now(UTC).isoformat()
        for objective in objectives:
            current = str(getattr(objective, "status", "")).upper()
            if mutation_objective_is_superseded(objective):
                # Superseded mutation work is historical/non-runnable.  It is
                # not reopened by a later reducer pass; the replacement
                # mutation owns the current desired outcome.
                # Normalize legacy rows that used FAILED as the projection
                # placeholder before ObjectiveStatus.SUPERSEDED existed.
                objective.status = ObjectiveStatus.SUPERSEDED
                objective.updated_at = now
                continue
            if current in _TERMINAL_OBJECTIVE_STATUSES:
                continue  # preserve terminal history (reopen must not tamper)
            owned_refs = _execution_refs_for_objective(task, objective)
            owned_statuses = {
                str(getattr(ref, "status", "") or "").upper()
                for ref in owned_refs
            }
            if is_objective_satisfied(task, objective):
                objective.status = ObjectiveStatus.COMPLETED
                objective.completed_at = objective.completed_at or now
            elif "CANCELLED" in owned_statuses:
                # Approval rejection is a terminal user decision.  Leaving
                # the Objective PENDING makes the ActionLoop select the old
                # mutation again after a later approval continuation, which
                # can create a second approval/write for the same request.
                objective.status = ObjectiveStatus.CANCELLED
            elif owned_statuses & _TERMINAL_FAILED:
                objective.status = ObjectiveStatus.FAILED
            elif owned_statuses & _ACTIVE:
                objective.status = ObjectiveStatus.IN_PROGRESS
            elif owned_statuses & _WAITING:
                objective.status = ObjectiveStatus.WAITING
            else:
                objective.status = ObjectiveStatus.PENDING
            objective.updated_at = now
        return objectives


def unsatisfied_objectives(task: Any) -> list[Objective]:
    objectives = list(getattr(task, "objectives", ()) or ())
    return [
        o for o in objectives
        if not mutation_objective_is_superseded(o)
        and str(getattr(o, "status", "")).upper() not in _TERMINAL_OBJECTIVE_STATUSES
    ]


def all_objectives_satisfied(task: Any) -> bool:
    if has_nonterminal_execution(task):
        return False
    # FAILED/CANCELLED are terminal for work selection, but they are not
    # terminal-success.  A Task may only project COMPLETED when every required
    # Objective is explicitly verified as COMPLETED.
    return all(
        mutation_objective_is_superseded(objective)
        or str(getattr(objective, "status", "")).upper() == "COMPLETED"
        for objective in getattr(task, "objectives", ()) or ()
    )


__all__ = [
    "ObjectiveStateReducer",
    "all_objectives_satisfied",
    "bind_related",
    "has_active_execution",
    "has_failed_execution",
    "has_nonterminal_execution",
    "has_waiting_execution",
    "is_context_isolated_task",
    "is_objective_satisfied",
    "mutation_conflicts",
    "mutation_details",
    "mutation_domain",
    "mutation_execution_state",
    "mutation_expected_state",
    "mutation_is_superseded",
    "mutation_objective_details",
    "mutation_objective_is_superseded",
    "objective_for_resource",
    "supersede_mutation_objective",
    "unsatisfied_objectives",
]
