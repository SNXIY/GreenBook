"""Deterministic projection from Runtime facts to UserActivity events.

This module intentionally has no LLM, tool, Java client, queue, or state
mutation dependency.  It can only translate facts that have already occurred
into a public-safe representation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from greenbook_contracts.tool_contract import semantic_action_for_tool
from greenbook_contracts.tool_result import ResourceRef
from greenbook_contracts.user_activity import (
    UserActivityEvent,
    UserActivityMapping,
    UserActivityStatus,
    UserActivityType,
    activity_mapping_for_semantic_action,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ProjectedUserActivity:
    """Event plus a private idempotency key used by the Activity store."""

    event: UserActivityEvent
    dedupe_key: str


# This bridge is only for existing Runtime capability names.  The product
# vocabulary itself remains rooted in SemanticAction/ToolContract; capability
# aliases prevent a legacy plan step from forcing the frontend to understand
# either a capability or a tool name.
_CAPABILITY_ACTIONS: dict[str, str] = {
    "SEARCH_COMMUNITY": "SEARCH_POSTS",
    "GET_POST_DETAIL": "GET_POST",
    "READ_POST": "GET_POST",
    "READ_CONTENT": "GET_POST",
    "LIST_OWN_POSTS": "LIST_OWN_POSTS",
    "GENERATE_CONTENT": "CREATE_DRAFT",
    "CREATE_DRAFT": "CREATE_DRAFT",
    "IMPROVE_CONTENT": "UPDATE_DRAFT",
    "REVISE_DRAFT": "UPDATE_DRAFT",
    "UPDATE_DRAFT": "UPDATE_DRAFT",
    "DELETE_DRAFT": "DELETE_DRAFT",
    "SCHEDULE_PUBLISH": "CREATE_SCHEDULE",
    "CREATE_SCHEDULE": "CREATE_SCHEDULE",
    "MANAGE_SCHEDULE": "UPDATE_SCHEDULE",
    "UPDATE_SCHEDULE": "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE": "CANCEL_SCHEDULE",
    "PUBLISH_NOW": "PUBLISH_NOW",
    "LIST_COMMENTS": "LIST_COMMENTS",
    "REPLY_USER": "REPLY_COMMENT",
    "REPLY_COMMENT": "REPLY_COMMENT",
    "GET_POST_PERFORMANCE": "GET_POST_PERFORMANCE",
    "GET_ACCOUNT_SUMMARY": "GET_ACCOUNT_SUMMARY",
}


class UserActivityProjector:
    """Facts -> user-visible activity, with verification-aware completion."""

    def project_started(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        semantic_action: str | None = None,
        capability: str | None = None,
        tool_name: str | None = None,
        source_key: str,
        created_at: str | None = None,
    ) -> ProjectedUserActivity | None:
        mapping = self._mapping(
            semantic_action=semantic_action,
            capability=capability,
            tool_name=tool_name,
        )
        if mapping is None:
            return None
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                activity_type=mapping.started_type,
                status=UserActivityStatus.IN_PROGRESS,
                display_key=mapping.started_display_key,
                safe_payload={"business_state": "PROCESSING"},
                created_at=created_at or _now_iso(),
                terminal=False,
            ),
            dedupe_key=f"{source_key}:started",
        )

    def project_result(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        result: Any,
        semantic_action: str | None = None,
        capability: str | None = None,
        tool_name: str | None = None,
        source_key: str,
        created_at: str | None = None,
    ) -> ProjectedUserActivity | None:
        """Project a completed attempted action without trusting Run state.

        A successful side-effecting ToolResult is promoted to COMPLETED only
        when its OperationReceipt has verified the business postcondition.
        Queue acceptance is not a terminal operation result and produces no
        event here; the Worker will project the actual invocation.
        """

        data = _as_mapping(result)
        mapping = self._mapping(
            semantic_action=semantic_action or _optional_text(data.get("semantic_action")),
            capability=capability or _optional_text(data.get("capability")),
            tool_name=tool_name or _optional_text(data.get("tool_name")),
        )
        if mapping is None:
            return None
        status = str(data.get("status") or "").upper()
        code = str(data.get("code") or data.get("error_code") or "").upper()
        if status in {"QUEUED", "SUBMITTED", "ACCEPTED"} or bool(data.get("queued")):
            # Durable acceptance is real, but it is not a completed business
            # operation. Keep the already-emitted IN_PROGRESS event open.
            return None

        if status in {"PENDING", "RUNNING", "PROCESSING", "WAITING_EXTERNAL"}:
            # A worker/external operation that is still active must not close
            # the started activity, even when a transport envelope says ok.
            return None

        if (
            status == "SUPERSEDED"
            or str(data.get("mutation_status") or "").upper() == "SUPERSEDED"
        ):
            # Superseded logical work is history, not a failed business
            # operation and not a new actionable activity.
            return None

        resource_ref = _resource_ref(data)
        safe_payload = _safe_payload(data, resource_ref=resource_ref)
        try:
            from greenbook_agent_core.execution.presenter import business_state_for_resource

            business_state = business_state_for_resource(
                resource_ref.kind if resource_ref is not None else data.get("resource_type"),
                data.get("status") or _as_mapping(data.get("data")).get("status"),
                data.get("run_at") or _as_mapping(data.get("data")).get("run_at"),
            )
            if business_state:
                safe_payload["business_state"] = business_state
        except (ImportError, TypeError, ValueError):
            # Activity projection remains usable for narrow legacy imports.
            pass
        known_success = bool(data.get("ok", data.get("success", False)))
        receipt = _receipt(data)

        if code == "RESULT_UNKNOWN" or _receipt_is_unknown(receipt):
            return self._unknown(
                conversation_id=conversation_id,
                run_id=run_id,
                task_id=task_id,
                objective_id=objective_id,
                resource_ref=resource_ref,
                safe_payload=safe_payload,
                source_key=source_key,
                created_at=created_at,
            )

        if known_success:
            if mapping.requires_verified_postcondition and not _is_verified(receipt):
                # A write result without proof must never become a friendly
                # success card merely because a Runtime step happened to end.
                return self._unknown(
                    conversation_id=conversation_id,
                    run_id=run_id,
                    task_id=task_id,
                    objective_id=objective_id,
                    resource_ref=resource_ref,
                    safe_payload=safe_payload,
                    source_key=source_key,
                    created_at=created_at,
                )
            verified_at = _optional_text(
                _as_mapping(receipt).get("verified_at")
                or _as_mapping(receipt).get("completed_at")
            ) or (created_at or _now_iso())
            return ProjectedUserActivity(
                event=UserActivityEvent(
                    conversation_id=conversation_id,
                    run_id=_optional_text(run_id),
                    task_id=_optional_text(task_id),
                    objective_id=_optional_text(objective_id),
                    resource_ref=resource_ref,
                    activity_type=mapping.completed_type,
                    status=UserActivityStatus.COMPLETED,
                    display_key=mapping.completed_display_key,
                    safe_payload=safe_payload,
                    created_at=created_at or _now_iso(),
                    verified_at=verified_at,
                    terminal=True,
                ),
                dedupe_key=f"{source_key}:completed",
            )

        # Unknown delivery of a side effect is semantically different from a
        # known business rejection.  Reads with a timeout remain FAILED unless
        # their explicit result code says RESULT_UNKNOWN.
        safe_payload["message"] = _safe_failure_message(code)
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                resource_ref=resource_ref,
                activity_type=UserActivityType.FAILED,
                status=UserActivityStatus.FAILED,
                display_key="activity.failed",
                safe_payload=safe_payload,
                created_at=created_at or _now_iso(),
                terminal=True,
            ),
            dedupe_key=f"{source_key}:failed",
        )

    def project_clarification(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        clarification: Mapping[str, Any] | None,
        source_key: str,
    ) -> ProjectedUserActivity:
        payload = _safe_clarification_payload(clarification or {})
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                activity_type=UserActivityType.NEEDS_CLARIFICATION,
                status=UserActivityStatus.WAITING_CLARIFICATION,
                display_key="activity.clarification.required",
                safe_payload=payload,
                terminal=True,
            ),
            dedupe_key=f"{source_key}:clarification",
        )

    def project_semantic_confirmation(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        confirmation: Mapping[str, Any] | None,
        source_key: str,
    ) -> ProjectedUserActivity:
        """Project a canonical pre-WRITE semantic confirmation request."""

        raw = dict(confirmation or {})
        payload = _safe_semantic_confirmation_payload(raw)
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=None,
                activity_type=UserActivityType.NEEDS_SEMANTIC_CONFIRMATION,
                status=UserActivityStatus.WAITING_SEMANTIC_CONFIRMATION,
                display_key="activity.semantic_confirmation.required",
                safe_payload=payload,
                terminal=True,
            ),
            dedupe_key=f"{source_key}:semantic_confirmation",
        )

    def project_approval(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        approval: Mapping[str, Any] | None,
        source_key: str,
    ) -> ProjectedUserActivity:
        raw = dict(approval or {})
        payload = {
            key: value
            for key, value in {
                "title": _optional_text(raw.get("title") or raw.get("resource_title")),
                "description": _optional_text(raw.get("description")),
                "resource_id": _optional_text(raw.get("resource_id")),
                "resource_type": _optional_text(raw.get("resource_type")),
                "approval_id": _optional_text(raw.get("approval_id")),
            }.items()
            if value is not None
        }
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                activity_type=UserActivityType.NEEDS_APPROVAL,
                status=UserActivityStatus.WAITING_APPROVAL,
                display_key="activity.approval.required",
                safe_payload=payload,
                terminal=True,
            ),
            dedupe_key=f"{source_key}:approval",
        )

    def project_reconciling(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        resource_ref: ResourceRef | None,
        source_key: str,
    ) -> ProjectedUserActivity:
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                resource_ref=resource_ref,
                activity_type=UserActivityType.RECONCILING,
                status=UserActivityStatus.RECONCILING,
                display_key="activity.result.reconciling",
                safe_payload={
                    "resolution_status": "pending",
                    "business_state": "VERIFYING_RESULT",
                },
                terminal=False,
            ),
            dedupe_key=f"{source_key}:reconciling",
        )

    def project_unknown(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        resource_ref: ResourceRef | None,
        source_key: str,
        safe_payload: Mapping[str, Any] | None = None,
    ) -> ProjectedUserActivity:
        """Publish a known uncertainty even when the action metadata vanished."""

        return self._unknown(
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
            objective_id=objective_id,
            resource_ref=resource_ref,
            safe_payload=dict(safe_payload or {}),
            source_key=source_key,
            created_at=None,
        )

    def project_failed(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        source_key: str,
        code: str = "",
    ) -> ProjectedUserActivity:
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                activity_type=UserActivityType.FAILED,
                status=UserActivityStatus.FAILED,
                display_key="activity.failed",
                safe_payload={"message": _safe_failure_message(code)},
                terminal=True,
            ),
            dedupe_key=f"{source_key}:failed",
        )

    def _unknown(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        resource_ref: ResourceRef | None,
        safe_payload: dict[str, Any],
        source_key: str,
        created_at: str | None,
    ) -> ProjectedUserActivity:
        safe_payload = {
            **safe_payload,
            "message": "请求可能已发送，结果暂时无法确认。请不要重复操作。",
            "resolution_status": "pending",
            "business_state": "VERIFYING_RESULT",
        }
        return ProjectedUserActivity(
            event=UserActivityEvent(
                conversation_id=conversation_id,
                run_id=_optional_text(run_id),
                task_id=_optional_text(task_id),
                objective_id=_optional_text(objective_id),
                resource_ref=resource_ref,
                activity_type=UserActivityType.RESULT_UNKNOWN,
                status=UserActivityStatus.RESULT_UNKNOWN,
                display_key="activity.result.unknown",
                safe_payload=safe_payload,
                created_at=created_at or _now_iso(),
                terminal=False,
            ),
            dedupe_key=f"{source_key}:unknown",
        )

    @staticmethod
    def _mapping(
        *,
        semantic_action: str | None,
        capability: str | None,
        tool_name: str | None,
    ) -> UserActivityMapping | None:
        action = _optional_text(semantic_action)
        if action:
            mapping = activity_mapping_for_semantic_action(action)
            if mapping is not None:
                return mapping
            action = _CAPABILITY_ACTIONS.get(action.upper().replace("-", "_"))
            mapping = activity_mapping_for_semantic_action(action)
            if mapping is not None:
                return mapping
        capability_action = _CAPABILITY_ACTIONS.get(
            str(capability or "").upper().replace("-", "_")
        )
        mapping = activity_mapping_for_semantic_action(capability_action)
        if mapping is not None:
            return mapping
        if tool_name:
            try:
                return activity_mapping_for_semantic_action(
                    semantic_action_for_tool(str(tool_name)).value
                )
            except (KeyError, RuntimeError):
                return None
        return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        rendered = dump(mode="json")
        if isinstance(rendered, Mapping):
            return dict(rendered)
    return dict(getattr(value, "__dict__", {}) or {})


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = result.get("operation_receipt") or result.get("receipt") or {}
    return _as_mapping(raw)


def _is_verified(receipt: Mapping[str, Any]) -> bool:
    return bool(
        receipt
        and receipt.get("result_known") is True
        and str(receipt.get("status") or "").upper() == "COMPLETED"
        and receipt.get("verification_evidence")
    )


def _receipt_is_unknown(receipt: Mapping[str, Any]) -> bool:
    return bool(
        receipt
        and (
            receipt.get("result_known") is False
            or str(receipt.get("status") or "").upper() == "RESULT_UNKNOWN"
        )
    )


def _resource_ref(result: Mapping[str, Any]) -> ResourceRef | None:
    receipt = _receipt(result)
    raw = receipt.get("resource_ref")
    if raw:
        try:
            return ResourceRef.model_validate(raw)
        except (TypeError, ValueError):
            pass
    for item in result.get("resource_refs") or []:
        try:
            return ResourceRef.model_validate(item)
        except (TypeError, ValueError):
            continue
    data = _as_mapping(result.get("data"))
    candidates = (
        ("DRAFT", data.get("draft_id") or data.get("draftId")),
        ("SCHEDULE", data.get("schedule_id") or data.get("scheduleId")),
        ("POST", data.get("post_id") or data.get("postId")),
        ("DRAFT", result.get("draft_id")),
        ("SCHEDULE", result.get("schedule_id")),
    )
    for kind, resource_id in candidates:
        if _optional_text(resource_id):
            return ResourceRef(
                ref=f"{kind.lower()}:{resource_id}",
                kind=kind,
                resource_id=str(resource_id),
                version=_integer_or_none(data.get("version")),
            )
    return None


def _safe_payload(
    result: Mapping[str, Any],
    *,
    resource_ref: ResourceRef | None,
) -> dict[str, Any]:
    """Whitelist display data only; never forward raw result objects."""

    raw_data = result.get("data")
    data = _as_mapping(raw_data)
    payload: dict[str, Any] = {}
    # Read tools commonly return a top-level list.  Its length is a real
    # search fact, even though it is not a mapping with a ``total`` field.
    count = len(raw_data) if isinstance(raw_data, list) else _count_from(data)
    if count is not None:
        payload["result_count"] = count
    title = _optional_text(data.get("title") or result.get("title"))
    if title:
        payload["title"] = title[:300]
    preview = _optional_text(
        data.get("preview")
        or data.get("summary")
        or data.get("body")
        or data.get("content")
    )
    if preview:
        payload["preview"] = preview[:280]
    for output_key, source_keys in {
        "draft_id": ("draft_id", "draftId"),
        "schedule_id": ("schedule_id", "scheduleId"),
        "post_id": ("post_id", "postId"),
        "run_at": ("run_at", "runAt", "publish_at", "publishAt"),
        "timezone": ("timezone", "time_zone"),
        "status": ("status",),
        "version": ("version",),
    }.items():
        value = next((data.get(key) for key in source_keys if data.get(key) is not None), None)
        if value is not None and isinstance(value, (str, int, float, bool)):
            payload[output_key] = value
    if resource_ref is not None:
        payload.setdefault("resource_id", resource_ref.resource_id)
        payload.setdefault("resource_type", resource_ref.kind)
    return payload


def _count_from(data: Mapping[str, Any]) -> int | None:
    for key in ("count", "total", "total_count", "totalMatched"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    for key in ("items", "posts", "results", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    if isinstance(data, Mapping) and data.get("data") and isinstance(data["data"], list):
        return len(data["data"])
    return None


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_failure_message(code: str) -> str:
    normalized = str(code or "").upper()
    if normalized in {"NOT_FOUND", "DRAFT_NOT_FOUND", "SCHEDULE_NOT_FOUND"}:
        return "没有找到要操作的内容。"
    if normalized in {"CONFLICT", "VERSION_CONFLICT", "OPTIMISTIC_LOCK"}:
        return "内容刚刚发生了变化，请重新确认后再试。"
    if normalized in {"VALIDATION_ERROR", "PERMANENT_INPUT"}:
        return "提供的信息不完整或不符合要求。"
    if normalized in {"PERMISSION_DENIED", "AUTHENTICATION_REQUIRED"}:
        return "当前没有执行这项操作的权限。"
    if normalized in {"APPROVAL_REQUIRED"}:
        return "需要你的确认后才能继续。"
    return "这项操作暂时没有完成，请稍后重试。"


def _safe_clarification_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, str]] = []
    for item in raw.get("candidates") or raw.get("targets") or []:
        value = _as_mapping(item)
        label = _optional_text(
            value.get("label")
            or value.get("title")
            or value.get("display_name")
            or value.get("name")
        )
        resource_id = _optional_text(
            value.get("resource_id") or value.get("id") or value.get("target_id")
        )
        candidate: dict[str, str] = {}
        if label:
            candidate["label"] = label[:300]
        if resource_id:
            candidate["resource_id"] = resource_id
        kind = _optional_text(value.get("resource_type") or value.get("kind") or value.get("type"))
        if kind:
            candidate["resource_type"] = kind
        run_at = _optional_text(value.get("run_at") or value.get("scheduled_at"))
        if run_at:
            candidate["run_at"] = run_at
        if candidate:
            candidates.append(candidate)
    question = _optional_text(raw.get("question") or raw.get("message"))
    return {
        "question": (question or "请确认你要操作的是哪一项内容。")[:500],
        "candidates": candidates[:12],
    }


def _safe_semantic_confirmation_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist canonical business facts; omit runtime and internal IDs."""

    payload: dict[str, Any] = {}
    for key in (
        "confirmation_id",
        "task_version",
        "confirmation_version",
        "title",
        "has_real_side_effect",
        "available_actions",
        "policy_reason",
    ):
        value = raw.get(key)
        if value is not None and isinstance(value, (str, int, float, bool, list)):
            payload[key] = value
    rendered_objectives: list[dict[str, Any]] = []
    for item in raw.get("objectives") or []:
        if not isinstance(item, Mapping):
            continue
        objective: dict[str, Any] = {}
        for key in (
            "topic",
            "desired_outcome",
            "outcome",
            "run_at",
            "timezone",
            "publication_intent",
            "dependencies",
            "has_real_side_effect",
        ):
            value = item.get(key)
            if key == "dependencies" and isinstance(value, list):
                objective[key] = [
                    str(dependency)[:300]
                    for dependency in value
                    if isinstance(dependency, str) and dependency.strip()
                ][:8]
            elif value is not None and isinstance(value, (str, int, float, bool)):
                objective[key] = value
        target = item.get("target")
        if isinstance(target, Mapping):
            safe_target = {
                key: str(target[key])[:300]
                for key in ("kind", "label")
                if target.get(key)
            }
            if safe_target:
                objective["target"] = safe_target
        constraints = item.get("constraints")
        if isinstance(constraints, Mapping):
            safe_constraints = {
                key: constraints[key]
                for key in ("run_at", "timezone", "publication_intent", "requirements")
                if constraints.get(key) is not None
                and isinstance(constraints[key], (str, int, float, bool, list))
            }
            if safe_constraints:
                objective["constraints"] = safe_constraints
        rendered_objectives.append(objective)
    payload["objectives"] = rendered_objectives
    return payload


__all__ = ["ProjectedUserActivity", "UserActivityProjector"]
