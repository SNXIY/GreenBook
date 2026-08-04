"""TurnPlan: composable, goal-aware control-plane contract for one user turn.

Natural language is interpreted into a small set of typed Changes against one
ConversationGoal (or a Task Bag of TurnPlans). Deterministic compilers turn
those Changes into DAGs; open-ended work sets ``open_plan`` and falls through
to the adaptive Planner.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import Field

from app.domain import (
    ApiModel,
    ConversationGoal,
    IntentDelta,
    TargetContext,
    TurnIntent,
)
from app.execution import is_new_scheduled_post_request
from app.intent_delta import IntentDeltaParser, TurnIntentParser


ChangeRole = Literal[
    "CONTENT",
    "SCHEDULE",
    "PUBLICATION",
    "INTERACTION",
    "ANALYSIS",
]
ChangeOp = Literal[
    "CREATE",
    "APPEND",
    "REPLACE",
    "UPDATE",
    "CANCEL",
    "QUERY",
    "PUBLISH_NOW",
    "UPDATE_TITLE",
]
TurnRelation = Literal[
    "NEW_GOAL",
    "CONTINUE",
    "MODIFY",
    "CANCEL",
    "RETRY",
    "QUERY_STATE",
]


class Change(ApiModel):
    """One typed attribute mutation on the resolved Goal."""

    role: ChangeRole
    op: ChangeOp
    payload: dict[str, Any] = Field(default_factory=dict)


class TurnPlan(ApiModel):
    """Structured interpretation of one user message against conversation goals."""

    turn_relation: TurnRelation = "NEW_GOAL"
    goal_ref: str | None = Field(default=None, max_length=160)
    semantic_subject: str = Field(default="", max_length=500)
    explicit_refs: list[str] = Field(default_factory=list, max_length=12)
    changes: list[Change] = Field(default_factory=list, max_length=8)
    open_plan: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_message: str = Field(default="", max_length=10_000)
    # Nested independent goal actions for one message (serial in Phase 1).
    tasks: list["TurnPlan"] = Field(default_factory=list, max_length=4)


_OPERATION_TO_CHANGES: dict[str, list[tuple[ChangeRole, ChangeOp]]] = {
    "CREATE_POST": [("CONTENT", "CREATE")],
    "APPEND_CONTENT": [("CONTENT", "APPEND")],
    "REPLACE_CONTENT": [("CONTENT", "REPLACE")],
    "UPDATE_TITLE": [("CONTENT", "UPDATE_TITLE")],
    "UPDATE_SCHEDULE": [("SCHEDULE", "UPDATE")],
    "CANCEL_SCHEDULE": [("SCHEDULE", "CANCEL")],
    "PUBLISH_NOW": [("PUBLICATION", "PUBLISH_NOW")],
    "QUERY_SCHEDULE": [("SCHEDULE", "QUERY")],
    "QUERY_CONTENT": [("CONTENT", "QUERY")],
    "QUERY_PUBLICATION_STATUS": [("PUBLICATION", "QUERY")],
    "OPEN_PLAN": [],
}


def changes_from_operation(
    operation: str,
    *,
    message: str,
    schedule_request: str | None = None,
) -> list[Change]:
    """Expand a legacy IntentDelta operation into composable Changes."""

    specs = _OPERATION_TO_CHANGES.get(operation, [])
    changes: list[Change] = []
    for role, op in specs:
        payload: dict[str, Any] = {"message": message}
        if role == "CONTENT" and op in {"APPEND", "REPLACE", "UPDATE_TITLE"}:
            payload["instruction"] = message
        if role == "SCHEDULE" and op == "UPDATE":
            payload["schedule_request"] = schedule_request or message
        if role == "CONTENT" and schedule_request:
            payload["schedule_request"] = schedule_request
        changes.append(Change(role=role, op=op, payload=payload))

    # Compound: content edit that also asks to change publish time.
    if (
        operation in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE"}
        and schedule_request
        and not any(item.role == "SCHEDULE" for item in changes)
    ):
        changes.append(
            Change(
                role="SCHEDULE",
                op="UPDATE",
                payload={"schedule_request": schedule_request, "message": message},
            )
        )
    return changes


def primary_operation_from_changes(changes: list[Change], *, open_plan: bool) -> str:
    """Collapse Changes into the legacy IntentDelta.operation for contracts."""

    if open_plan and not changes:
        return "OPEN_PLAN"
    if not changes:
        return "OPEN_PLAN"
    roles = {(item.role, item.op) for item in changes}
    if ("PUBLICATION", "PUBLISH_NOW") in roles:
        return "PUBLISH_NOW"
    if ("SCHEDULE", "CANCEL") in roles:
        return "CANCEL_SCHEDULE"
    if ("CONTENT", "APPEND") in roles:
        return "APPEND_CONTENT"
    if ("CONTENT", "REPLACE") in roles:
        return "REPLACE_CONTENT"
    if ("CONTENT", "UPDATE_TITLE") in roles:
        return "UPDATE_TITLE"
    if ("SCHEDULE", "UPDATE") in roles and not any(
        role == "CONTENT" for role, _ in roles
    ):
        return "UPDATE_SCHEDULE"
    if ("SCHEDULE", "QUERY") in roles:
        return "QUERY_SCHEDULE"
    if ("CONTENT", "QUERY") in roles:
        return "QUERY_CONTENT"
    if ("PUBLICATION", "QUERY") in roles:
        return "QUERY_PUBLICATION_STATUS"
    if ("CONTENT", "CREATE") in roles:
        return "CREATE_POST"
    if ("SCHEDULE", "UPDATE") in roles:
        # content + schedule compound collapses to content mutation for contracts
        if ("CONTENT", "APPEND") in roles:
            return "APPEND_CONTENT"
        return "UPDATE_SCHEDULE"
    return "OPEN_PLAN"


def turn_plan_from_intent_delta(delta: IntentDelta) -> TurnPlan:
    message = str(delta.delta.get("message") or "")
    schedule_request = str(delta.delta.get("schedule_request") or "") or None
    open_plan = delta.operation == "OPEN_PLAN"
    changes = changes_from_operation(
        delta.operation,
        message=message,
        schedule_request=schedule_request,
    )
    return TurnPlan(
        turn_relation=str(delta.delta.get("turn_relation") or "MODIFY"),  # type: ignore[arg-type]
        goal_ref=f"goal:{delta.goal_id}" if delta.goal_id else None,
        semantic_subject=str(delta.delta.get("semantic_subject") or ""),
        explicit_refs=list(delta.delta.get("explicit_refs") or []),
        changes=changes,
        open_plan=open_plan,
        confidence=delta.confidence,
        raw_message=message,
    )


def intent_delta_from_turn_plan(
    *,
    turn_plan: TurnPlan,
    goal: ConversationGoal,
    run_id: str,
    message_id: str,
    target_context: TargetContext | None = None,
    intent_domain: str | None = None,
    intent_goal: str | None = None,
) -> IntentDelta:
    """Project a TurnPlan onto the persisted IntentDelta row shape."""

    context = target_context or goal.target_context
    if any(c.role == "ANALYSIS" for c in turn_plan.changes):
        operation = "CONTINUE_ANALYSIS"
    elif any(c.role == "INTERACTION" for c in turn_plan.changes):
        operation = "REPLY_COMMENT"
    else:
        operation = primary_operation_from_changes(
            turn_plan.changes,
            open_plan=turn_plan.open_plan,
        )
    message = turn_plan.raw_message
    schedule_change = next(
        (
            item
            for item in turn_plan.changes
            if item.role == "SCHEDULE" and item.op == "UPDATE"
        ),
        None,
    )
    content_change = next(
        (item for item in turn_plan.changes if item.role == "CONTENT"),
        None,
    )
    if operation in {"OPEN_PLAN", "REPLY_COMMENT", "CONTINUE_ANALYSIS"}:
        operation_class = "WRITE"
        target_role = "INTERACTION" if operation == "REPLY_COMMENT" else None
    elif operation.startswith("QUERY_"):
        operation_class = "READ"
        target_role = {
            "QUERY_SCHEDULE": "SCHEDULE",
            "QUERY_CONTENT": "CONTENT",
            "QUERY_PUBLICATION_STATUS": "PUBLICATION",
        }.get(operation)
    elif operation in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}:
        operation_class = "SIDE_EFFECT"
        target_role = "SCHEDULE" if operation != "PUBLISH_NOW" else "CONTENT"
    else:
        operation_class = "WRITE"
        target_role = "CONTENT"

    # Compatibility: content+schedule compound stays APPEND_* with schedule_request.
    if (
        content_change is not None
        and schedule_change is not None
        and operation
        in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE", "UPDATE_SCHEDULE"}
    ):
        if content_change.op == "APPEND":
            operation = "APPEND_CONTENT"
        elif content_change.op == "REPLACE":
            operation = "REPLACE_CONTENT"
        elif content_change.op == "UPDATE_TITLE":
            operation = "UPDATE_TITLE"
        operation_class = "WRITE"
        target_role = "CONTENT"

    operation_target = context.for_operation(operation) if operation != "OPEN_PLAN" else None
    target_ref = (
        f"{operation_target.target_type.lower()}:{operation_target.target_id}"
        if operation_target is not None
        else goal.active_target_ref
    )
    preserve = IntentDeltaParser._preserve(operation, context)
    delta: dict[str, Any] = {
        "message": message,
        "intent_domain": intent_domain,
        "intent_goal": intent_goal,
        "turn_relation": turn_plan.turn_relation,
        "semantic_subject": turn_plan.semantic_subject,
        "explicit_refs": turn_plan.explicit_refs,
        "changes": [item.model_dump(mode="json") for item in turn_plan.changes],
        "open_plan": turn_plan.open_plan,
    }
    if schedule_change is not None:
        delta["schedule_request"] = str(
            schedule_change.payload.get("schedule_request") or message
        )
    if content_change is not None and content_change.op in {
        "APPEND",
        "REPLACE",
        "UPDATE_TITLE",
    }:
        delta["instruction"] = str(
            content_change.payload.get("instruction") or message
        )
    if operation == "UPDATE_SCHEDULE" and "schedule_request" not in delta:
        delta["schedule_request"] = message

    return IntentDelta(
        delta_id=str(uuid.uuid4()),
        goal_id=goal.goal_id,
        run_id=run_id,
        message_id=message_id,
        operation=operation,  # type: ignore[arg-type]
        operation_class=operation_class,  # type: ignore[arg-type]
        target_role=target_role,  # type: ignore[arg-type]
        target_ref=target_ref,
        delta=delta,
        preserve=preserve,
        confidence=turn_plan.confidence,
        status="ACTIVE",
    )


_OPERATIONS_FROM_ROUTER = {
    "CREATE_POST",
    "APPEND_CONTENT",
    "REPLACE_CONTENT",
    "UPDATE_TITLE",
    "UPDATE_SCHEDULE",
    "PUBLISH_NOW",
    "CANCEL_SCHEDULE",
    "QUERY_SCHEDULE",
    "QUERY_CONTENT",
    "QUERY_PUBLICATION_STATUS",
    "OPEN_PLAN",
    "REPLY_COMMENT",
    "CONTINUE_ANALYSIS",
}


def reconcile_router_operation(
    *,
    router_operation: str | None,
    parsed_operation: str,
    message: str,
    prefer_router: bool = False,
) -> str:
    """Merge Adaptive Router labels with safety-valve parsing.

    Concrete router ops usually win, except when they would drop half of a
    compound mutation (e.g. router UPDATE_SCHEDULE while the user also asked
    to edit draft content).
    """

    if IntentDeltaParser._has_content_mutation_request(message) and parsed_operation in {
        "APPEND_CONTENT",
        "REPLACE_CONTENT",
        "UPDATE_TITLE",
    }:
        if router_operation in {None, "OPEN_PLAN", "UPDATE_SCHEDULE"}:
            return parsed_operation
    if prefer_router and router_operation and router_operation in _OPERATIONS_FROM_ROUTER:
        return router_operation
    if router_operation and router_operation in _OPERATIONS_FROM_ROUTER:
        # Non-prefer path: still allow concrete router to override OPEN_PLAN-ish
        # parser misses, but not to shrink content mutations (handled above).
        if parsed_operation == "OPEN_PLAN":
            return router_operation
    return parsed_operation


def split_task_bag_messages(message: str) -> list[str]:
    """Split one user utterance into independent goal actions when obvious.

    Conservative: only splits on explicit multi-task connectors. Compound
    changes on the *same* goal (content + schedule) stay as one message and
    become multiple Changes, not a Task Bag.
    """

    text = message.strip()
    if not text:
        return []
    connectors = (
        "顺便",
        "另外",
        "同时再",
        "同时帮我",
        "然后再",
        "再帮我写一篇",
        "再写一篇",
        "再创作一篇",
        "再发一篇",
    )
    for connector in connectors:
        index = text.find(connector)
        if index <= 0:
            continue
        left = text[:index].strip(" ，,;；")
        right = text[index:].strip()
        if left and right and _looks_like_content_goal(right, None):
            return [left, right]
    return [text]


class TurnPlanBuilder:
    """Build a TurnPlan from router signals + safety-valve heuristics."""

    def build(
        self,
        *,
        message: str,
        turn_relation: str = "NEW_GOAL",
        intent_domain: str | None = None,
        intent_goal: str | None = None,
        plan_intent: str | None = None,
        has_target: bool = False,
        goal_ref: str | None = None,
        router_operation: str | None = None,
        router_open_plan: bool | None = None,
        follow_up_prompts: list[str] | None = None,
        prefer_router: bool = False,
    ) -> TurnPlan:
        text = message.strip()
        if follow_up_prompts:
            bag_messages = [text, *[item.strip() for item in follow_up_prompts if item.strip()]]
        else:
            bag_messages = split_task_bag_messages(text)
        primary_message = bag_messages[0] if bag_messages else text
        # Always parse the utterance. Router labels are hints that must not
        # shrink a compound content±schedule mutation into schedule-only.
        parsed_intent = TurnIntentParser().parse(
            message=primary_message,
            has_target=has_target,
            turn_relation=turn_relation,
            plan_intent=plan_intent or (
                router_operation
                if router_operation in _OPERATIONS_FROM_ROUTER
                else None
            ),
            intent_domain=intent_domain,
            intent_goal=intent_goal,
        )
        router_concrete = (
            router_operation
            if router_operation in _OPERATIONS_FROM_ROUTER
            and router_operation != "OPEN_PLAN"
            else None
        )
        operation = reconcile_router_operation(
            router_operation=router_concrete,
            parsed_operation=parsed_intent.operation,
            message=primary_message,
            prefer_router=prefer_router,
        )
        turn_intent = parsed_intent.model_copy(
            update={
                "operation": operation,  # type: ignore[arg-type]
                "operation_class": (
                    "READ"
                    if operation.startswith("QUERY_")
                    else (
                        "SIDE_EFFECT"
                        if operation
                        in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}
                        else "WRITE"
                    )
                ),
                "confidence": (
                    max(parsed_intent.confidence, 0.95)
                    if prefer_router and router_concrete == operation
                    else parsed_intent.confidence
                ),
            }
        )
        plan = self.from_turn_intent(
            turn_intent=turn_intent,
            message=primary_message,
            turn_relation=turn_relation,
            intent_domain=intent_domain,
            goal_ref=goal_ref,
        )
        # Router open_plan is advisory. Bounded Changes already recovered from
        # the utterance (content±schedule, cancel, publish, query) must not be
        # wiped — that path is exactly what sent live compound edits into the
        # Planner repair loop with invented capabilities like schedule_update.
        if router_open_plan is True and not plan.changes:
            plan = plan.model_copy(update={"open_plan": True, "changes": []})
        nested: list[TurnPlan] = []
        for follow in bag_messages[1:4]:
            nested.append(
                TurnPlanBuilder().from_turn_intent(
                    turn_intent=TurnIntentParser().parse(
                        message=follow,
                        has_target=False,
                        turn_relation="NEW_GOAL",
                        intent_domain=intent_domain,
                    ),
                    message=follow,
                    turn_relation="NEW_GOAL",
                    intent_domain=intent_domain,
                )
            )
        if nested:
            plan = plan.model_copy(
                update={
                    "tasks": nested,
                    "raw_message": text,
                }
            )
        return plan

    def from_turn_intent(
        self,
        *,
        turn_intent: TurnIntent,
        message: str,
        turn_relation: str = "NEW_GOAL",
        intent_domain: str | None = None,
        goal_ref: str | None = None,
    ) -> TurnPlan:
        operation = turn_intent.operation
        open_plan = operation == "OPEN_PLAN"
        schedule_request = None
        if operation in {"UPDATE_SCHEDULE"}:
            schedule_request = message
        elif operation in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE"}:
            if IntentDeltaParser._has_schedule_request(message):
                schedule_request = message
        changes = changes_from_operation(
            operation,
            message=message,
            schedule_request=schedule_request,
        )
        # Map analysis / comment follow-ups.
        if operation == "CONTINUE_ANALYSIS":
            changes = [
                Change(
                    role="ANALYSIS",
                    op="UPDATE",
                    payload={"message": message, "instruction": message},
                )
            ]
            open_plan = True
        elif operation == "REPLY_COMMENT":
            changes = [
                Change(
                    role="INTERACTION",
                    op="UPDATE",
                    payload={"message": message, "instruction": message},
                )
            ]
            open_plan = True
        # Non-content new goals must not pretend to be CREATE_POST.
        if open_plan or (
            operation == "CREATE_POST"
            and not _looks_like_content_goal(message, intent_domain)
            and turn_relation == "NEW_GOAL"
        ):
            if operation not in {"CONTINUE_ANALYSIS", "REPLY_COMMENT"} and (
                not _looks_like_content_goal(message, intent_domain)
            ):
                open_plan = True
                changes = []
        return TurnPlan(
            turn_relation=turn_relation,  # type: ignore[arg-type]
            goal_ref=goal_ref,
            semantic_subject=turn_intent.semantic_subject,
            explicit_refs=list(turn_intent.explicit_refs),
            changes=changes,
            open_plan=open_plan,
            confidence=turn_intent.confidence,
            raw_message=message,
        )


def _looks_like_content_goal(message: str, intent_domain: str | None) -> bool:
    domain = (intent_domain or "").lower()
    if domain.startswith("content"):
        return True
    if is_new_scheduled_post_request(message):
        return True
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("帖子", "草稿", "创作", "写一篇", "发布一篇", "draft", "post")
    )


def extract_schedule_run_at(
    turn_plan: TurnPlan,
    *,
    client_timezone: str = "Asia/Shanghai",
    current_time: datetime | None = None,
    existing_run_at: datetime | None = None,
) -> str | None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.temporal_resolver import normalize_run_at_for_tool, resolve_schedule_time

    for change in turn_plan.changes:
        if change.role != "SCHEDULE" or change.op != "UPDATE":
            continue
        stamped = change.payload.get("run_at")
        if stamped:
            return normalize_run_at_for_tool(str(stamped))
        request = str(
            change.payload.get("schedule_request")
            or change.payload.get("message")
            or turn_plan.raw_message
        )
        try:
            zone = ZoneInfo(client_timezone)
        except Exception:
            zone = ZoneInfo("Asia/Shanghai")
        current = current_time or datetime.now(zone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=zone)
        existing = existing_run_at
        if existing is not None and existing.tzinfo is None:
            existing = existing.replace(tzinfo=zone)
        resolution = resolve_schedule_time(
            message=request,
            current_time=current,
            timezone=client_timezone,
            existing_run_at=existing,
        )
        if resolution.run_at is not None:
            return normalize_run_at_for_tool(resolution.run_at)
    return None


def goal_id_from_ref(goal_ref: str | None) -> str | None:
    if not goal_ref:
        return None
    match = re.match(r"^goal:(.+)$", goal_ref.strip(), flags=re.IGNORECASE)
    return match.group(1) if match else goal_ref.strip() or None


__all__ = [
    "Change",
    "TurnPlan",
    "TurnPlanBuilder",
    "changes_from_operation",
    "primary_operation_from_changes",
    "reconcile_router_operation",
    "turn_plan_from_intent_delta",
    "intent_delta_from_turn_plan",
    "extract_schedule_run_at",
    "goal_id_from_ref",
    "split_task_bag_messages",
]
