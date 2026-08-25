"""Deterministic projections from durable facts into ContextSnapshot fields."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from types import SimpleNamespace
from typing import Any

from .models import ContextSnapshot, DerivedConversationContext


_TERMINAL_STATES = frozenset({
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "SUPERSEDED",
})


def _is_identity_key(key: Any) -> bool:
    """Return whether a provider view key would expose canonical identity."""

    normalized = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        str(key or "").strip(),
    ).lower().replace("-", "_")
    return (
        normalized in {"id", "ids", "resource_refs"}
        or normalized.endswith("_id")
        or normalized.endswith("_ids")
    )


def _safe_interpreter_value(value: Any, *, limit: int = 1200) -> Any:
    """Keep semantic evidence while removing nested runtime/business ids."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_interpreter_value(item, limit=max(100, limit // 2))
            for key, item in list(value.items())[:20]
            if not _is_identity_key(key)
        }
    if isinstance(value, list):
        return [
            _safe_interpreter_value(item, limit=max(100, limit // 2))
            for item in value[:20]
        ]
    if isinstance(value, str):
        return value[:limit]
    return value


def _verified_outcome(value: Mapping[str, Any]) -> Any:
    """Project only an already-present verified/business observation."""

    for key in (
        "verified_outcome",
        "verified_facts",
        "business_result",
        "outcome",
    ):
        candidate = value.get(key)
        if candidate not in (None, "", {}, []):
            return _safe_interpreter_value(candidate)
    provenance = value.get("provenance")
    if provenance not in (None, "", [], {}):
        return {"provenance": _safe_interpreter_value(provenance)}
    return None


def as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return {}


def project_task(task: Any) -> dict[str, Any]:
    value = as_dict(task)
    # The durable Task contains complete goal/plan/revision snapshots.  Those
    # are repository facts, not model-facing context.  Returning the whole
    # object here made every active task repeat a potentially very large goal
    # tree in Command, Goal, and Agent prompts.  Keep binding and lifecycle
    # facts, plus body-free references, and let repositories remain the source
    # of truth for the full snapshots.
    result = {
        key: value.get(key)
        for key in (
            "task_id",
            "conversation_id",
            "user_id",
            "tenant_id",
            "goal",
            "goal_category",
            "goal_summary",
            "status",
            "phase",
            "priority",
            "task_type",
            "execution_mode",
            "root_goal_id",
            "goal_tree_version",
            "plan_version",
            "active_execution_id",
            "last_action",
            "last_error",
            "retry_count",
            "max_retries",
            "version",
            "created_at",
            "updated_at",
            "completed_at",
        )
        if value.get(key) is not None
    }
    result.update({
        "task_id": str(value.get("task_id", "")),
        "kind": "TASK",
        "status": str(value.get("status", "")),
        "depends_on": [str(item) for item in (value.get("depends_on") or [])[:20]],
        "artifacts": [_compact_artifact(item) for item in (value.get("artifacts") or [])[:20]],
        "goals": [_compact_task_goal(item) for item in (value.get("goals") or [])[:40]],
        "objectives": [_compact_objective(item) for item in (value.get("objectives") or [])[:40]],
        "execution_refs": [
            _compact_execution_ref(item)
            for item in (value.get("execution_refs") or [])[:40]
        ],
        "resource_index": [
            _compact_resource(item)
            for item in (value.get("resource_index") or [])[:40]
        ],
        "plan_history": [
            _compact_plan_revision(item)
            for item in (value.get("plan_history") or [])[-8:]
        ],
        "revisions": [
            _compact_task_revision(item)
            for item in (value.get("revisions") or [])[-8:]
        ],
        "action_history": [str(item)[:500] for item in (value.get("action_history") or [])[-8:]],
    })
    return result


def project_goal(goal: Any, *, task_id: str = "") -> dict[str, Any]:
    value = as_dict(goal)
    result = {
        key: value.get(key)
        for key in (
            "goal_id",
            "task_id",
            "description",
            "kind",
            "goal_type",
            "parent_goal",
            "status",
            "required_capabilities",
            "dependencies",
            "depends_on_goal_ids",
            "execution_id",
            "updated_at",
        )
        if value.get(key) is not None
    }
    if task_id and not result.get("task_id"):
        result["task_id"] = task_id
    result["goal_id"] = str(value.get("goal_id", ""))
    result["kind"] = "GOAL"
    result["description"] = str(value.get("description", ""))[:2000]
    result["required_capabilities"] = [
        str(item) for item in (value.get("required_capabilities") or [])[:20]
    ]
    result["dependencies"] = [
        str(item)
        for item in (value.get("dependencies") or value.get("depends_on_goal_ids") or [])[:40]
    ]
    # Preserve the semantic shape without recursively embedding child Goals.
    result["children"] = [
        str(item.get("goal_id"))
        for item in (value.get("children") or [])
        if isinstance(item, Mapping) and item.get("goal_id")
    ][:40]
    return result


def project_artifact(artifact: Any, *, task_id: str = "") -> dict[str, Any]:
    value = as_dict(artifact)
    result = {
        key: value.get(key)
        for key in (
            "artifact_id",
            "task_id",
            "execution_id",
            "owner_task_id",
            "owner_execution_id",
            "created_by_agent",
            "step_id",
            "artifact_type",
            "resource_id",
            "resource_kind",
            "resource_type",
            "title",
            "summary",
            "status",
            "run_at",
            "timezone",
            "version",
            "content_hash",
            "lifecycle",
            "created_at",
            "updated_at",
        )
        if value.get(key) is not None
    }
    if task_id and not result.get("task_id"):
        result["task_id"] = task_id
    result["kind"] = "ARTIFACT"
    result["artifact_id"] = str(value.get("artifact_id", ""))
    result["title"] = _text(value.get("title"), 500)
    result["summary"] = _text(value.get("summary"), 2000)
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        refs = metadata.get("resource_refs")
        if isinstance(refs, list):
            result["resource_refs"] = [
                _compact_resource_ref(item) for item in refs[:40]
            ]
    return result


def project_execution(execution: Any) -> dict[str, Any]:
    value = as_dict(execution)
    result = {
        key: value.get(key)
        for key in (
            "execution_id",
            "plan_id",
            "task_id",
            "status",
            "control_state",
            "control_reason",
            "current_step_index",
            "requires_approval",
            "has_side_effects",
            "created_at",
            "updated_at",
            "completed_at",
            "version",
        )
        if value.get(key) is not None
    }
    result.update({
        "kind": "EXECUTION",
        "execution_id": str(value.get("execution_id", "")),
        "task_id": str(value.get("task_id", "")),
    })
    steps = value.get("steps") or []
    result["total_step_count"] = len(steps)
    result["completed_step_count"] = sum(
        1 for item in steps
        if str(as_dict(item).get("status", "")).upper() == "COMPLETED"
    )
    result["steps"] = [_compact_step(item) for item in steps[:20]]
    return result


def _compact_artifact(value: Any) -> dict[str, Any]:
    return project_artifact(value)


def _compact_task_goal(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        "goal_id": str(item.get("goal_id", "")),
        "task_id": str(item.get("task_id", "")),
        "description": _text(item.get("description"), 1200),
        "kind": _text(item.get("kind"), 120),
        "status": _text(item.get("status"), 80),
        "depends_on_goal_ids": [
            str(ref) for ref in (item.get("depends_on_goal_ids") or [])[:40]
        ],
        "execution_id": item.get("execution_id"),
        "artifact_refs": [
            _compact_resource_ref(ref)
            for ref in (item.get("artifact_refs") or [])[:20]
        ],
        "updated_at": item.get("updated_at"),
    }


def _compact_objective(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    result = {
        "objective_id": str(item.get("objective_id", "")),
        "task_id": str(item.get("task_id", "")),
        "description": _text(item.get("description"), 1200),
        "intent": _text(item.get("intent"), 1200),
        "status": _text(item.get("status", "PENDING"), 80),
        "expected_resource_kind": _text(item.get("expected_resource_kind"), 80),
        "result_requirement": _text(item.get("result_requirement"), 80),
        "required_capabilities": [
            _text(capability, 100)
            for capability in (item.get("required_capabilities") or [])[:20]
        ],
        "constraints": dict(item.get("constraints") or {})
        if isinstance(item.get("constraints"), Mapping)
        else {},
        "dependencies": [
            str(ref) for ref in (item.get("dependencies") or [])[:20]
        ],
        "expected_postcondition": item.get("expected_postcondition") or {},
        "related_resource_ids": [
            str(ref) for ref in (item.get("related_resource_ids") or [])[:20]
        ],
        "related_artifact_ids": [
            str(ref) for ref in (item.get("related_artifact_ids") or [])[:20]
        ],
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("completed_at"),
    }
    for key in (
        "desired_outcome",
        "outcome",
        "verified_outcome",
        "business_result",
        "verified_facts",
    ):
        if item.get(key) not in (None, "", {}, []):
            result[key] = item.get(key)
    related_kinds = item.get("related_resource_kinds") or item.get("resource_kinds")
    if related_kinds:
        result["related_resource_kinds"] = (
            [str(kind).upper() for kind in related_kinds[:10]]
            if isinstance(related_kinds, list)
            else [str(related_kinds).upper()]
        )
    return result


def _compact_execution_ref(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        "execution_id": str(item.get("execution_id", "")),
        "task_id": str(item.get("task_id", "")),
        "goal_id": item.get("goal_id"),
        "status": _text(item.get("status"), 80),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _compact_resource(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        key: item.get(key)
        for key in (
            "resource_id",
            "resource_kind",
            "kind",
            "title",
            "label",
            "status",
            "state",
            "lifecycle",
            "scheduled_at",
            "run_at",
        "updated_at",
        "task_id",
        # This is immutable ownership evidence from TaskResourceRef.  It is
        # intentionally retained in the canonical projection and removed by
        # _safe_interpreter_value before the provider view is serialized.
        "objective_id",
        "verified_outcome",
            "verified_facts",
            "business_result",
            "outcome",
            "provenance",
            "source",
            "tool",
        )
        if item.get(key) is not None
    }


def _compact_resource_ref(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        key: item.get(key)
        for key in (
            "artifact_id",
            "artifact_type",
            "resource_id",
            "resource_kind",
            # Executor-produced refs carry ``kind`` ("POST", "DRAFT", ...);
            # preserve it so downstream facts projections can classify them.
            "kind",
            "title",
            "label",
            "version",
            "source",
            "tool",
            "summary",
        )
        if item.get(key) is not None
    }


def _compact_plan_revision(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        key: item.get(key)
        for key in (
            "revision_id",
            "task_id",
            "plan_version",
            "previous_plan_version",
            "decision",
            "reason",
            "created_at",
        )
        if item.get(key) is not None
    }


def _compact_task_revision(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    return {
        key: item.get(key)
        for key in (
            "revision_id",
            "task_id",
            "type",
            "previous_version",
            "created_at",
        )
        if item.get(key) is not None
    }


def _compact_step(value: Any) -> dict[str, Any]:
    item = as_dict(value)
    result = {
        key: item.get(key)
        for key in (
            "step_id",
            "goal_id",
            "capability",
            "tool_name",
            "status",
            "ordinal",
            "error_code",
            "started_at",
            "completed_at",
        )
        if item.get(key) is not None
    }
    output = item.get("output_artifact")
    if output:
        # Preserve the resource_refs list (SEARCH_RESULT outputs reference
        # every returned post) in addition to the primary resource identity so
        # follow-up GET_POST_DETAIL steps can resolve a real post_id from the
        # durable facts projection.
        compact = _compact_resource_ref(output)
        refs = [
            _compact_resource_ref(ref)
            for ref in (output.get("resource_refs") or [])
            if isinstance(ref, dict)
        ]
        if refs:
            compact["resource_refs"] = refs
        result["output_artifact"] = compact
    return result


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


_RESOURCE_TERMINAL_STATES = frozenset({
    "CANCELLED",
    "DELETED",
    "EXPIRED",
    "FAILED",
})
_RESOURCE_CURRENT_STATES = frozenset({
    "DRAFT",
    "READY",
    "SCHEDULED",
    "QUEUED",
    "PENDING",
    "ACTIVE",
    "RUNNING",
    "IN_PROGRESS",
})


def _upper(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_")


def _resource_identity(value: Mapping[str, Any]) -> tuple[str, str]:
    kind = _upper(
        value.get("resource_kind")
        or value.get("resource_type")
        or value.get("kind")
    )
    identifier = str(
        value.get("resource_id")
        or value.get("id")
        or value.get("draft_id")
        or value.get("schedule_id")
        or value.get("post_id")
        or ""
    ).strip()
    if kind in {"TASK", "ARTIFACT", "EXECUTION"}:
        return "", ""
    return kind, identifier


def _resource_label(value: Mapping[str, Any], fallback: str = "") -> str:
    return _text(
        value.get("semantic_label")
        or value.get("label")
        or value.get("title")
        or value.get("summary")
        or fallback,
        500,
    )


def _objective_lifecycle(status: Any) -> str:
    return "TERMINAL" if _upper(status) in _TERMINAL_STATES else "CURRENT"


def _resource_lifecycle(value: Mapping[str, Any], objective_statuses: Sequence[str]) -> str:
    """Derive resource state without borrowing Objective lifecycle.

    A completed Objective can still own an editable Draft.  Only explicit
    business resource state may make a resource terminal; an absent resource
    state is therefore not evidence that the resource is historical.
    """

    kind = _upper(value.get("resource_kind") or value.get("resource_type") or value.get("kind"))
    explicit_lifecycle = _upper(value.get("lifecycle"))
    if explicit_lifecycle in {"CURRENT", "HISTORICAL", "TERMINAL"}:
        return explicit_lifecycle
    state = _upper(value.get("state") or value.get("status"))
    if state in _RESOURCE_TERMINAL_STATES:
        return "TERMINAL"
    if kind == "SCHEDULE" and state in {"PUBLISHED", "COMPLETED"}:
        return "TERMINAL"
    if state in _RESOURCE_CURRENT_STATES or (
        kind == "POST" and state == "PUBLISHED"
    ):
        return "CURRENT"
    # ``objective_statuses`` is intentionally not consulted here.  Objective
    # lifecycle and Resource lifecycle are independent canonical facts.
    return "CURRENT"


def _compact_verified_outcome(value: Any) -> dict[str, Any]:
    """Keep only the bounded receipt fields useful to the next turn."""

    item = as_dict(value)
    result: dict[str, Any] = {}
    for key in (
        "execution_id",
        "task_id",
        "conversation_id",
        "goal_id",
        "capability",
        "status",
        "draft_id",
        "schedule_id",
        "post_id",
        "error",
        "observed_at",
        "source",
    ):
        if item.get(key) not in (None, ""):
            result[key] = item.get(key)
    refs = item.get("resource_refs")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
        result["resource_refs"] = [
            {
                key: ref.get(key)
                for key in ("resource_type", "resource_kind", "resource_id", "artifact_id")
                if ref.get(key) not in (None, "")
            }
            for ref in refs[:8]
            if isinstance(ref, Mapping)
        ]
    business = item.get("business_result")
    if isinstance(business, Mapping):
        compact_business = {
            key: business.get(key)
            for key in ("draft_id", "schedule_id", "post_id", "summary", "status", "state", "run_at")
            if business.get(key) not in (None, "")
        }
        schedule = business.get("schedule")
        if isinstance(schedule, Mapping):
            compact_business["schedule"] = {
                key: schedule.get(key)
                for key in ("schedule_id", "draft_id", "post_id", "status", "state", "run_at")
                if schedule.get(key) not in (None, "")
            }
        if compact_business:
            result["business_result"] = compact_business
    return result


def _outcome_resource_keys(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for field, kind in (("draft_id", "DRAFT"), ("schedule_id", "SCHEDULE"), ("post_id", "POST")):
        identifier = str(value.get(field) or "").strip()
        if identifier:
            keys.add((kind, identifier))
    for ref in value.get("resource_refs") or ():
        if not isinstance(ref, Mapping):
            continue
        kind, identifier = _resource_identity(ref)
        if kind and identifier:
            keys.add((kind, identifier))
    business = value.get("business_result")
    if isinstance(business, Mapping):
        for field, kind in (("draft_id", "DRAFT"), ("schedule_id", "SCHEDULE"), ("post_id", "POST")):
            identifier = str(business.get(field) or "").strip()
            if identifier:
                keys.add((kind, identifier))
        schedule = business.get("schedule")
        if isinstance(schedule, Mapping):
            for field, kind in (("draft_id", "DRAFT"), ("schedule_id", "SCHEDULE"), ("post_id", "POST")):
                identifier = str(schedule.get(field) or "").strip()
                if identifier:
                    keys.add((kind, identifier))
    return keys


def _outcome_for_resource(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    key: tuple[str, str],
    task_id: str,
    objective_id: str,
) -> dict[str, Any] | None:
    for outcome in outcomes:
        if key in _outcome_resource_keys(outcome):
            return dict(outcome)
    for outcome in outcomes:
        if task_id and str(outcome.get("task_id") or "") == task_id:
            if not objective_id or str(outcome.get("goal_id") or "") == objective_id:
                return dict(outcome)
    return None


def _reference_evidence(
    user_input: str,
    resources: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    objective_statuses: Mapping[str, str] | None = None,
) -> tuple[Any | None, list[tuple[str, str]], dict[str, Any]]:
    """Extract reference features and return all evidence-supported matches.

    This function never ranks or selects a target.  It only narrows a bounded
    projection; cardinality is preserved for TargetResolver's 0/1/N rule.
    """

    if not user_input or not user_input.strip():
        return None, [], {}
    if _is_unqualified_create(user_input):
        # A noun such as "Java 草稿" is an output description in a normal
        # CREATE request, not a reference to an existing Draft.  Do not let a
        # weak extractor feature narrow the current context in that case.
        return None, [], {}
    try:
        from ..command.reference_extractor import ReferenceExtractor

        feature = ReferenceExtractor().extract(user_input)
    except Exception:  # pragma: no cover - a projection must remain best effort
        feature = None
    fallback = _natural_reference_feature(user_input)
    fallback_topic = str(getattr(fallback, "topic", "") or "").strip()
    if fallback is not None and (
        feature is None
        or (
            not str(getattr(feature, "topic", "") or "").strip()
            and _is_semantic_reference_topic(fallback_topic)
        )
    ):
        # The checked-in legacy extractor is still useful for structured
        # English/typed references.  This bounded Unicode fallback covers the
        # same generic grammar for natural CJK input without selecting a
        # resource or changing the Interpreter prompt contract.
        feature = fallback
    if feature is None:
        return None, [], {}
    kind = _upper(getattr(getattr(feature, "kind", None), "value", getattr(feature, "kind", "")))
    topic = str(getattr(feature, "topic", "") or "").strip().casefold()
    identifier = str(getattr(feature, "id", "") or "").strip()
    explicit_marker = _has_reference_marker(user_input)
    kind_hint = _reference_kind_hint(user_input)
    if kind in {"", "TASK"} and kind_hint:
        kind = kind_hint
    matches: list[tuple[str, str]] = []
    for key, value in resources.items():
        resource_kind, resource_id = key
        if identifier:
            if resource_id == identifier:
                matches.append(key)
            continue
        if kind not in {"", "TASK"} and resource_kind != kind:
            continue
        labels = " ".join(
            str(value.get(field) or "")
            for field in ("semantic_label", "label", "title", "task_label", "goal", "description")
        ).casefold()
        if topic:
            if topic in labels:
                matches.append(key)
        else:
            # A typed/anaphoric reference with no topic still has bounded
            # evidence: scan every resource of that kind.  Cardinality is
            # preserved for TargetResolver's 0/1/N rule; no recency ranking
            # or latest fallback occurs here.
            if _is_failed_reference(user_input):
                owner_id = str(value.get("owner_objective_id") or "")
                if (
                    not objective_statuses
                    or str(objective_statuses.get(owner_id) or "").upper() == "FAILED"
                ):
                    matches.append(key)
            else:
                matches.append(key)
    # A single extractor feature is not enough for a natural multi-objective
    # turn (for example Java + Agent + Redis).  When the user supplied an
    # explicit reference marker, add every canonical resource whose semantic
    # label is actually mentioned.  This broadens to the user's stated
    # package; it never proves ownership or ranks a target.
    if explicit_marker:
        for key, value in resources.items():
            resource_kind, _resource_id = key
            if kind not in {"", "TASK"} and resource_kind != kind:
                continue
            labels = " ".join(
                str(value.get(field) or "")
                for field in (
                    "semantic_label",
                    "label",
                    "title",
                    "task_label",
                    "goal",
                    "description",
                )
            )
            if _semantic_label_mentioned(user_input, labels):
                matches.append(key)
    matches = list(dict.fromkeys(matches))
    if (
        not matches
        and not identifier
        and not explicit_marker
    ):
        # A weak property extraction such as "只保留草稿" is an action
        # constraint, not a reference to an existing resource.  Leave the
        # full bounded context intact for that turn.
        return None, [], {}
    if not matches and not identifier and not resources:
        # An empty conversation has no canonical candidate to recall.  Keep
        # ordinary creation language on the normal CREATE path; the durable
        # resolver still fail-closes any mutation that lacks a target.
        return None, [], {}
    counts: dict[str, int] = {}
    for resource_kind, _resource_id in matches:
        counts[resource_kind] = counts.get(resource_kind, 0) + 1
    evidence = {
        "reference_kind": kind or "TASK",
        "semantic_label": str(getattr(feature, "topic", "") or ""),
        "reference_type": _upper(
            getattr(getattr(feature, "reference_type", None), "value", getattr(feature, "reference_type", "NONE"))
        ),
        "evidence_sources": ["reference_extractor"],
        # Resource aliases in one Task (a Draft plus its Schedule) are one
        # target package; peers of the same kind remain ambiguous.
        "candidate_cardinality": max(counts.values(), default=0),
        "matched_resource_kinds": sorted({item[0] for item in matches}),
    }
    return feature, matches, evidence


def _has_reference_marker(text: str) -> bool:
    raw = str(text or "")
    return any(
        marker in raw
        for marker in (
            "\u90a3\u7bc7",   # 那篇
            "\u8fd9\u7bc7",   # 这篇
            "\u521a\u624d",   # 刚才
            "\u521a\u521a",   # 刚刚
            "\u4e4b\u524d",   # 之前
            "\u90a3\u4e2a",   # 那个
            "\u8fd9\u4e2a",   # 这个
            "\u7b2c",          # 第...篇
            "\u5931\u8d25",    # 失败的那个
            "\u6ca1\u6210\u529f",  # 没成功的那个
        )
    )


def _is_failed_reference(text: str) -> bool:
    raw = str(text or "")
    return any(marker in raw for marker in ("\u5931\u8d25", "\u6ca1\u6210\u529f"))


def _reference_kind_hint(text: str) -> str:
    raw = str(text or "")
    if "\u8349\u7a3f" in raw:
        return "DRAFT"
    if any(marker in raw for marker in ("\u5b9a\u65f6", "\u53d1\u5e03\u65f6\u95f4", "\u5b89\u6392\u53d1\u5e03")):
        return "SCHEDULE"
    if any(marker in raw for marker in ("\u5e16\u5b50", "\u6587\u7ae0", "\u5185\u5bb9")):
        return "POST"
    return ""


def _is_semantic_reference_topic(topic: str) -> bool:
    value = str(topic or "").strip()
    if not value:
        return False
    # Fallback extraction must produce a subject, not a conversational
    # modifier such as "刚刚定时的" or "只保留".
    return not any(
        marker in value
        for marker in (
            "\u521a\u624d",
            "\u521a\u521a",
            "\u5b9a\u65f6",
            "\u53d1\u5e03\u65f6\u95f4",
            "\u5b89\u6392\u53d1\u5e03",
            "\u53ea\u4fdd\u7559",
            "\u5931\u8d25",
            "\u7684",
        )
    )


def _semantic_label_mentioned(user_input: str, label: str) -> bool:
    raw = str(user_input or "").casefold()
    if not raw:
        return False
    terms = re.findall(
        r"[a-z][a-z0-9._-]{1,40}|[\u4e00-\u9fff]{2,16}",
        str(label or "").casefold(),
    )
    ignored = {
        "\u8349\u7a3f",  # 草稿
        "\u6587\u7ae0",  # 文章
        "\u5e16\u5b50",  # 帖子
        "\u5185\u5bb9",  # 内容
        "\u5b9a\u65f6",  # 定时
        "\u53d1\u5e03",  # 发布
    }
    label_value = str(label or "").casefold()
    ascii_terms = re.findall(r"[a-z][a-z0-9._-]{1,40}", label_value)
    if any(term not in ignored and term in raw for term in ascii_terms):
        return True

    # Two-character words such as "实践" or "内容" are shared by many
    # resources and cannot be reference evidence.  Sliding windows preserve
    # distinctive subjects such as "消息队列" without matching a generic
    # suffix from every title.
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,16}", label_value):
        for width in range(min(8, len(chunk)), 2, -1):
            for start in range(0, len(chunk) - width + 1):
                term = chunk[start : start + width]
                if term in raw and term not in ignored:
                    return True
    return False


def _natural_reference_feature(text: str) -> Any | None:
    """Small Unicode-safe feature fallback for ordinary user references.

    It is intentionally feature-only.  Candidate selection and 0/1/N
    resolution remain in the existing TargetResolver.
    """

    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    # The legacy extractor has older code-page literals in some deployments.
    # Keep this projection fallback Unicode-safe for the natural references
    # that must be bounded before the provider/resolver split.
    safe_match = re.search(
        r"(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40})"
        r"\s*(?:\u90a3\u7bc7|\u8fd9\u7bc7|\u8349\u7a3f|\u6587\u7ae0|\u5185\u5bb9|\u5e16\u5b50)",
        raw,
    )
    if safe_match is None:
        safe_match = re.search(
            r"(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40}|[\u4e00-\u9fff]{2,12})"
            r"\s*(?:\u90a3\u7bc7|\u8fd9\u7bc7|\u90a3\u7bc7\u6587\u7ae0|\u8fd9\u7bc7\u6587\u7ae0)",
            raw,
        )
    if safe_match is None:
        safe_match = re.search(
            r"(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40}|[\u4e00-\u9fff]{2,12})"
            r"\s*(?:\u8349\u7a3f|\u6587\u7ae0|\u5185\u5bb9|\u5e16\u5b50)",
            raw,
        )
    if safe_match is not None:
        topic = str(safe_match.group("topic") or "").strip()
        for prefix in (
            "\u90a3\u7bc7",
            "\u8fd9\u7bc7",
            "\u521a\u624d\u7684",
            "\u521a\u521a\u7684",
            "\u4e4b\u524d\u7684",
        ):
            if topic.startswith(prefix):
                topic = topic[len(prefix) :].strip()
        if topic:
            if "\u8349\u7a3f" in raw:
                kind = "DRAFT"
            elif any(
                word in raw
                for word in (
                    "\u5b9a\u65f6",
                    "\u53d1\u5e03\u65f6\u95f4",
                    "\u53d1\u5e03\u8ba1\u5212",
                    "\u5b89\u6392\u53d1\u5e03",
                )
            ):
                kind = "SCHEDULE"
            elif any(word in raw for word in ("\u5e16\u5b50", "\u6587\u7ae0", "\u5185\u5bb9")):
                kind = "POST"
            else:
                kind = "TASK"
            return SimpleNamespace(
                kind=kind,
                id=None,
                topic=topic,
                reference_type="PROPERTY",
                raw=raw,
            )
    match = re.search(
        r"(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40}|[\u4e00-\u9fff]{2,12})"
        r"\s*(?:那篇|这篇|那篇文章|这篇文章)",
        raw,
    )
    if match is None:
        if not any(
            marker in raw
            for marker in (
                "刚才",
                "刚刚",
                "之前",
                "改",
                "修改",
                "安排",
                "发布时间",
                "取消",
                "发布",
                "删除",
                "补充",
                "更新",
                "定时",
            )
        ):
            return None
        match = re.search(
            r"(?:那篇|这篇)(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40}?|[\u4e00-\u9fff]{2,12}?)"
            r"\s*(?:草稿|文章|内容|帖子)",
            raw,
        )
    if match is None:
        match = re.search(
            r"(?P<topic>[A-Za-z][A-Za-z0-9._-]{1,40}|[\u4e00-\u9fff]{2,12})"
            r"\s*(?:草稿|文章|内容|帖子)",
            raw,
        )
    if match is None:
        return None
    topic = str(match.group("topic") or "").strip()
    if not topic:
        return None
    if "草稿" in raw:
        kind = "DRAFT"
    elif any(word in raw for word in ("定时", "发布时间", "发布计划", "安排发布")):
        kind = "SCHEDULE"
    elif any(word in raw for word in ("帖子", "文章", "内容")):
        kind = "POST"
    else:
        kind = "TASK"
    return SimpleNamespace(
        kind=kind,
        id=None,
        topic=topic,
        reference_type="PROPERTY",
        raw=raw,
    )


def _is_unqualified_create(text: str) -> bool:
    raw = " ".join(str(text or "").split()).casefold()
    create_markers = (
        "写一篇",
        "写一份",
        "帮我写",
        "创建",
        "生成",
        "起草",
        "create ",
        "draft ",
    )
    reference_markers = (
        "那篇",
        "这篇",
        "刚才",
        "刚刚",
        "之前",
        "那个",
        "这个",
        "失败的",
        "第",
    )
    return any(marker in raw for marker in create_markers) and not any(
        marker in raw for marker in reference_markers
    )


def derive_conversation_context(
    snapshot: ContextSnapshot | Mapping[str, Any] | Any,
    *,
    user_input: str = "",
    focus_task_ids: Sequence[str] = (),
    resource_limit: int = 30,
    objective_limit: int = 12,
    outcome_limit: int = 8,
) -> DerivedConversationContext:
    """Build the one scoped context consumed by Interpreter and Resolver.

    Resource identity and ownership are copied from the existing projected
    TaskResourceRef/Objective fields.  ``target_objective_id`` and explicit
    result/artifact relations only add derived lineage; labels and recency
    never prove ownership.
    """

    source = as_dict(snapshot)
    task_rows = [dict(item) for item in source.get("active_tasks") or () if isinstance(item, Mapping)]
    task_by_id = {
        str(task.get("task_id") or ""): task
        for task in task_rows
        if str(task.get("task_id") or "")
    }
    objectives: list[dict[str, Any]] = []
    objective_by_id: dict[str, dict[str, Any]] = {}
    for task in task_rows:
        task_id = str(task.get("task_id") or "")
        for raw in task.get("objectives") or ():
            if not isinstance(raw, Mapping):
                continue
            objective = dict(raw)
            objective.setdefault("task_id", task_id)
            objective_id = str(objective.get("objective_id") or "")
            if not objective_id:
                continue
            objectives.append(objective)
            objective_by_id[objective_id] = objective

    resources: dict[tuple[str, str], dict[str, Any]] = {}

    def add_resource(raw: Mapping[str, Any], *, task_id: str = "", fallback_label: str = "") -> None:
        kind, identifier = _resource_identity(raw)
        if not kind or not identifier:
            return
        value = dict(resources.get((kind, identifier)) or {})
        for key, item in raw.items():
            if item not in (None, "", {}, []):
                value.setdefault(str(key), item)
        value.update({
            "resource_id": identifier,
            "resource_kind": kind,
        })
        if task_id:
            value.setdefault("task_id", task_id)
        if fallback_label and not _resource_label(value):
            value["semantic_label"] = fallback_label
        resources[(kind, identifier)] = value

    for task in task_rows:
        task_id = str(task.get("task_id") or "")
        fallback = _text(task.get("goal") or task.get("goal_summary"), 500)
        for raw in task.get("resource_index") or ():
            if isinstance(raw, Mapping):
                add_resource(raw, task_id=task_id, fallback_label=fallback)
        for raw in task.get("artifacts") or ():
            if isinstance(raw, Mapping):
                add_resource(raw, task_id=task_id, fallback_label=fallback)
    for raw in source.get("available_resources") or ():
        if isinstance(raw, Mapping):
            add_resource(raw, task_id=str(raw.get("task_id") or ""))
    for raw in source.get("artifacts") or ():
        if isinstance(raw, Mapping):
            add_resource(raw, task_id=str(raw.get("task_id") or ""))

    raw_outcomes: list[dict[str, Any]] = []
    for raw in source.get("recent_verified_outcomes") or ():
        compact = _compact_verified_outcome(raw)
        if compact.get("status"):
            compact.setdefault("source", "action_observation")
            raw_outcomes.append(compact)
    for raw in source.get("recent_operations") or ():
        if not isinstance(raw, Mapping) or not _upper(raw.get("status")):
            continue
        if _upper(raw.get("status")) not in {
            "COMPLETED", "FAILED", "CANCELLED", "RESULT_UNKNOWN", "VERIFIED_COMPLETED",
        }:
            continue
        compact = _compact_verified_outcome({**raw, "source": "operation_projection"})
        raw_outcomes.append(compact)
    for raw in source.get("execution_states") or ():
        if not isinstance(raw, Mapping):
            continue
        if _upper(raw.get("status")) not in {"RESULT_UNKNOWN", "COMPLETED", "FAILED", "CANCELLED"}:
            continue
        raw_outcomes.append(_compact_verified_outcome({**raw, "source": "execution_projection"}))
    outcomes_by_key: dict[str, dict[str, Any]] = {}
    for outcome in raw_outcomes:
        key = str(outcome.get("execution_id") or "")
        if not key:
            key = f"{outcome.get('task_id', '')}:{outcome.get('status', '')}:{len(outcomes_by_key)}"
        existing = outcomes_by_key.get(key)
        if existing is None or existing.get("source") != "action_observation":
            outcomes_by_key[key] = outcome
    outcomes = list(outcomes_by_key.values())[: max(0, outcome_limit)]

    # Explicit resource refs are canonical evidence for observations and
    # artifacts; adding the row here only fills a projection gap before the
    # relation graph is built.  It does not create a new resource truth.
    for outcome in outcomes:
        for ref in outcome.get("resource_refs") or ():
            if isinstance(ref, Mapping):
                add_resource(ref, task_id=str(outcome.get("task_id") or ""))
        for field, kind in (("draft_id", "DRAFT"), ("schedule_id", "SCHEDULE"), ("post_id", "POST")):
            identifier = str(outcome.get(field) or "").strip()
            if identifier:
                add_resource({"resource_id": identifier, "resource_kind": kind}, task_id=str(outcome.get("task_id") or ""))

    direct_owners: dict[tuple[str, str], set[str]] = {}
    related_owners: dict[tuple[str, str], set[str]] = {}
    for key, resource in resources.items():
        owner = str(resource.get("objective_id") or "").strip()
        if owner:
            direct_owners.setdefault(key, set()).add(owner)
    for objective in objectives:
        objective_id = str(objective.get("objective_id") or "")
        for resource_id in objective.get("related_resource_ids") or ():
            rid = str(resource_id or "").strip()
            for key in resources:
                if key[1] == rid:
                    related_owners.setdefault(key, set()).add(objective_id)

    target_by_objective = {
        str(objective.get("objective_id")): str(
            (objective.get("constraints") or {}).get("target_objective_id")
            or ""
        )
        for objective in objectives
        if str(objective.get("objective_id") or "")
    }
    children: dict[str, set[str]] = {}
    for child, parent in target_by_objective.items():
        if parent:
            children.setdefault(parent, set()).add(child)

    def lineage(root: str) -> list[str]:
        if not root:
            return []
        seen: list[str] = []
        pending = [root]
        while pending:
            current = pending.pop(0)
            if not current or current in seen:
                continue
            seen.append(current)
            parent = target_by_objective.get(current, "")
            if parent:
                pending.append(parent)
            pending.extend(sorted(children.get(current, set())))
        return seen

    owner_by_resource: dict[tuple[str, str], str] = {}
    owner_evidence: dict[tuple[str, str], str] = {}
    for key in resources:
        direct = direct_owners.get(key, set())
        fallback = related_owners.get(key, set())
        if len(direct) == 1:
            owner_by_resource[key] = next(iter(direct))
            owner_evidence[key] = "TaskResourceRef.objective_id"
        elif len(direct) > 1:
            owner_evidence[key] = "ambiguous_owner_evidence"
        elif len(fallback) == 1:
            owner_by_resource[key] = next(iter(fallback))
            owner_evidence[key] = "Objective.related_resource_ids"
        elif len(fallback) > 1:
            owner_evidence[key] = "ambiguous_owner_evidence"

    # Direct result/artifact evidence can connect a Schedule to its Draft even
    # when the two rows are owned by different mutation Objectives.
    relations: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def connect(left: tuple[str, str], right: tuple[str, str], relation: str, source_name: str) -> None:
        if left == right or left not in resources or right not in resources:
            return
        entry = {
            "resource_id": right[1],
            "resource_kind": right[0],
            "semantic_label": _resource_label(resources[right]),
            "relation": relation,
            "evidence_sources": [source_name],
        }
        existing = relations.setdefault(left, [])
        for item in existing:
            if item.get("resource_id") == right[1] and item.get("resource_kind") == right[0]:
                sources = list(item.get("evidence_sources") or [])
                if source_name not in sources:
                    sources.append(source_name)
                item["evidence_sources"] = sources
                return
        existing.append(entry)

    for key, resource in resources.items():
        for field, kind, relation in (
            ("draft_id", "DRAFT", "schedule_for_draft"),
            ("schedule_id", "SCHEDULE", "draft_for_schedule"),
            ("post_id", "POST", "publication_for_post"),
        ):
            identifier = str(resource.get(field) or "").strip()
            if identifier and (kind, identifier) in resources:
                connect(key, (kind, identifier), relation, "explicit_resource_ref")
                connect((kind, identifier), key, relation, "explicit_resource_ref")
        for ref in resource.get("resource_refs") or ():
            if isinstance(ref, Mapping):
                ref_key = _resource_identity(ref)
                if ref_key in resources:
                    connect(key, ref_key, "artifact_resource_ref", "Artifact.resource_refs")

    for outcome in outcomes:
        outcome_keys = [key for key in _outcome_resource_keys(outcome) if key in resources]
        for left in outcome_keys:
            for right in outcome_keys:
                if left[0] == "SCHEDULE" and right[0] == "DRAFT":
                    connect(left, right, "verified_schedule_draft", "ActionObservation.business_result")
                    connect(right, left, "verified_schedule_draft", "ActionObservation.business_result")
                if left[0] == "POST" and right[0] == "DRAFT":
                    connect(left, right, "verified_publication_draft", "ActionObservation.business_result")
                    connect(right, left, "verified_publication_draft", "ActionObservation.business_result")

    # Objective relations and target_objective_id form lineage evidence, not
    # semantic similarity.  Connect only resources with an explicit owner or
    # an explicit related_resource_ids/target_objective_id chain.
    for left, left_owner in owner_by_resource.items():
        left_lineage = set(lineage(left_owner))
        for right, right_owner in owner_by_resource.items():
            if left == right or not right_owner:
                continue
            right_lineage = set(lineage(right_owner))
            if left_owner == right_owner:
                connect(left, right, "objective_related_resources", owner_evidence.get(left, "objective_relation"))
            elif left_lineage.intersection(right_lineage):
                connect(left, right, "objective_lineage", "target_objective_id")

    resource_cards: dict[tuple[str, str], dict[str, Any]] = {}
    for key, resource in resources.items():
        owner = owner_by_resource.get(key, "")
        owner_objective = objective_by_id.get(owner, {})
        task_id = str(resource.get("task_id") or owner_objective.get("task_id") or "")
        objective_statuses = [
            _upper(item.get("status"))
            for item in objectives
            if str(item.get("task_id") or "") == task_id
        ]
        lifecycle = _resource_lifecycle(resource, objective_statuses)
        objective_lineage = lineage(owner)
        outcome = _outcome_for_resource(
            outcomes,
            key=key,
            task_id=task_id,
            objective_id=owner,
        )
        resource_cards[key] = {
            "resource_kind": key[0],
            "resource_id": key[1],
            "semantic_label": _resource_label(
                resource,
                _text(owner_objective.get("description") or task_by_id.get(task_id, {}).get("goal"), 500),
            ),
            "business_state": _text(
                resource.get("state") or resource.get("status") or lifecycle,
                120,
            ).upper(),
            "lifecycle": lifecycle,
            "task_id": task_id,
            "owner_objective_id": owner,
            "ownership_evidence": owner_evidence.get(key, "missing"),
            "objective_lineage": list(objective_lineage),
            "related_resources": list(relations.get(key, [])),
            "verified_outcome": outcome,
            "title": _resource_label(resource),
            "status": _text(resource.get("status"), 80).upper(),
            "run_at": resource.get("run_at") or resource.get("scheduled_at"),
        }

    feature, matched_keys, reference_card = _reference_evidence(
        user_input,
        resource_cards,
        objective_statuses={
            objective_id: _upper(objective.get("status"))
            for objective_id, objective in objective_by_id.items()
        },
    )
    explicit_reference = feature is not None
    package_task_ids: set[str]
    if explicit_reference:
        package_task_ids = {
            str(resource_cards[key].get("task_id") or "")
            for key in matched_keys
            if key in resource_cards and resource_cards[key].get("task_id")
        }
        # A generated title can differ from the user's semantic Objective;
        # Objective fields are allowed to widen the bounded package, but never
        # to prove a Resource lineage.
        topic = str(getattr(feature, "topic", "") or "").casefold()
        if topic:
            for objective in objectives:
                label = " ".join(
                    str(objective.get(field) or "")
                    for field in ("description", "intent")
                ).casefold()
                if topic in label:
                    package_task_ids.add(str(objective.get("task_id") or ""))
        candidate_keys = [
            key for key in matched_keys
            if resource_cards.get(key, {}).get("lifecycle") == "CURRENT"
        ]
        # Keep explicitly mentioned historical/terminal facts as bounded COLD
        # evidence, but do not expose them as a normal mutation candidate.
        relevant_keys = list(dict.fromkeys(candidate_keys + [
            key for key in matched_keys
            if resource_cards.get(key, {}).get("lifecycle") in {"HISTORICAL", "TERMINAL"}
        ]))
        reference_card["candidate_cardinality"] = len(candidate_keys)
        reference_card["matched_resource_kinds"] = sorted({key[0] for key in candidate_keys})
        reference_card["cold_candidate_cardinality"] = len(matched_keys) - len(candidate_keys)
        reference_card["evidence_sources"] = ["reference_extractor", "scoped_resource_projection"]
    else:
        package_task_ids = {
            str(item)
            for item in focus_task_ids
            if str(item or "")
        }
        if not package_task_ids:
            package_task_ids = set(task_by_id)
        relevant_keys = [
            key for key, card in resource_cards.items()
            if str(card.get("task_id") or "") in package_task_ids
            # WARM/COLD facts remain in the derived projection for lifecycle
            # explanation and deterministic filtering, but are removed from
            # the normal Interpreter/Resolver candidate view below.
            and card.get("lifecycle") in {"CURRENT", "HISTORICAL", "TERMINAL"}
        ]

    # Expand an explicit reference to its evidence-backed Task package.  The
    # package may contain the Draft and its Schedule/Post, but only relation
    # edges above can explain those additions.
    package_task_ids.update(
        str(resource_cards[key].get("task_id") or "")
        for key in relevant_keys
        if resource_cards.get(key, {}).get("task_id")
    )
    relevant_key_set = set(relevant_keys)
    for key, card in resource_cards.items():
        if str(card.get("task_id") or "") not in package_task_ids:
            continue
        if key in relevant_key_set:
            continue
        if explicit_reference and card.get("lifecycle") != "TERMINAL":
            # This is a WARM companion resource only when it is connected to a
            # matched resource by canonical relation evidence.
            if any(
                relation.get("resource_id") == key[1]
                and relation.get("resource_kind") == key[0]
                for matched in matched_keys
                for relation in resource_cards.get(matched, {}).get("related_resources") or ()
            ):
                relevant_keys.append(key)
                relevant_key_set.add(key)

    relevant_resources: list[dict[str, Any]] = []
    matched_set = set(matched_keys)
    for key in relevant_keys:
        card = dict(resource_cards[key])
        card["context_tier"] = "HOT" if key in matched_set else "WARM"
        if card.get("lifecycle") != "CURRENT":
            card["context_tier"] = "COLD"
        relevant_resources.append(card)
    relevant_resources = relevant_resources[: max(0, resource_limit)]

    relevant_objectives: list[dict[str, Any]] = []
    relevant_task_ids = {
        str(card.get("task_id") or "")
        for card in relevant_resources
        if card.get("task_id")
    } | package_task_ids
    for objective in objectives:
        if str(objective.get("task_id") or "") not in relevant_task_ids:
            continue
        objective_id = str(objective.get("objective_id") or "")
        owned_kinds = sorted({
            key[0]
            for key, owner in owner_by_resource.items()
            if owner in set(lineage(objective_id))
        })
        objective_outcome = next(
            (
                dict(item)
                for item in outcomes
                if str(item.get("goal_id") or "") == objective_id
            ),
            None,
        )
        constraints = objective.get("constraints") or {}
        relevant_objectives.append({
            "objective_id": objective_id,
            "task_id": str(objective.get("task_id") or ""),
            "semantic_label": _text(objective.get("description") or objective.get("intent"), 500),
            "status": _upper(objective.get("status")),
            "lifecycle": _objective_lifecycle(objective.get("status")),
            "target_objective_id": str(constraints.get("target_objective_id") or ""),
            "related_resource_kinds": owned_kinds,
            "verified_outcome": objective_outcome,
        })
    relevant_objectives = relevant_objectives[: max(0, objective_limit)]

    return DerivedConversationContext(
        relevant_resources=relevant_resources,
        relevant_objectives=relevant_objectives,
        recent_verified_outcomes=outcomes,
        reference_evidence=[reference_card] if explicit_reference else [],
    )


def project_interpreter_context(value: Any) -> dict[str, Any]:
    """Project durable context into semantic, non-canonical evidence.

    The resolver still receives the full ``CommandContext``.  This projection
    is only used while serializing context for the Interpreter provider, so a
    model can distinguish user-facing labels and business state without being
    invited to copy internal task/objective/resource identities into a
    semantic reference.  ``semantic_label`` is recall evidence only; it is not
    an identity and never resolves a target on its own.  ``verified_outcome``
    is emitted only when an existing Objective/ResourceRef/ActionObservation
    projection already carries that fact.
    """

    source = as_dict(value)
    target_values = (
        source.get("targets")
        or source.get("target_candidates")
        or source.get("available_resources")
        or []
    )
    result: dict[str, Any] = {
        "timezone": str(source.get("timezone") or "Asia/Shanghai"),
        "summary": source.get("summary"),
        # Conversation history contains verified artifacts and execution
        # receipts.  Keep the semantic evidence, but apply the same
        # recursive identity boundary as the rest of this provider view.
        "history": _safe_interpreter_value(list(source.get("history") or [])),
        "active_target": _interpreter_target(source.get("active_target")),
        "active_tasks": [
            _interpreter_task(item)
            for item in (source.get("active_tasks") or [])
            if isinstance(item, Mapping)
        ],
        "unfinished_goals": [
            _interpreter_goal(item)
            for item in (source.get("unfinished_goals") or [])
            if isinstance(item, Mapping)
        ],
        "targets": [
            _interpreter_resource(item)
            for item in target_values
            if isinstance(item, Mapping)
        ],
        "reference_evidence": _safe_interpreter_value(
            list(source.get("reference_evidence") or [])
        ),
        "recent_verified_outcomes": _safe_interpreter_value(
            list(source.get("recent_verified_outcomes") or [])
        ),
    }
    # Do not pass CommandContext.metadata through: from_any stores the full
    # snapshot there for resolver compatibility, and it contains the very
    # canonical ids this view is meant to hide.
    return result


def _interpreter_target(value: Any) -> dict[str, Any] | None:
    item = as_dict(value)
    if not item:
        return None
    result = {
        "kind": _text(item.get("kind"), 80).upper(),
        "label": _text(item.get("label") or item.get("title") or item.get("goal"), 500),
        "status": _text(item.get("status"), 80).upper(),
        "reference_type": _text(item.get("reference_type"), 80).upper(),
    }
    return {key: value for key, value in result.items() if value}


def _interpreter_resource(value: Mapping[str, Any]) -> dict[str, Any]:
    kind = _text(
        value.get("resource_kind")
        or value.get("resource_type")
        or value.get("kind"),
        80,
    ).upper()
    label = _text(
        value.get("semantic_label")
        or value.get("label")
        or value.get("title")
        or value.get("goal")
        or value.get("summary"),
        500,
    )
    status = _text(value.get("status"), 80).upper()
    lifecycle = _text(value.get("lifecycle"), 80).upper()
    current_state = _text(
        value.get("current_state")
        or value.get("lifecycle")
        or value.get("state")
        or value.get("status"),
        120,
    ).upper()
    historical = lifecycle in {"HISTORICAL", "TERMINAL"} or status in _TERMINAL_STATES
    relation = _text(value.get("relation"), 120).lower()
    if not relation:
        if status == "FAILED":
            relation = "failed_previous_turn"
        elif historical:
            relation = "historical_resource"
        else:
            relation = "conversation_resource"
    result = {
        "kind": kind,
        "resource_kind": kind,
        "semantic_label": label,
        "title": _text(value.get("title") or label, 500),
        "label": label,
        "status": status,
        "current_state": current_state,
        "lifecycle": lifecycle or ("HISTORICAL" if historical else "CURRENT"),
        "current": not historical,
        "historical": historical,
        "run_at": value.get("run_at") or value.get("scheduled_at"),
        "business_state": current_state,
        "relation": relation,
    }
    verified = _verified_outcome(value)
    if verified is not None:
        result["verified_outcome"] = verified
    return {key: item for key, item in result.items() if item not in (None, "")}


def _interpreter_goal(value: Mapping[str, Any]) -> dict[str, Any]:
    status = _text(value.get("status"), 80).upper()
    lifecycle = _text(value.get("lifecycle"), 80).upper()
    description = _text(
        value.get("description") or value.get("intent") or value.get("goal"),
        1200,
    )
    historical = lifecycle in {"HISTORICAL", "TERMINAL"} or status in _TERMINAL_STATES
    relation = _text(value.get("relation"), 120).lower()
    if not relation:
        if status == "FAILED":
            relation = "failed_previous_turn"
        else:
            relation = "historical_objective" if historical else "current_objective"
    result = {
        "semantic_label": _text(value.get("semantic_label") or description, 500),
        "title": _text(value.get("title") or description, 500),
        "description": description,
        "operation": _text(value.get("intent") or value.get("operation"), 120),
        "resource_kind": _text(
            value.get("expected_resource_kind") or value.get("resource_kind"),
            80,
        ).upper(),
        "status": status,
        "current_state": _text(value.get("current_state") or status, 120).upper(),
        "lifecycle": lifecycle or ("TERMINAL" if historical else "CURRENT"),
        "current": not historical,
        "historical": historical,
        "relation": relation,
    }
    desired_outcome = value.get("desired_outcome")
    if desired_outcome:
        result["desired_outcome"] = _text(desired_outcome, 120)
    outcome = desired_outcome or value.get("outcome")
    constraints = value.get("constraints")
    if historical:
        if outcome:
            result["historical_outcome"] = _text(outcome, 120)
        if isinstance(constraints, Mapping) and constraints:
            result["historical_constraints"] = _safe_interpreter_value(constraints)
    else:
        if outcome:
            result["current_outcome"] = _text(outcome, 120)
        if isinstance(constraints, Mapping) and constraints:
            result["current_constraints"] = _safe_interpreter_value(constraints)
    related_kinds = value.get("related_resource_kinds") or value.get("resource_kinds")
    if isinstance(related_kinds, list) and related_kinds:
        result["resource_kinds"] = [str(item).upper() for item in related_kinds[:10]]
    verified = _verified_outcome(value)
    if verified is not None:
        result["verified_outcome"] = verified
        result["historical_verified_outcome" if historical else "current_verified_outcome"] = verified
    return {key: item for key, item in result.items() if item not in (None, "")}


def _interpreter_task(value: Mapping[str, Any]) -> dict[str, Any]:
    status = _text(value.get("status"), 80).upper()
    historical = status in _TERMINAL_STATES
    label = _text(
        value.get("semantic_label") or value.get("goal") or value.get("goal_summary"),
        500,
    )
    result = {
        "kind": "TASK",
        "semantic_label": label,
        "title": _text(value.get("title") or label, 500),
        "label": label,
        "status": status,
        "current_state": _text(value.get("phase") or status, 120).upper(),
        "current": not historical,
        "historical": historical,
        "business_state": _text(value.get("phase") or value.get("status"), 120).upper(),
        "objectives": [
            _interpreter_goal(item)
            for item in (value.get("objectives") or value.get("goals") or [])
            if isinstance(item, Mapping)
        ][:40],
        "resources": [
            _interpreter_resource(item)
            for item in (value.get("resource_index") or value.get("resources") or [])
            if isinstance(item, Mapping)
        ][:40],
        "relation": "conversation_task",
    }
    return {key: item for key, item in result.items() if item not in (None, "")}


__all__ = [
    "as_dict",
    "derive_conversation_context",
    "project_artifact",
    "project_execution",
    "project_goal",
    "project_interpreter_context",
    "project_task",
]
