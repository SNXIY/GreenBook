"""Phase 6.8.1 — Compatibility: IntentSpec → TaskIntent conversion.

保证 Orchestrator 以下完全不知道 IntentSpec。
"""

from __future__ import annotations

from .intent_models import ActionType, IntentSpec, ResourceType
from .models import TaskIntent


def to_task_intent(spec: IntentSpec) -> TaskIntent:
    """从 IntentSpec 推导旧 TaskIntent，零信息丢失在旧端。

    新字段 (mode, conditions) 下游尚未消费，仅在 TaskIntent.intent_spec 中保留。
    """

    # ── relation: from first action ──
    primary = spec.actions[0].action if spec.actions else ActionType.QUERY
    relation_map: dict[ActionType, str] = {
        ActionType.CREATE:            "NEW_TASK",
        ActionType.SEARCH:            "NEW_TASK",
        ActionType.ANALYZE:           "NEW_TASK",
        ActionType.UPDATE:            "MODIFY_TASK",
        ActionType.UPDATE_OR_CREATE:  "NEW_TASK",
        ActionType.DELETE:            "CANCEL_TASK",
        ActionType.QUERY:             "DIRECT",
        ActionType.PUBLISH:           "NEW_TASK",
    }
    if spec.target_hint and primary == ActionType.UPDATE:
        relation = "MODIFY_TASK"
    else:
        relation = relation_map.get(primary, "NEW_TASK")

    # ── goal_category (resource-aware) ──
    primary_resource = spec.actions[0].resource if spec.actions else None
    action_types = {a.action for a in spec.actions}

    if primary == ActionType.UPDATE and primary_resource == ResourceType.SCHEDULE:
        category = "MANAGE_SCHEDULE"
    elif primary == ActionType.DELETE and primary_resource == ResourceType.SCHEDULE:
        category = "MANAGE_SCHEDULE"
    elif primary == ActionType.UPDATE and primary_resource == ResourceType.DRAFT:
        category = "QUERY_INFO"
    elif primary == ActionType.QUERY:
        category = "QUERY_INFO"
    elif ActionType.UPDATE_OR_CREATE in action_types:
        category = "CREATE_CONTENT"
    elif spec.mode.value == "COMPOSITE" and ActionType.CREATE in action_types:
        category = "CREATE_CONTENT"
    elif spec.mode.value == "COMPOSITE" and ActionType.UPDATE in action_types:
        category = "IMPROVE_CONTENT"
    elif spec.mode.value == "CONDITIONAL" and ActionType.UPDATE_OR_CREATE in action_types:
        category = "CREATE_CONTENT"
    elif ActionType.PUBLISH in action_types and ActionType.CREATE not in action_types:
        category = "PUBLISH_CONTENT"
    else:
        category_map: dict[ActionType, str] = {
            ActionType.CREATE:            "CREATE_CONTENT",
            ActionType.UPDATE:            "IMPROVE_CONTENT",
            ActionType.UPDATE_OR_CREATE:  "CREATE_CONTENT",
            ActionType.SEARCH:            "ANALYZE_COMMUNITY",
            ActionType.ANALYZE:           "ANALYZE_COMMUNITY",
            ActionType.PUBLISH:           "PUBLISH_CONTENT",
            ActionType.DELETE:            "MANAGE_SCHEDULE",
            ActionType.QUERY:             "QUERY_INFO",
        }
        category = category_map.get(primary, "QUERY_INFO")

    # ── requirements ──
    req_type_map: dict[ActionType, str] = {
        ActionType.SEARCH:  "SEARCH",
        ActionType.ANALYZE: "ANALYZE",
        ActionType.CREATE:  "CREATE",
        ActionType.UPDATE:  "IMPROVE",
        ActionType.UPDATE_OR_CREATE: "CREATE",
        ActionType.PUBLISH: "PUBLISH",
        ActionType.DELETE:  "CANCEL",
    }
    reqs = [
        {"type": req_type_map[a.action]}
        for a in spec.actions
        if a.action in req_type_map
    ]

    # ── resource_requests ──
    op_map: dict[ActionType, str] = {
        ActionType.CREATE:            "CREATE",
        ActionType.UPDATE:            "UPDATE",
        ActionType.UPDATE_OR_CREATE:  "CREATE",
        ActionType.DELETE:            "DELETE",
        ActionType.PUBLISH:           "CREATE",
        ActionType.SEARCH:            "QUERY",
        ActionType.QUERY:             "QUERY",
    }
    res_map: dict[ResourceType, str] = {
        ResourceType.CONTENT:  "CONTENT_DRAFT",
        ResourceType.DRAFT:    "CONTENT_DRAFT",
        ResourceType.SCHEDULE: "SCHEDULE",
        ResourceType.POST:     "POST",
        ResourceType.TASK:     "TASK",
    }
    resource_reqs: list[dict[str, str]] = [
        {
            "operation": op_map[a.action],
            "resource_type": res_map.get(a.resource, "CONTENT_DRAFT"),
            "hint": spec.target_hint or "",
        }
        for a in spec.actions
        if a.action in op_map and a.resource in res_map
    ]

    return TaskIntent(
        relation=relation,  # type: ignore[arg-type]
        goal=spec.goal,
        goal_category=category,
        target_task_hint=spec.target_hint,
        requirements=reqs,
        resource_requests=resource_reqs,
        constraints=[
            {"type": c.type.value, "value": c.value} for c in spec.constraints
        ],
        confidence=spec.confidence,
        source=spec.source,
    )
"""Deprecated compatibility adapter.

Do not extend. Migration target: direct IntentSpec consumers.
"""
