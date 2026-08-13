"""Deterministic projections from durable facts into ContextSnapshot fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
            "title",
            "status",
            "scheduled_at",
            "updated_at",
            "task_id",
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
        result["output_artifact"] = _compact_resource_ref(output)
    return result


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


__all__ = ["as_dict", "project_artifact", "project_execution", "project_goal", "project_task"]
