"""Resolve durable Runtime facts into the existing presentation input contract."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..models.runtime_result import RuntimeResult


class ResultResolver:
    """Build a body-free RuntimeResult from Execution and ArtifactStore facts."""

    def __init__(self, *, artifact_store: Any | None = None) -> None:
        self._artifact_store = artifact_store

    def resolve(
        self,
        result: RuntimeResult,
        *,
        execution: Any | None = None,
    ) -> RuntimeResult:
        resolved = deepcopy(result)
        execution_id = str(resolved.execution_id or getattr(execution, "execution_id", ""))
        if execution_id:
            resolved.execution_id = execution_id

        persisted = self._persisted_artifacts(execution_id)
        incoming = [
            item for item in (_safe_artifact_payload(raw) for raw in resolved.artifacts)
            if item is not None
        ]
        artifacts = _merge_artifacts(persisted, incoming)
        resolved.artifacts = artifacts
        resolved.artifact_ids = [
            str(item["artifact_id"])
            for item in artifacts
            if item.get("artifact_id")
        ]

        draft = next((item for item in artifacts if _kind(item) == "DRAFT"), None)
        schedule = next((item for item in artifacts if _kind(item) == "SCHEDULE"), None)
        post = next((item for item in artifacts if _kind(item) == "POST"), None)
        if draft and draft.get("resource_id"):
            resolved.draft_id = str(draft["resource_id"])
        if schedule and schedule.get("resource_id"):
            resolved.schedule_id = str(schedule["resource_id"])
            resolved.schedule = {
                key: value
                for key, value in schedule.items()
                if key in {"resource_id", "resource_type", "status", "run_at", "timezone"}
                and value is not None
            }
            resolved.schedule["schedule_id"] = str(schedule["resource_id"])
            if draft and draft.get("resource_id"):
                resolved.schedule["draft_id"] = str(draft["resource_id"])
        if post and post.get("resource_id"):
            resolved.side_effect_committed = True

        if execution is not None and not resolved.steps:
            resolved.steps = [_step_payload(step) for step in getattr(execution, "steps", [])]
        if str(resolved.status).upper() == "COMPLETED" and str(resolved.content).startswith(
            ("已完成：", "执行已完成：")
        ):
            # Retire the legacy echo result while preserving meaningful custom results.
            resolved.content = ""
        return resolved

    def _persisted_artifacts(self, execution_id: str) -> list[dict[str, Any]]:
        if not execution_id or self._artifact_store is None:
            return []
        finder = getattr(self._artifact_store, "find_by_execution", None)
        if finder is None:
            return []
        return [_artifact_payload(item) for item in finder(execution_id)]


def _artifact_payload(artifact: Any) -> dict[str, Any]:
    metadata = getattr(artifact, "metadata", {}) or {}
    projection = metadata.get("projection") if isinstance(metadata, dict) else {}
    if not isinstance(projection, dict):
        projection = {}
    artifact_type = str(getattr(artifact, "artifact_type", ""))
    resource_type = (
        getattr(artifact, "resource_type", None)
        or getattr(artifact, "resource_kind", None)
        or _resource_type(artifact_type)
    )
    return {
        "artifact_id": str(getattr(artifact, "artifact_id", "")),
        "artifact_type": artifact_type,
        "type": artifact_type,
        "resource_type": resource_type,
        "resource_id": getattr(artifact, "resource_id", None) or projection.get("resource_id"),
        "title": getattr(artifact, "title", None) or projection.get("title"),
        "summary": getattr(artifact, "summary", "") or projection.get("summary") or "",
        "status": getattr(artifact, "status", None) or projection.get("status"),
        "run_at": getattr(artifact, "run_at", None) or projection.get("run_at"),
        "timezone": getattr(artifact, "timezone", None) or projection.get("timezone"),
        "receipt_id": projection.get("receipt_id"),
        "external_operation_id": projection.get("external_operation_id"),
        "resource_refs": projection.get("resource_refs", []),
        "tool_call_id": projection.get("tool_call_id"),
        "artifact_ref": str(getattr(artifact, "artifact_id", "")),
        "step_id": str(getattr(artifact, "step_id", "")),
    }


def _safe_artifact_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    data = raw.get("data") or raw.get("payload") or {}
    if not isinstance(data, dict):
        data = {}
    artifact_type = str(raw.get("artifact_type") or raw.get("type") or "")
    resource_type = str(
        raw.get("resource_type") or raw.get("resource_kind") or _resource_type(artifact_type) or ""
    ) or None
    resource_key = {
        "DRAFT": "draft_id",
        "SCHEDULE": "schedule_id",
        "POST": "post_id",
    }.get(resource_type or "")
    resource_id = raw.get("resource_id")
    if not resource_id and resource_key:
        resource_id = data.get(resource_key)
    return {
        "artifact_id": str(raw.get("artifact_id") or ""),
        "artifact_type": artifact_type,
        "type": artifact_type,
        "resource_type": resource_type,
        "resource_id": _optional_text(resource_id),
        "title": _optional_text(raw.get("title") or data.get("title")),
        "summary": _optional_text(
            raw.get("summary") or data.get("summary") or data.get("description")
        ) or "",
        "status": _optional_text(raw.get("status") or data.get("status")),
        "run_at": _optional_text(raw.get("run_at") or data.get("run_at")),
        "timezone": _optional_text(raw.get("timezone") or data.get("timezone")),
        "receipt_id": _optional_text(raw.get("receipt_id") or data.get("receipt_id")),
        "external_operation_id": _optional_text(
            raw.get("external_operation_id") or data.get("external_operation_id")
        ),
        "resource_refs": raw.get("resource_refs") or data.get("resource_refs") or [],
        "tool_call_id": _optional_text(raw.get("tool_call_id") or data.get("tool_call_id")),
        "artifact_ref": _optional_text(raw.get("artifact_ref")),
        "step_id": str(raw.get("step_id") or ""),
    }


def _merge_artifacts(
    persisted: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], int] = {}
    for raw in [*persisted, *incoming]:
        key = (
            str(raw.get("artifact_id") or ""),
            str(raw.get("artifact_type") or raw.get("type") or ""),
        )
        if key in by_key:
            index = by_key[key]
            merged[index] = {
                field: value
                for field, value in {**merged[index], **raw}.items()
                if value not in (None, "")
            }
        else:
            by_key[key] = len(merged)
            merged.append(raw)
    return merged


def _step_payload(step: Any) -> dict[str, Any]:
    status = getattr(getattr(step, "status", ""), "value", getattr(step, "status", ""))
    return {
        "step_id": str(getattr(step, "step_id", "")),
        "capability": str(getattr(step, "capability", "")),
        "status": str(status),
        "retry_count": int(getattr(step, "retry_count", 0)),
        "error_code": str(getattr(step, "error_code", "") or ""),
        "error_message": str(getattr(step, "error_message", "") or ""),
        "started_at": str(getattr(step, "started_at", "") or ""),
        "completed_at": str(getattr(step, "completed_at", "") or ""),
    }


def _kind(raw: dict[str, Any]) -> str | None:
    return str(
        raw.get("resource_type")
        or _resource_type(str(raw.get("artifact_type") or raw.get("type") or ""))
        or ""
    ) or None


def _resource_type(artifact_type: str) -> str | None:
    normalized = str(artifact_type).upper()
    if normalized in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
        return "DRAFT"
    if normalized in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
        return "SCHEDULE"
    if normalized in {"POST", "PUBLISHED_POST", "PUBLICATION"}:
        return "POST"
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


__all__ = ["ResultResolver"]
