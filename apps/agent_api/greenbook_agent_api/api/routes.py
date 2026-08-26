"""Agent API routes for conversations, tasks, history, approvals, and events."""

from __future__ import annotations

import asyncio
import json
import time
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from greenbook_agent_core.compatibility.history import RunExecutionAdapter
from greenbook_agent_core.context import PendingApproval, SessionContext
from greenbook_agent_core.conversation import ConversationNotFoundError
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.operation_ledger import is_reconciliation_exhausted
from greenbook_agent_core.execution.presenter import business_state_for_resource
from greenbook_agent_core.human import ApprovalRequestStatus
from greenbook_agent_core.memory.models import MemoryQuery, MemoryStatus, MemoryType
from greenbook_agent_core.task.provider import TaskScope
from greenbook_agent_core.task.manager import TaskConfirmationConflictError
from greenbook_agent_core.task.models import TaskConfirmationState
from greenbook_agent_core.task.semantic_confirmation import confirmation_identity
from greenbook_agent_core.time_parser import format_local_schedule_time
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.user_activity import (
    SemanticConfirmationAction,
    SemanticConfirmationControl,
    UserActivityEvent,
)
from pydantic import BaseModel, Field

from ..models.runtime_result import RuntimeResult
from ..runner import (
    EVENT_FOLLOW_UP_QUEUED,
    RUN_ACCEPTED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_WORKING,
    AgentRun,
)
from ..runner import (
    RUN_TERMINAL as _RUN_TERMINAL,
)
from ..services.conversation_runtime_adapter import ConversationRuntimeAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent")

# ── How session data is stored ───────────────────────────────────────
#
# request.app.state.conversation_store[conv_id] = {
#     "conversation_id": str,
#     "user_id": str,          # frozen — from AuthContext at create time
#     "tenant_id": str,        # frozen — from AuthContext at create time
#     "title": str | None,
#     "created_at": str,       # ISO-8601 UTC
#     "updated_at": str,       # ISO-8601 UTC — bumped on every message
#     "active_draft_id": str | None,
#     "active_schedule_id": str | None,
#     "active_post_id": str | None,
#     "recent_entities": [...],
#     "recent_tool_calls": [...],
#     "pending_approval": ... | None,
#     "last_successful_run_id": str | None,
# }
#
# run_store maps run_id → { run_id, conversation_id, user_id, tenant_id, ... }
# approval_store maps approval_id → { approval_id, user_id, conversation_id, ... }


# ── Request / Response models ────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    # The POST detail surface supplies the current post so a new Agent
    # conversation can resolve "this post" without guessing from search
    # history. This is an existing SessionContext binding.
    context_post_id: str | None = Field(default=None, max_length=128)
    surface: str | None = Field(default=None, max_length=32)


class ConversationSummary(BaseModel):
    """Public-safe conversation list item.  Never exposes tokens, secrets, or internals."""
    conversation_id: str
    title: str | None = None
    active_draft_id: str | None = None
    active_schedule_id: str | None = None
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    page: int = 1
    size: int = 20
    total: int = 0


class MessageView(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str
    content: str
    parts: list[dict[str, Any]] = Field(default_factory=list)
    run_id: str | None = None
    execution_id: str | None = None
    trace_id: str | None = None
    created_at: str


class MemorySettings(BaseModel):
    enabled: bool = False
    preference_enabled: bool = False
    episodic_enabled: bool = False
    semantic_enabled: bool = False


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    context_post_id: str | None = Field(default=None, max_length=128)
    context_comment_id: str | None = Field(default=None, max_length=128)
    command: dict[str, Any] | None = None


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    execution_id: str | None = None
    approval_id: str | None = None
    goal: str = ""
    status: str
    execution_path: str = "ORCHESTRATED"
    workload_lane: str = "WRITE"
    summary: str | None = None
    final_response: str | None = None
    error_code: str | None = None
    error: str | None = None
    trace_id: str | None = None
    budget: dict[str, int | None] = {}
    timing: dict[str, int | None] = {}
    performance: dict[str, Any] = {}
    steps: list[dict[str, object]] = []
    artifacts: list[dict[str, Any]] = []
    partial_results: dict[str, Any] = {}
    approval: object | None = None
    created_at: str = ""
    updated_at: str = ""
    # Mid-turn injection link: this Run is queued behind the named working
    # Run of the same conversation and starts once that parent ends.
    follow_up_of: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")


# ── Helpers ──────────────────────────────────────────────────────

def _get_auth(request: Request) -> AuthContext:
    auth = getattr(request.state, "auth_context", None)
    if auth is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def deterministic_conversation_title(content: str, *, max_length: int = 64) -> str:
    """Build a stable first-message title without an additional model call."""

    normalized = re.sub(r"\s+", " ", str(content or "").strip())
    if not normalized:
        return ""
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[:max(1, max_length - 1)].rstrip()}…"


def _has_custom_conversation_title(title: Any) -> bool:
    normalized = str(title or "").strip().casefold()
    return normalized not in {"", "new conversation", "新会话", "greenbook agent"}


async def _set_title_from_first_message(
    request: Request,
    session: SessionContext,
    content: str,
    auth: AuthContext,
) -> None:
    """Persist a deterministic title only for an untitled first message.

    A title projection failure must never block the durable user message or
    create a second runtime path.
    """

    title = deterministic_conversation_title(content)
    if not title:
        return
    service = getattr(request.app.state, "conversation_service", None)
    try:
        if service is not None:
            conversation = await service.get_conversation(
                session.conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
            if conversation is None or _has_custom_conversation_title(conversation.get("title")):
                return
            messages = await service.list_messages(
                session.conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
            if any(str(item.get("role") or "") == "user" for item in messages):
                return
            await service.save_session(session, title=title)
            return

        store = getattr(request.app.state, "conversation_store", {})
        record = store.get(session.conversation_id)
        if not isinstance(record, dict):
            return
        if _has_custom_conversation_title(record.get("title")):
            return
        if any(
            str(item.get("role") or "") == "user"
            for item in (record.get("messages") or [])
            if isinstance(item, Mapping)
        ):
            return
        record["title"] = title
        record["updated_at"] = _now_iso()
    except Exception:  # noqa: BLE001 - title projection is non-critical
        logger.exception(
            "conversation_title_projection_failed conversation_id=%s",
            session.conversation_id,
        )


def _record_value(record: Any, key: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _run_execution_ids(run: Any) -> list[str]:
    payload = dict(_record_value(run, "payload", {}) or {})
    values: list[str] = []
    for raw in (
        _record_value(run, "execution_id", ""),
        payload.get("execution_id"),
        *(payload.get("execution_ids") or []),
    ):
        value = str(raw or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _execution_status(execution: Any) -> str:
    return str(
        getattr(getattr(execution, "status", None), "value", None)
        or getattr(execution, "status", "")
        or ""
    ).upper()


def _is_historical_waiting_run(run: Any, request: Request) -> bool:
    """Recognize the pre-recovery stale Run shape without changing its truth.

    New approval waits are projected as WAITING_APPROVAL.  The historical
    residue is specifically a RUNNING AgentRun with no claim/lease whose
    durable Execution is WAITING_APPROVAL/WAITING_HUMAN.  Keeping this
    predicate narrow prevents a valid current HITL request from being hidden.
    """

    if str(_record_value(run, "status", "") or "").upper() != RUN_RUNNING:
        return False
    if str(_record_value(run, "claimed_by", "") or "").strip():
        return False
    if str(_record_value(run, "lease_until", "") or "").strip():
        return False
    repository = getattr(request.app.state, "execution_repository", None)
    finder = getattr(repository, "find_by_id", None)
    if not callable(finder):
        return False
    for execution_id in _run_execution_ids(run):
        execution = finder(execution_id)
        if execution is not None and _execution_status(execution) in {
            "WAITING_APPROVAL",
            "WAITING_HUMAN",
        }:
            return True
    return False


def _is_historical_waiting_execution(execution_id: str, request: Request) -> bool:
    durable_store = getattr(request.app.state, "agent_run_store", None)
    list_recent = getattr(durable_store, "list_recent", None)
    if not callable(list_recent):
        return False
    for run in list_recent(limit=500):
        if execution_id not in _run_execution_ids(run):
            continue
        if _is_historical_waiting_run(run, request):
            return True
    return False


def _conversation_has_exhausted_reconciliation(
    conversation_id: str,
    request: Request,
) -> bool:
    store = getattr(request.app.state, "external_operation_store", None)
    finder = getattr(store, "find_reconciliation_needed", None)
    if not callable(finder):
        return False
    try:
        operations = finder(now="", limit=500)
    except TypeError:
        try:
            operations = finder(limit=500)
        except TypeError:
            operations = finder()
    return any(
        str(getattr(operation, "conversation_id", "") or "") == str(conversation_id)
        and is_reconciliation_exhausted(operation)
        for operation in (operations or ())
    )


def _hide_user_activity_event(
    event: UserActivityEvent,
    request: Request,
    *,
    exhausted: bool,
) -> bool:
    run_id = str(event.run_id or "")
    if run_id:
        durable_store = getattr(request.app.state, "agent_run_store", None)
        getter = getattr(durable_store, "get", None)
        run = getter(run_id) if callable(getter) else None
        if run is None:
            local_store = getattr(request.app.state, "run_store", None)
            run = local_store.get(run_id) if isinstance(local_store, Mapping) else None
        if run is not None and _is_historical_waiting_run(run, request):
            return True
    activity_type = str(
        getattr(getattr(event, "activity_type", None), "value", None)
        or getattr(event, "activity_type", "")
        or ""
    ).upper()
    return exhausted and activity_type in {"RESULT_UNKNOWN", "RECONCILING"}


def _filter_user_activity_events(
    events: Sequence[UserActivityEvent],
    request: Request,
    *,
    conversation_id: str,
) -> list[UserActivityEvent]:
    exhausted = _conversation_has_exhausted_reconciliation(conversation_id, request)
    return [
        event
        for event in events
        if not _hide_user_activity_event(event, request, exhausted=exhausted)
    ]


def _conversation_belongs_to(auth: AuthContext, conversation_data: dict[str, Any]) -> bool:
    return (
        str(conversation_data.get("user_id", "")) == auth.user_id
        and str(conversation_data.get("tenant_id", "")) == auth.tenant_id
    )


async def _get_session(request: Request, conversation_id: str) -> SessionContext:
    """Get or create a SessionContext, verifying ownership on existing sessions."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        try:
            snapshot = await conversation_service.load(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            ) from exc
        return snapshot.session

    store = request.app.state.conversation_store

    if conversation_id in store:
        data = store[conversation_id]
        if not _conversation_belongs_to(auth, data):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return SessionContext(**data)

    # Conversations are created explicitly by POST /conversations.  A guessed
    # ID must never create an implicit resource or become a side channel.
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Conversation not found",
    )


async def _save_session(request: Request, session: SessionContext) -> None:
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        await conversation_service.save_session(session)
        return
    store = request.app.state.conversation_store
    existing = store.get(session.conversation_id, {})
    store[session.conversation_id] = {
        **session.model_dump(mode="json"),
        "title": existing.get("title"),
        "created_at": existing.get("created_at", _now_iso()),
        "updated_at": _now_iso(),
    }


async def _prepare_message_history(
    request: Request,
    *,
    conversation_id: str,
    auth: AuthContext,
    content: str,
    trace_id: str,
) -> list[dict[str, str]]:
    """Load compressed durable context, then append the current user turn."""

    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        snapshot = await conversation_service.load(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        history = snapshot.history_for_model()
        await conversation_service.append_message(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            role="user",
            content=content,
            trace_id=trace_id,
        )
        return history

    msg_store = request.app.state.message_store
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in msg_store.get(conversation_id, [])
        if item.get("role") in {"user", "assistant"}
    ]
    msg_store.setdefault(conversation_id, []).append({
        "role": "user",
        "content": content,
        "created_at": _now_iso(),
    })
    return history


async def _append_agent_message(
    request: Request,
    *,
    conversation_id: str,
    auth: AuthContext,
    content: str,
    trace_id: str,
    parts: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
) -> None:
    if not content:
        return
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        await conversation_service.append_message(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            role="assistant",
            content=content,
            trace_id=trace_id,
            parts=parts,
            run_id=run_id,
            execution_id=execution_id,
        )
        return
    request.app.state.message_store.setdefault(conversation_id, []).append({
        "role": "assistant",
        "content": content,
        "trace_id": trace_id,
        "parts": list(parts or []),
        "created_at": _now_iso(),
    })


def _public_target_clarification_part(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize resolver candidates at the public message boundary.

    Resolver command models use ``id``/``kind`` while the Frontend message
    contract uses the stable selection ``identity`` and business ``type``.
    Keeping this translation here prevents runtime command fields from being
    rendered as an opaque, non-actionable clarification card.
    """

    command = raw.get("command")
    public_command = dict(command) if isinstance(command, Mapping) else {}
    requested_target = public_command.get("target")
    requested_kind = ""
    if isinstance(requested_target, Mapping):
        requested_kind = str(
            requested_target.get("resource_kind")
            or requested_target.get("kind")
            or ""
        ).upper()
    candidates: list[dict[str, Any]] = []
    raw_candidates = raw.get("candidates") or raw.get("targets") or []
    for item in raw_candidates:
        if not isinstance(item, Mapping):
            continue
        raw_kind = str(
            item.get("resource_type")
            or item.get("kind")
            or item.get("type")
            or "TASK"
        ).upper()
        kind_aliases = {
            "POST_DRAFT": "DRAFT",
            "PUBLICATION_SCHEDULE": "SCHEDULE",
        }
        public_kind = kind_aliases.get(raw_kind, raw_kind)

        # A resolver candidate may represent the owning Task/Objective while
        # its durable resource binding is the actual business identity needed
        # by the continuation.  Project that binding here; never turn an
        # Objective id into a fake POST/DRAFT/SCHEDULE id.
        metadata = item.get("metadata")
        resource_refs: list[Mapping[str, Any]] = []
        for source in (
            item.get("resource_index"),
            item.get("resource_refs"),
            metadata.get("resource_index") if isinstance(metadata, Mapping) else None,
            metadata.get("resource_refs") if isinstance(metadata, Mapping) else None,
        ):
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                resource_refs.extend(
                    value for value in source if isinstance(value, Mapping)
                )
        bound_ref: Mapping[str, Any] | None = None
        if requested_kind in {"DRAFT", "SCHEDULE", "POST"}:
            matching_refs = [
                value
                for value in resource_refs
                if kind_aliases.get(
                    str(
                        value.get("resource_type")
                        or value.get("resource_kind")
                        or value.get("kind")
                        or ""
                    ).upper(),
                    str(
                        value.get("resource_type")
                        or value.get("resource_kind")
                        or value.get("kind")
                        or ""
                    ).upper(),
                ) == requested_kind
                and str(value.get("resource_id") or value.get("id") or "").strip()
            ]
            if len(matching_refs) == 1:
                bound_ref = matching_refs[0]
            elif len(matching_refs) > 1:
                raw_resource_id = str(
                    item.get("resource_id") or item.get("target_id") or ""
                ).strip()
                bound_ref = next(
                    (
                        value
                        for value in matching_refs
                        if str(value.get("resource_id") or value.get("id") or "").strip()
                        == raw_resource_id
                    ),
                    None,
                )
            # A Task/Objective without a resource of the requested business
            # kind is not a valid selectable candidate for this operation.
            if raw_kind == "TASK" and bound_ref is None:
                continue

        resource_id = str(
            (bound_ref or {}).get("resource_id")
            or (bound_ref or {}).get("id")
            or item.get("resource_id")
            or item.get("target_id")
            or item.get("id")
            or ""
        ).strip()
        identity = str(
            resource_id if bound_ref is not None else item.get("identity")
            or resource_id
            or item.get("task_id")
            or item.get("artifact_id")
            or item.get("execution_id")
            or ""
        ).strip()
        if not identity:
            continue
        if bound_ref is not None:
            public_kind = requested_kind
        candidate: dict[str, Any] = {
            "identity": identity,
            "type": public_kind,
        }
        for key in ("task_id", "artifact_id", "execution_id"):
            value = item.get(key)
            if value:
                candidate[key] = str(value)
        if resource_id:
            candidate["resource_id"] = resource_id
        label = str(
            (bound_ref or {}).get("title")
            or (bound_ref or {}).get("label")
            or item.get("label")
            or item.get("title")
            or item.get("display_name")
            or item.get("name")
            or ""
        ).strip()
        if label:
            candidate["label"] = label[:300]
        if (bound_ref or {}).get("status") or item.get("status"):
            candidate["status"] = str(
                (bound_ref or {}).get("status") or item.get("status")
            )
        candidates.append(candidate)
    return {
        "type": "target_clarification",
        "command": public_command,
        "candidates": candidates[:12],
    }


def _command_payload(command: Any) -> dict[str, Any] | None:
    """Serialize both typed and public-dict continuation commands."""

    if command is None:
        return None
    dump = getattr(command, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(command, Mapping):
        return dict(command)
    return None


def _conversation_summary(data: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=str(data.get("conversation_id", "")),
        title=data.get("title"),
        active_draft_id=data.get("active_draft_id"),
        active_schedule_id=data.get("active_schedule_id"),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )




def _auth_store_get(store_key: str, request: Request, record_id: str) -> dict[str, Any]:
    """Get a stored record, verifying it belongs to the authenticated user."""
    auth = _get_auth(request)
    store = getattr(request.app.state, store_key)
    if record_id not in store:
        raise HTTPException(status_code=404, detail="Record not found")
    record = store[record_id]
    if not _conversation_belongs_to(auth, record):
        raise HTTPException(status_code=404, detail="Record not found")
    return record


def _run_record_from_projection(
    projection: Any,
    *,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    response = projection.assistant_response or {}
    return {
        "run_id": projection.run_id,
        "objective_id": str(getattr(projection, "objective_id", "") or ""),
        "conversation_id": projection.conversation_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": projection.status,
        "content": response.get("message") or projection.summary,
        "trace_id": projection.trace_id,
        "execution_id": projection.execution_id,
        "task_id": projection.task_id,
        "steps": list(response.get("steps") or []),
        "artifacts": list(projection.artifacts),
        "presentation": response,
        "error_code": response.get("error_code"),
        "error": response.get("error") or response.get("error_message"),
        "created_at": getattr(projection, "created_at", ""),
        "updated_at": getattr(projection, "updated_at", ""),
    }


async def _final_response_projection(
    record: dict[str, Any],
    projections: list[Any],
    *,
    request: Request,
    auth: AuthContext,
) -> str:
    """Build the user-facing terminal response from durable Objective facts.

    Execution presenter messages remain step-level detail.  A Run response is
    projected from the touched Objectives and their owned resources so one
    completed child cannot overwrite the result of its siblings.
    """
    started_at = time.perf_counter()
    try:
        from greenbook_agent_core.observability.run_metrics import record_stage
        record_stage("final_response_start", run_id=str(record.get("run_id") or ""))
    except Exception:
        pass
    status = str(record.get("status") or "").upper()
    if status not in _RUN_TERMINAL:
        return str(record.get("content") or "")
    task_id = str(
        record.get("task_id")
        or next((getattr(item, "task_id", "") for item in projections if getattr(item, "task_id", "")), "")
        or ""
    )
    provider = getattr(request.app.state, "task_provider", None)
    if not task_id or provider is None:
        return str(record.get("content") or "")
    conversation_id = str(record.get("conversation_id") or getattr(projections[0], "conversation_id", ""))
    try:
        task = await provider.get_task(
            TaskScope(
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                conversation_id=conversation_id,
            ),
            task_id,
        )
    except Exception:
        return str(record.get("content") or "")
    objectives = list(getattr(task, "objectives", ()) or ()) if task is not None else []
    if not objectives:
        return str(record.get("content") or "")
    touched_ids = {
        str(getattr(item, "objective_id", "") or "")
        for item in projections
        if str(getattr(item, "objective_id", "") or "")
    }
    # Historical single-objective projections predate objective_id.  This is
    # deliberately limited to the unambiguous one-objective case.
    if not touched_ids and len(objectives) == 1:
        touched_ids = {str(getattr(objectives[0], "objective_id", "") or "")}
    if not touched_ids:
        return str(record.get("content") or "")
    from greenbook_agent_core.task.objective_reducer import mutation_objective_is_superseded

    task_resources = {
        str(getattr(item, "resource_id", "") or ""): item
        for item in (getattr(task, "resource_index", ()) or ())
        if str(getattr(item, "resource_id", "") or "")
    }
    lines: list[str] = []
    for objective in objectives:
        objective_id = str(getattr(objective, "objective_id", "") or "")
        if objective_id not in touched_ids:
            continue
        if mutation_objective_is_superseded(objective):
            continue
        owned_ids = {
            str(value)
            for value in (getattr(objective, "related_resource_ids", ()) or ())
            if str(value)
        }
        changed_ids = {
            _resource_id_from_artifact(artifact)
            for projection in projections
            if str(getattr(projection, "objective_id", "") or "") == objective_id
            for artifact in (getattr(projection, "artifacts", ()) or ())
            if (
                _resource_id_from_artifact(artifact)
                and _resource_id_from_artifact(artifact) in owned_ids
            )
        }
        owned_resources = [task_resources[rid] for rid in owned_ids if rid in task_resources]
        changed_resources = [task_resources[rid] for rid in changed_ids if rid in task_resources]
        lines.append(_render_objective_terminal_line(objective, owned_resources, changed_resources))
    content = "\n".join(line for line in lines if line) or str(record.get("content") or "")
    try:
        from greenbook_agent_core.observability.run_metrics import (
            record_final_response,
            record_stage,
        )
        record_final_response(round((time.perf_counter() - started_at) * 1000), run_id=str(record.get("run_id") or ""))
        record_stage("final_response_finished", run_id=str(record.get("run_id") or ""))
    except Exception:
        pass
    return content


def _render_objective_terminal_line(
    objective: Any,
    owned_resources: list[Any],
    changed_resources: list[Any],
) -> str:
    label = str(
        getattr(objective, "intent", "")
        or getattr(objective, "description", "")
        or "目标"
    ).strip()
    label = label[:32]
    resources = changed_resources or owned_resources
    by_kind = {
        str(getattr(item, "resource_kind", "") or "").upper(): item
        for item in resources
    }
    schedule = by_kind.get("SCHEDULE")
    draft = by_kind.get("DRAFT")
    if schedule is None:
        schedule = next(
            (item for item in owned_resources if str(getattr(item, "resource_kind", "") or "").upper() == "SCHEDULE"),
            None,
        )
    if draft is None:
        draft = next(
            (item for item in owned_resources if str(getattr(item, "resource_kind", "") or "").upper() == "DRAFT"),
            None,
        )
    schedule_status = str(getattr(schedule, "status", "") or "").upper() if schedule is not None else ""
    schedule_state = business_state_for_resource(
        getattr(schedule, "resource_kind", "SCHEDULE") if schedule is not None else "SCHEDULE",
        schedule_status,
        getattr(schedule, "scheduled_at", "") if schedule is not None else "",
    )
    post = by_kind.get("POST")
    post_state = business_state_for_resource(
        "POST",
        getattr(post, "status", "") if post is not None else "",
    )
    if post_state == "PUBLISHED" or schedule_state == "PUBLISHED":
        return f"{label}已发布。"
    if schedule_state == "CANCELLED" or schedule_status in {"CANCELLED", "CANCELED"}:
        return f"{label}的发布已取消，草稿保留。"
    if schedule is not None:
        run_at = str(getattr(schedule, "scheduled_at", "") or "")
        timezone = str(getattr(objective, "constraints", {}).get("timezone") or "Asia/Shanghai")
        local_time = format_local_schedule_time(run_at, timezone) if run_at else "待确定"
        changed_kinds = {
            str(getattr(item, "resource_kind", "") or "").upper()
            for item in changed_resources
        }
        if changed_kinds == {"SCHEDULE"} and draft is not None:
            return f"{label}的发布时间已更新为 {local_time}。"
        return f"{label}内容已创建并安排 {local_time} 发布。"
    if draft is not None:
        return f"{label}内容已创建。"
    return f"{label}已完成。"


def _resource_id_from_artifact(artifact: Any) -> str:
    if isinstance(artifact, Mapping):
        return str(artifact.get("resource_id") or "")
    return str(getattr(artifact, "resource_id", "") or "")


def _extract_completed_turn_preference(
    app: Any,
    *,
    result: RuntimeResult,
    conversation_id: str,
    auth: AuthContext,
    message_content: str,
) -> None:
    """Classify one completed turn without turning the turn into Memory."""

    if str(result.status or "").upper() != "COMPLETED":
        return
    service = getattr(app.state, "preference_memory_service", None)
    process = getattr(service, "process_completed_turn", None)
    if not callable(process) or not str(message_content or "").strip():
        return
    try:
        extraction, record = process(
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            user_message=message_content,
        )
        if record is not None:
            logger.info(
                "preference_memory_written memory_id=%s conversation_id=%s "
                "preference_key=%s confidence=%.2f",
                record.memory_id,
                conversation_id,
                extraction.preference_key,
                extraction.confidence,
            )
    except Exception:  # noqa: BLE001 - Memory must not break turn convergence
        logger.warning(
            "preference_memory_extraction_failed conversation_id=%s",
            conversation_id,
            exc_info=True,
        )


def _extract_completed_turn_semantic(
    app: Any,
    *,
    result: RuntimeResult,
    conversation_id: str,
    auth: AuthContext,
    message_content: str,
) -> None:
    """Admit only explicit user facts after a completed turn."""

    if str(result.status or "").upper() != "COMPLETED":
        return
    service = getattr(app.state, "semantic_memory_service", None)
    process = getattr(service, "process_user_statement", None)
    if not callable(process) or not str(message_content or "").strip():
        return
    try:
        records = process(
            message_content,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        if records:
            logger.info(
                "semantic_memory_written conversation_id=%s count=%s predicates=%s",
                conversation_id,
                len(records),
                [item.metadata.get("predicate") for item in records],
            )
    except Exception:  # noqa: BLE001 - Memory must not break turn convergence
        logger.warning(
            "semantic_memory_extraction_failed conversation_id=%s",
            conversation_id,
            exc_info=True,
        )


async def _durable_run_record(
    run_id: str,
    request: Request,
    auth: AuthContext,
) -> dict[str, Any] | None:
    store = getattr(request.app.state, "execution_result_projection_store", None)
    getter = getattr(store, "get_by_run_id", None)
    if not callable(getter):
        return None
    list_getter = getattr(store, "list_by_run_id", None)
    projections = list_getter(run_id) if callable(list_getter) else []
    projection = projections[0] if projections else getter(run_id)
    if projection is None:
        durable_store = getattr(request.app.state, "agent_run_store", None)
        getter = getattr(durable_store, "get", None)
        durable_run = getter(run_id) if callable(getter) else None
        if durable_run is None:
            return None
        if (
            str(getattr(durable_run, "user_id", "")) != auth.user_id
            or str(getattr(durable_run, "tenant_id", "")) != auth.tenant_id
        ):
            return None
        return _run_record_from_durable(durable_run)

    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        conversation = await conversation_service.get_conversation(
            projection.conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        if conversation is None:
            return None
    else:
        conversation = request.app.state.conversation_store.get(
            projection.conversation_id
        )
        if conversation is None or not _conversation_belongs_to(auth, conversation):
            return None
    record = _run_record_from_projection(
        projection,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
    )
    if not projections:
        projections = [projection]
    record["content"] = await _final_response_projection(
        record,
        projections,
        request=request,
        auth=auth,
    )
    return record


# ── Tool schema for LLM ─────────────────────────────────────────


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    context_post_id: str | None = Query(default=None, max_length=128),
) -> ConversationListResponse:
    """List conversations for the authenticated user, newest first."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        owned = await conversation_service.list_conversations(
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        if context_post_id:
            owned = [
                item for item in owned
                if str(item.get("active_post_id") or "") == context_post_id
            ]
        total = len(owned)
        start = (page - 1) * size
        page_items = owned[start : start + size]
        return ConversationListResponse(
            items=[_conversation_summary(item) for item in page_items],
            page=page,
            size=size,
            total=total,
        )
    store = request.app.state.conversation_store

    # Filter and sort
    owned: list[dict[str, Any]] = [
        data for data in store.values()
        if _conversation_belongs_to(auth, data)
    ]
    owned.sort(
        key=lambda d: d.get("updated_at") or d.get("created_at") or "",
        reverse=True,
    )
    if context_post_id:
        owned = [
            item for item in owned
            if str(item.get("active_post_id") or "") == context_post_id
        ]

    total = len(owned)
    start = (page - 1) * size
    page_items = owned[start : start + size]

    return ConversationListResponse(
        items=[_conversation_summary(item) for item in page_items],
        page=page,
        size=size,
        total=total,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationSummary)
async def get_conversation(conversation_id: str, request: Request) -> ConversationSummary:
    """Validate one durable Conversation against the caller's ownership scope."""

    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        record = await conversation_service.get_conversation(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    else:
        record = getattr(request.app.state, "conversation_store", {}).get(conversation_id)
        if record is not None and not _conversation_belongs_to(auth, record):
            record = None
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return _conversation_summary(record)


@router.post("/conversations", response_model=ConversationSummary)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
) -> ConversationSummary:
    auth = _get_auth(request)
    conversation_id = str(uuid.uuid4())
    now = _now_iso()
    session = SessionContext(
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        timezone=auth.timezone,
        active_post_id=body.context_post_id,
    )
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        record = await conversation_service.create_conversation(
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            title=body.title,
            timezone=auth.timezone,
        )
        if body.context_post_id:
            await conversation_service.save_session(session)
            record = await conversation_service.get_conversation(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            ) or record
        return _conversation_summary(record)

    store = request.app.state.conversation_store
    store[conversation_id] = {
        **session.model_dump(mode="json"),
        "title": body.title,
        "created_at": now,
        "updated_at": now,
    }
    return _conversation_summary(store[conversation_id])


@router.get("/conversations/{conversation_id}/tasks")
async def list_conversation_tasks(conversation_id: str, request: Request):
    """Return the structured Task/Goal/Execution index for one conversation."""
    auth = _get_auth(request)
    session = await _get_session(request, conversation_id)
    if session.user_id != auth.user_id or session.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conversation scope mismatch")
    adapter = _conversation_runtime_adapter(request)
    return {
        "conversation_id": conversation_id,
        "items": await adapter.get_task_index(
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        ),
    }


class SemanticConfirmationControlResponse(BaseModel):
    """Public result of one Task-level confirmation CAS."""

    task_id: str
    action: SemanticConfirmationAction
    status: str
    confirmation_state: TaskConfirmationState
    task_version: int
    confirmation_version: int
    confirmed_version: int | None = None
    idempotent: bool = False
    resume_queued: bool = False
    requires_new_compilation: bool = False


async def _dispatch_confirmed_task(request: Request, task: Any, auth: AuthContext) -> bool:
    """Wake the existing durable Run, or use the embedded executor fallback.

    The normal path changes the existing AgentRun from WAITING_USER to
    ACCEPTED with a typed resume marker.  AgentRunner then calls
    ``resume_task(command=None)``; it never re-enters CommandInterpreter and it
    reuses the existing execution queue/runtime.  The direct call is retained
    for the in-memory/embedded composition used by focused tests.
    """

    task_id = str(getattr(task, "task_id", "") or "")
    run_id = str(getattr(task, "confirmation_resume_run_id", "") or "")
    marker = {
        "task_id": task_id,
        "confirmation_id": confirmation_identity(task),
        "confirmation_version": int(getattr(task, "confirmation_version", 0) or 0),
        "task_version": int(getattr(task, "version", 0) or 0),
    }
    run_store = getattr(request.app.state, "agent_run_store", None)
    if run_store is not None and run_id:
        run = run_store.get(run_id)
        if run is not None:
            payload = dict(getattr(run, "payload", {}) or {})
            existing_marker = payload.get("semantic_confirmation_resume")
            if (
                isinstance(existing_marker, dict)
                and existing_marker == marker
                and str(getattr(run, "status", "")) in {RUN_ACCEPTED, RUN_RUNNING}
            ):
                return True
            payload["semantic_confirmation_resume"] = marker
            task_ids = [str(item) for item in (payload.get("task_ids") or []) if item]
            if task_id not in task_ids:
                task_ids.append(task_id)
            payload["task_ids"] = task_ids
            mark_status = getattr(run_store, "mark_status", None)
            if callable(mark_status):
                changed = mark_status(
                    run_id,
                    RUN_ACCEPTED,
                    expected_version=getattr(run, "version", None),
                    payload=payload,
                )
                changed = await changed if asyncio.iscoroutine(changed) else changed
                if changed:
                    return True
                latest = run_store.get(run_id)
                latest_marker = (
                    dict(getattr(latest, "payload", {}) or {}).get(
                        "semantic_confirmation_resume"
                    )
                    if latest is not None
                    else None
                )
                if (
                    isinstance(latest_marker, dict)
                    and latest_marker == marker
                    and str(getattr(latest, "status", "")) in {RUN_ACCEPTED, RUN_RUNNING}
                ):
                    return True

    executor = getattr(request.app.state, "action_loop_executor", None)
    resume = getattr(executor, "resume_task", None)
    if not callable(resume):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Semantic confirmation resume is unavailable.",
        )
    try:
        session = await _get_session(request, str(getattr(task, "conversation_id", "") or ""))
    except HTTPException:
        # Scope has already been checked against the canonical Task.  This
        # fallback only supports embedded tests/compositions without a
        # ConversationService or conversation read model.
        session = SessionContext(
            conversation_id=str(getattr(task, "conversation_id", "") or ""),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            timezone=auth.timezone,
        )
    result = resume(
        task_id=task_id,
        conversation_id=str(getattr(task, "conversation_id", "") or ""),
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        run_id=run_id or str(uuid.uuid4()),
        trace_id=run_id or str(uuid.uuid4()),
        session=session,
        timezone=session.timezone,
        mcp=getattr(request.app.state, "mcp", None),
        auth=auth,
        activity_callback=None,
        completion_callback=None,
        command=None,
        expected_confirmation_id=marker["confirmation_id"],
        expected_confirmation_version=marker["confirmation_version"],
        expected_task_version=marker["task_version"],
    )
    if asyncio.iscoroutine(result):
        await result
    return True


@router.post(
    "/tasks/{task_id}/semantic-confirmation",
    response_model=SemanticConfirmationControlResponse,
)
async def control_semantic_confirmation(
    task_id: str,
    body: SemanticConfirmationControl,
    request: Request,
) -> SemanticConfirmationControlResponse:
    """Apply a typed Task confirmation control; never route it as chat."""

    auth = _get_auth(request)
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task confirmation service is unavailable.",
        )
    get_task = getattr(manager, "get_task", None)
    if not callable(get_task):
        raise HTTPException(status_code=503, detail="Task confirmation service is unavailable.")
    task = get_task(
        task_id,
        conversation_id=None,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
    )
    task = await task if asyncio.iscoroutine(task) else task
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    try:
        if body.action == SemanticConfirmationAction.CONFIRM:
            transition = await manager.confirm_task_transition(
                task_id,
                expected_confirmation_version=body.expected_confirmation_version,
                expected_task_version=body.expected_task_version,
                expected_confirmation_id=body.confirmation_id,
            )
            resume_queued = False
            if transition.changed:
                resume_queued = await _dispatch_confirmed_task(request, transition.task, auth)
            task = transition.task
            return SemanticConfirmationControlResponse(
                task_id=task.task_id,
                action=body.action,
                status="CONFIRMED",
                confirmation_state=task.confirmation_state,
                task_version=task.version,
                confirmation_version=task.confirmation_version,
                confirmed_version=task.confirmed_version,
                idempotent=not transition.changed,
                resume_queued=resume_queued,
            )
        if body.action == SemanticConfirmationAction.CANCEL:
            transition = await manager.cancel_confirmation_transition(
                task_id,
                expected_confirmation_version=body.expected_confirmation_version,
                expected_task_version=body.expected_task_version,
                expected_confirmation_id=body.confirmation_id,
            )
            task = transition.task
            return SemanticConfirmationControlResponse(
                task_id=task.task_id,
                action=body.action,
                status="CANCELLED",
                confirmation_state=task.confirmation_state,
                task_version=task.version,
                confirmation_version=task.confirmation_version,
                confirmed_version=task.confirmed_version,
                idempotent=not transition.changed,
            )

        task = await manager.supersede_confirmation(
            task_id,
            expected_confirmation_version=body.expected_confirmation_version,
            expected_task_version=body.expected_task_version,
            expected_confirmation_id=body.confirmation_id,
        )
        return SemanticConfirmationControlResponse(
            task_id=task.task_id,
            action=body.action,
            status="SUPERSEDED",
            confirmation_state=task.confirmation_state,
            task_version=task.version,
            confirmation_version=task.confirmation_version,
            confirmed_version=task.confirmed_version,
            requires_new_compilation=True,
        )
    except TaskConfirmationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


class RunAcceptedResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    events_url: str
    execution_id: str | None = None
    execution_ids: list[str] = []
    task_ids: list[str] = []
    execution_events_url: str | None = None
    error_code: str | None = None
    error: str | None = None
    replayed: bool = False
    # First semantic capability the AgentLoop decided; lets the frontend show
    # a meaningful activity ("正在生成内容…") immediately on the 202 response.
    first_capability: str | None = None
    # Immediate-accept marker: the request was durably accepted; actual Agent
    # results follow via /runs/{run_id}/events and the message projection.
    created_at: str | None = None
    # Mid-turn injection: when a working Run already exists in this
    # conversation, the new message is queued behind it (nanobot-style). The
    # frontend shows a "已收到你的补充" hint on the parent card instead of a
    # second parallel card; the parent Run emits FOLLOW_UP_QUEUED.
    follow_up_of: str | None = None
    # Public-safe business fact stream.  ``events_url`` remains the legacy
    # Run/Execution compatibility feed and is not the ordinary user UI source.
    activities_url: str | None = None


class UserActivityListResponse(BaseModel):
    """Replayable, ownership-scoped public Activity events for one conversation."""

    items: list[UserActivityEvent] = Field(default_factory=list)
    next_cursor: int = 0


def _conversation_runtime_adapter(request: Request) -> ConversationRuntimeAdapter:
    """Return the lifespan-wired adapter, with a test-safe lazy fallback."""
    adapter = getattr(request.app.state, "conversation_runtime_adapter", None)
    if adapter is None:
        adapter = ConversationRuntimeAdapter(
            runtime_service=getattr(request.app.state, "runtime_agent_service", None),
            execution_repository=getattr(
                request.app.state, "execution_repository", None,
            ),
        )
        request.app.state.conversation_runtime_adapter = adapter
    return adapter


def _runtime_events(result: RuntimeResult, run_id: str) -> list[dict[str, Any]]:
    """Project Runtime events into the legacy run-event envelope."""
    projected: list[dict[str, Any]] = []
    for raw in result.events:
        if isinstance(raw, dict):
            event_type = raw.get("event") or raw.get("event_type") or "RUNTIME_EVENT"
            payload = raw.get("data")
            if not isinstance(payload, dict):
                payload = raw.get("payload")
            if not isinstance(payload, dict):
                payload = {}
        else:
            event_type = getattr(raw, "event_type", "RUNTIME_EVENT")
            event_type = getattr(event_type, "value", event_type)
            payload = getattr(raw, "payload", {})
            if not isinstance(payload, dict):
                payload = {}
        projected.append({
            "event": str(event_type),
            "data": {"run_id": run_id, **payload},
        })

    if projected:
        return projected

    terminal_event = (
        "RUN_COMPLETED" if result.status == "COMPLETED"
        else "RUN_FAILED" if result.status == "FAILED"
        else "RUN_STARTED"
    )
    data: dict[str, Any] = {"run_id": run_id, "status": result.status}
    if result.error_code:
        data["code"] = result.error_code
    if result.error_message:
        data["message"] = result.error_message
    return [{"event": terminal_event, "data": data}]


def _app_as_request(app: Any) -> Any:
    """Adapt an app object to the request-shaped helpers (app.state access)."""

    if getattr(app, "app", None) is not None or not hasattr(app, "state"):
        return app
    return SimpleNamespace(app=app)


def _runtime_run_record(
    result: RuntimeResult,
    *,
    run_id: str,
    conversation_id: str,
    user_id: str,
    tenant_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Create a compatibility projection; Runtime remains canonical state."""
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": result.status,
        "content": result.content,
        "error_code": result.error_code or None,
        "error": result.error_message or result.error or None,
        "trace_id": trace_id,
        "tool_rounds": result.tool_rounds,
        "events": _runtime_events(result, run_id),
        "execution_id": result.execution_id,
        "execution_ids": list((result.partial_results or {}).get("execution_ids", [])),
        "task_ids": list((result.partial_results or {}).get("task_ids", [])),
        "partial_results": result.partial_results or {},
        "agent_timeline": list((result.partial_results or {}).get("nodes", [])),
        "plan_id": result.plan_id,
        "task_id": result.task_id,
        "steps": list(result.steps),
        "artifacts": list(result.artifacts),
        "approval_id": result.approval_id,
        "created_at": getattr(result, "created_at", "") or _now_iso(),
        "updated_at": getattr(result, "updated_at", "") or _now_iso(),
    }


async def handle_run_result(
    app: Any,
    result: RuntimeResult,
    *,
    conversation_id: str,
    auth: AuthContext,
    session: SessionContext,
    message_content: str,
    trace_id: str,
) -> RunAcceptedResponse:
    """Project one finished Agent Run result.

    Shared by the synchronous POST path and the background Agent runner; the
    runner receives the same projection (assistant message, approval capture,
    run_store, completion publisher) so both paths stay equivalent.
    """

    run_id = result.run_id or ""
    if not result.status:
        result.status = "COMPLETED" if result.success else "FAILED"
    if not result.run_id:
        result.run_id = run_id
    if not result.trace_id:
        result.trace_id = trace_id

    logger.warning(
        "Agent runtime result run_id=%s status=%s error_code=%s execution_id=%s error=%s",
        run_id,
        result.status,
        result.error_code or "",
        result.execution_id or "",
        result.error_message or result.error or "",
    )

    if result.execution_id:
        link_adapter = getattr(app.state, "run_execution_adapter", None)
        if link_adapter is None:
            link_adapter = RunExecutionAdapter()
            app.state.run_execution_adapter = link_adapter
        link_adapter.bind_run_execution(
            run_id,
            result.execution_id,
            conversation_id=conversation_id,
            task_id=result.task_id,
        )

    approval_service = getattr(app.state, "approval_runtime_service", None)
    if approval_service is not None:
        # The durable Runtime may materialize approval before returning its
        # result envelope. Recover the existing row by execution identity so
        # projections never downgrade an approval wait to WAITING_USER and
        # never create a second approval record.
        approval = None
        if result.execution_id:
            approval = await approval_service.get_for_execution(result.execution_id)
            if approval is not None:
                result.approval_id = approval.approval_id
                result.approval_data = {
                    "approval_id": approval.approval_id,
                    "execution_id": approval.execution_id,
                    "operation": approval.operation,
                    "resource_id": approval.resource_id,
                    "message": approval.message,
                    "payload": dict(approval.payload or {}),
                }
        if approval is None:
            approval = await approval_service.capture_result(
                result,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        if approval is not None:
            result.approval_id = approval.approval_id
            session.pending_approval = PendingApproval(
                approval_id=approval.approval_id,
                operation=approval.operation,
                resource_id=approval.resource_id,
                description=approval.message,
            )
    # Terminal executions are projected by CompletionPublisher below.  Saving
    # the request-time session here would race that projection and overwrite
    # newly-bound resource pointers with the stale NULL values captured before
    # the worker ran.  Waiting outcomes still need their approval pointer.
    if not (
        result.execution_id
        and result.status in {"COMPLETED", "FAILED", "CANCELLED"}
        and getattr(app.state, "execution_completion_publisher", None) is not None
    ):
        await _save_session(_app_as_request(app), session)
    projected_terminal_result = False
    completion_publisher = getattr(
        app.state,
        "execution_completion_publisher",
        None,
    )
    if (
        completion_publisher is not None
        and result.execution_id
        and result.status in {"COMPLETED", "FAILED", "CANCELLED"}
    ):
        await completion_publisher(
            ExecutionQueueMessage(
                execution_id=result.execution_id,
                trace_id=trace_id,
                payload={
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "task_id": result.task_id,
                    "objective_id": str(
                        getattr(
                            getattr(
                                getattr(app.state, "execution_repository", None),
                                "find_by_id",
                                lambda _id: None,
                            )(result.execution_id),
                            "objective_id",
                            "",
                        )
                        or ""
                    ),
                    "user_message": message_content,
                    "timezone": session.timezone,
                    "auth_context": {
                        "user_id": auth.user_id,
                        "tenant_id": auth.tenant_id,
                        "roles": list(auth.roles),
                        "timezone": session.timezone,
                    },
                },
            ),
            result,
            auth,
        )
        projected_terminal_result = True
    if not projected_terminal_result:
        clarification = (result.partial_results or {}).get("clarification")
        message_parts: list[dict[str, Any]] = []
        user_facing_interaction = (result.partial_results or {}).get(
            "user_facing_interaction"
        )
        if isinstance(user_facing_interaction, dict):
            message_parts.append({
                "type": "user_facing_interaction",
                "interaction": user_facing_interaction,
            })
        if isinstance(clarification, dict):
            message_parts.append(_public_target_clarification_part(clarification))
        policy_decision = (result.partial_results or {}).get("policy_decision")
        audit_event = (result.partial_results or {}).get("audit_event")
        if isinstance(policy_decision, dict) or isinstance(audit_event, dict):
            message_parts.append({
                "type": "policy_decision",
                "policy_decision": policy_decision or {},
                "audit_event": audit_event or {},
            })
        await _append_agent_message(
            _app_as_request(app),
            conversation_id=conversation_id,
            auth=auth,
            content=(
                result.content
                or result.error_message
                or result.error
                or "任务没有完成，请重试或换个说法。"
            ),
            trace_id=trace_id,
            parts=message_parts or None,
            run_id=run_id,
            execution_id=result.execution_id,
        )

    # CompletionPublisher performs this same projection for queued work.  It
    # is intentionally invoked here as well for direct and waiting outcomes;
    # the durable source key makes repeated delivery idempotent.
    activity_publisher = getattr(app.state, "user_activity_publisher", None)
    if activity_publisher is not None:
        try:
            activity_publisher.publish_runtime_result(
                result,
                conversation_id=conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=run_id or None,
            )
        except Exception:
            # Never retry or relabel a completed Java side effect merely
            # because its public progress projection could not be stored.
            logger.exception("User activity result projection failed run_id=%s", run_id)
    if result.status == "COMPLETED":
        # Reload after terminal projection so this bookkeeping update cannot
        # clobber active Task/Draft/Schedule pointers written by the projector.
        conversation_service = getattr(app.state, "conversation_service", None)
        if conversation_service is not None:
            try:
                snapshot = await conversation_service.load(
                    conversation_id,
                    user_id=auth.user_id,
                    tenant_id=auth.tenant_id,
                )
                session = snapshot.session
            except ConversationNotFoundError:
                pass
        session.last_successful_run_id = run_id
        await _save_session(_app_as_request(app), session)

    _extract_completed_turn_preference(
        app,
        result=result,
        conversation_id=conversation_id,
        auth=auth,
        message_content=message_content,
    )
    _extract_completed_turn_semantic(
        app,
        result=result,
        conversation_id=conversation_id,
        auth=auth,
        message_content=message_content,
    )

    app.state.run_store[run_id] = _runtime_run_record(
        result,
        run_id=run_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        trace_id=trace_id,
    )

    return RunAcceptedResponse(
        run_id=run_id,
        conversation_id=conversation_id,
        status=result.status,
        events_url=(
            f"/api/v1/agent/executions/{result.execution_id}/events"
            if result.execution_id
            else f"/api/v1/agent/runs/{run_id}"
        ),
        execution_id=result.execution_id,
        execution_ids=list((result.partial_results or {}).get("execution_ids", [])),
        task_ids=list((result.partial_results or {}).get("task_ids", [])),
        execution_events_url=(
            f"/api/v1/agent/executions/{result.execution_id}/events"
            if result.execution_id else None
        ),
        error_code=result.error_code or None,
        error=result.error_message or result.error or None,
        first_capability=(result.partial_results or {}).get("first_capability") or None,
        activities_url=(
            f"/api/v1/agent/conversations/{conversation_id}/activities/stream"
        ),
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=RunAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
):
    auth = _get_auth(request)
    session = await _get_session(request, conversation_id)
    if body.context_post_id and session.active_post_id != body.context_post_id:
        # Keep the explicit detail-page binding durable for the next turn;
        # target resolution may then treat "this post" as a single resource.
        session.active_post_id = body.context_post_id
        await _save_session(request, session)
    # Immediate Accept is the only production new-request path.  The former
    # synchronous adapter call remains below solely for an explicit recovery
    # fixture, never as a silent runtime fallback.
    return await _send_runtime_message_async(
        conversation_id,
        body,
        request,
        auth,
        session,
    )


def _run_record_from_durable(run: Any) -> dict[str, Any]:
    payload = dict(getattr(run, "payload", {}) or {})
    return {
        "run_id": run.run_id,
        "conversation_id": run.conversation_id,
        "user_id": run.user_id,
        "tenant_id": run.tenant_id,
        "status": run.status,
        "content": str(payload.get("message") or ""),
        "trace_id": str(payload.get("trace_id") or ""),
        "error_code": run.error_code or None,
        "error": run.error_message or None,
        "execution_id": str(payload.get("execution_id") or "") or None,
        "approval_id": str(payload.get("approval_id") or "") or None,
        "task_ids": list(payload.get("task_ids") or []),
        "execution_ids": list(payload.get("execution_ids") or []),
        "performance": dict(payload.get("performance") or {}),
        "follow_up_of": str(payload.get("follow_up_of") or "") or None,
        "created_at": getattr(run, "created_at", ""),
        "updated_at": getattr(run, "updated_at", ""),
    }


def _accepted_response_from_durable(run: Any, *, replayed: bool = False) -> RunAcceptedResponse:
    """Project an already-accepted durable Run without creating a new one."""
    payload = dict(getattr(run, "payload", {}) or {})
    run_id = str(getattr(run, "run_id", "") or "")
    conversation_id = str(getattr(run, "conversation_id", "") or "")
    return RunAcceptedResponse(
        run_id=run_id,
        conversation_id=conversation_id,
        status=str(getattr(run, "status", RUN_ACCEPTED) or RUN_ACCEPTED),
        events_url=f"/api/v1/agent/runs/{run_id}/stream",
        execution_id=str(payload.get("execution_id") or "") or None,
        execution_ids=list(payload.get("execution_ids") or []),
        task_ids=list(payload.get("task_ids") or []),
        error_code=str(getattr(run, "error_code", "") or "") or None,
        error=str(getattr(run, "error_message", "") or "") or None,
        replayed=replayed,
        created_at=str(getattr(run, "created_at", "") or "") or None,
        follow_up_of=str(payload.get("follow_up_of") or "") or None,
        activities_url=(
            f"/api/v1/agent/conversations/{conversation_id}/activities/stream"
        ),
    )


async def _send_runtime_message_async(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
    auth: AuthContext,
    session: SessionContext,
) -> RunAcceptedResponse:
    """Immediate-accept path: persist the message and a durable Run, return
    202 without waiting for the first-turn LLM reasoning.

    The background Agent runner claims the ACCEPTED Run and executes the same
    ``adapter.execute`` path; its result is projected through
    ``handle_run_result`` (assistant message, run_store, completion publisher).
    The Run row is committed before the 202, so a crash after the response can
    still be recovered from the durable run state.
    """
    if body.timezone:
        session.timezone = body.timezone
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    run_store = getattr(request.app.state, "agent_run_store", None)
    if run_store is None:
        # Phase 4.2: no silent sync fallback. Immediate-accept is the only
        # production path; without the durable Run store the request fails
        # closed instead of running synchronously on the request thread.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Immediate-accept Agent runtime is unavailable.",
        )
    if idempotency_key:
        find_existing = getattr(run_store, "get_by_idempotency_key", None)
        if callable(find_existing):
            existing = find_existing(
                conversation_id=conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return _accepted_response_from_durable(existing, replayed=True)
    await _set_title_from_first_message(request, session, body.content, auth)
    run_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    try:
        from greenbook_agent_core.observability.run_metrics import record_stage
        record_stage("api_received", run_id=run_id)
    except Exception:
        pass
    # A new user message supersedes any unanswered clarification/waiting Run
    # in this conversation: the user moved on instead of answering it.  Leave
    # the old Run durable as CANCELLED (auditable history) but stop it from
    # lingering as an active concurrent card (design goal 0813 — the panel
    # shows what is happening now, not stale waiting cards).
    list_recent = getattr(run_store, "list_recent", None)
    follow_up_of = ""
    if callable(list_recent):
        for previous in list_recent(limit=100):
            same_scope = (
                str(previous.conversation_id or "") == conversation_id
                and str(previous.user_id or "") == auth.user_id
                and str(previous.tenant_id or "") == auth.tenant_id
            )
            if (
                same_scope
                and str(previous.status or "") in {"WAITING_USER", "WAITING_HUMAN"}
            ):
                run_store.mark_status(
                    previous.run_id,
                    RUN_CANCELLED,
                    error_code="SUPERSEDED_BY_NEW_MESSAGE",
                    error_message="已由新的用户消息取代。",
                )
                continue
            # Mid-turn injection: only queue behind a Run that is still working
            # in THIS conversation. Never follow a Run of another user or
            # another conversation (cross-scope links would deadlock the
            # runner's follow-up serialization).
            if (
                not follow_up_of
                and same_scope
                and str(previous.status or "") in RUN_WORKING
            ):
                follow_up_of = str(previous.run_id or "")

    payload = {
        "message": body.content,
        "command": _command_payload(body.command),
        "session": session.model_dump(mode="json"),
        "roles": list(auth.roles),
        "timezone": session.timezone,
        "trace_id": trace_id,
        "idempotency_key": idempotency_key,
        # Short-lived Java JWT used by the background runner to call the
        # Java business APIs; same pattern as the worker access token.
        "access_token": str(auth.raw_access_token or ""),
    }
    if follow_up_of:
        payload["follow_up_of"] = follow_up_of

    persisted_run = run_store.create(AgentRun(
        run_id=run_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        status=RUN_ACCEPTED,
        payload=payload,
    ))
    if str(getattr(persisted_run, "run_id", "") or "") != run_id:
        return _accepted_response_from_durable(persisted_run, replayed=True)
    await _prepare_message_history(
        request,
        conversation_id=conversation_id,
        auth=auth,
        content=body.content,
        trace_id=trace_id,
    )
    await _save_session(request, session)
    if follow_up_of:
        event_store = getattr(request.app.state, "agent_run_event_store", None)
        if event_store is not None:
            event_store.append(
                follow_up_of,
                EVENT_FOLLOW_UP_QUEUED,
                {
                    "run_id": follow_up_of,
                    "follow_up_run_id": run_id,
                    "message": body.content[:200],
                },
            )
    return RunAcceptedResponse(
        run_id=run_id,
        conversation_id=conversation_id,
        status=RUN_ACCEPTED,
        events_url=f"/api/v1/agent/runs/{run_id}/stream",
        created_at=datetime.now(UTC).isoformat(),
        follow_up_of=follow_up_of or None,
        activities_url=(
            f"/api/v1/agent/conversations/{conversation_id}/activities/stream"
        ),
    )


@router.get("/runs/{run_id}/stream")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    """SSE feed of Run-level business activity (semantic action, waiting,
    completed, failed). The first meaningful activity is pushed before any
    Execution exists, so the frontend never depends on execution_id."""
    event_store = getattr(request.app.state, "agent_run_event_store", None)
    run_store = getattr(request.app.state, "agent_run_store", None)
    if event_store is None or run_store is None:
        raise HTTPException(status_code=404, detail="Run event stream is not configured.")
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    auth = _get_auth(request)
    if run.user_id != auth.user_id or run.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="Run ownership mismatch.")
    if _is_historical_waiting_run(run, request):
        raise HTTPException(status_code=404, detail="Run not found.")

    async def body() -> AsyncIterator[str]:
        cursor = 0
        while not await request.is_disconnected():
            events = event_store.list_since(run_id, after_event_id=cursor)
            for event in events:
                cursor = event.event_id
                payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"event: {event.event_type}\ndata: {payload}\n\n"
            latest = run_store.get(run_id)
            if latest is not None and latest.status in _RUN_TERMINAL:
                if not events:
                    yield f"event: {latest.status}\ndata: {json.dumps({'run_id': run_id, 'status': latest.status}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _activity_cursor(*values: object) -> int:
    """Parse client replay cursors without turning malformed headers into 500s."""

    parsed: list[int] = []
    for value in values:
        try:
            parsed.append(max(0, int(str(value or "0").strip())))
        except (TypeError, ValueError):
            continue
    return max(parsed, default=0)


@router.get(
    "/conversations/{conversation_id}/activities",
    response_model=UserActivityListResponse,
)
async def list_user_activities(
    conversation_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> UserActivityListResponse:
    """Return durable user-facing business activity; never Runtime internals."""

    auth = _get_auth(request)
    # ConversationService performs the ownership check.  Do not leak an
    # activity stream merely because a caller can guess a conversation UUID.
    await _get_session(request, conversation_id)
    store = getattr(request.app.state, "user_activity_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User activity projection is unavailable.",
        )
    items = store.list_since(
        conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    visible_items = _filter_user_activity_events(
        items,
        request,
        conversation_id=conversation_id,
    )
    return UserActivityListResponse(
        items=visible_items,
        # Advance over hidden historical rows as well, otherwise a client
        # polling after the last visible event would replay the same residue
        # forever.
        next_cursor=(items[-1].sequence if items else after_sequence),
    )


@router.get("/conversations/{conversation_id}/activities/stream")
async def stream_user_activities(
    conversation_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Replayable SSE feed of public-safe Activity facts.

    The stream deliberately stays open after a Run finishes because a
    conversation may resume an existing Task in a later turn.  ``sequence``
    is a durable, monotonic cursor and is emitted as the SSE event id.
    """

    auth = _get_auth(request)
    await _get_session(request, conversation_id)
    store = getattr(request.app.state, "user_activity_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User activity projection is unavailable.",
        )
    initial_cursor = _activity_cursor(after_sequence, last_event_id)

    async def body() -> AsyncIterator[str]:
        cursor = initial_cursor
        keepalive_at = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            events = store.list_since(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                after_sequence=cursor,
                limit=200,
            )
            visible_events = _filter_user_activity_events(
                events,
                request,
                conversation_id=conversation_id,
            )
            visible_sequences = {event.sequence for event in visible_events}
            for event in events:
                cursor = event.sequence
                if event.sequence not in visible_sequences:
                    continue
                payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {event.sequence}\nevent: user_activity\ndata: {payload}\n\n"
            now = asyncio.get_running_loop().time()
            if not events and now - keepalive_at >= 15:
                yield ": keepalive\n\n"
                keepalive_at = now
            elif events:
                keepalive_at = now
            await asyncio.sleep(0.3)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str, request: Request) -> list[MessageView]:
    """Get message history for a conversation (ownership-verified)."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        try:
            messages = await conversation_service.list_messages(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return [
            MessageView(
                message_id=item.get("message_id", str(uuid.uuid4())),
                role=item["role"],
                content=item["content"],
                parts=list(item.get("parts") or []),
                run_id=item.get("run_id"),
                execution_id=item.get("execution_id"),
                trace_id=item.get("trace_id"),
                created_at=item.get("created_at", ""),
            )
            for item in messages
        ]

    store = request.app.state.conversation_store
    if conversation_id not in store:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not _conversation_belongs_to(auth, store[conversation_id]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg_store = request.app.state.message_store
    return [
        MessageView(message_id=m.get("message_id", str(uuid.uuid4())),
                    role=m["role"], content=m["content"],
                    trace_id=m.get("trace_id"), created_at=m.get("created_at", ""))
        for m in msg_store.get(conversation_id, [])
    ]


@router.get("/memory/settings")
async def get_memory_settings(request: Request) -> MemorySettings:
    _get_auth(request)
    enabled = bool(getattr(request.app.state, "memory_enabled", False))
    return MemorySettings(
        enabled=enabled,
        preference_enabled=enabled,
        semantic_enabled=enabled,
    )


class MemoryRecordView(BaseModel):
    memory_id: str
    memory_type: str
    content: str
    importance: float
    created_at: str
    conversation_id: str | None = None


@router.get("/memory/records")
async def get_memory_records(
    request: Request,
    limit: int = 20,
    memory_type: str = "",
) -> list[MemoryRecordView]:
    """最近记住的内容（P1 记忆可视化）：只读展示 agent 记住了什么。"""
    auth = _get_auth(request)
    store = getattr(request.app.state, "memory_store", None)
    if store is None or not hasattr(store, "search"):
        return []

    try:
        normalized_type = memory_type.strip().upper()
        parsed_type = (
            MemoryType.PREFERENCE
            if normalized_type == "PREFERENCE"
            else MemoryType(normalized_type)
        ) if normalized_type else None
    except ValueError:
        parsed_type = None
    query = MemoryQuery(
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        type=parsed_type,
        status=MemoryStatus.ACTIVE,
        limit=max(1, min(int(limit), 100)),
        sort_by="created_at",
    )
    try:
        records = await store.search(query)
    except Exception:
        return []
    return [
        MemoryRecordView(
            memory_id=record.memory_id,
            memory_type=str(record.memory_type.value if hasattr(record.memory_type, "value") else record.memory_type),
            content=str(record.content or "")[:500],
            importance=float(record.importance or 0.0),
            created_at=str(record.created_at or ""),
            conversation_id=str(record.conversation_id) if record.conversation_id else None,
        )
        for record in (records or [])
    ][:50]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> RunResponse:
    auth = _get_auth(request)
    record = request.app.state.run_store.get(run_id)
    durable_store = getattr(request.app.state, "agent_run_store", None)
    durable_get = getattr(durable_store, "get", None)
    durable_run = durable_get(run_id) if callable(durable_get) else None
    if durable_run is not None:
        if (
            durable_run.user_id != auth.user_id
            or durable_run.tenant_id != auth.tenant_id
        ):
            raise HTTPException(status_code=404, detail="Record not found")
        if _is_historical_waiting_run(durable_run, request):
            raise HTTPException(status_code=404, detail="Record not found")
        durable_record = _run_record_from_durable(durable_run)
        if record is None:
            record = durable_record
        else:
            # The local result projection owns business content, while the
            # PostgreSQL AgentRun row owns the live lifecycle.  Refresh the
            # status on every read so QUEUED/RUNNING cannot masquerade as a
            # terminal compatibility projection.
            for key in (
                "run_id",
                "conversation_id",
                "user_id",
                "tenant_id",
                "status",
                "error_code",
                "error",
                "execution_id",
                "approval_id",
                "performance",
                "created_at",
                "updated_at",
            ):
                if key in durable_record:
                    record[key] = durable_record[key]
    if record is not None and not _conversation_belongs_to(auth, record):
        # Startup reconciliation can restore a projection into the process
        # local run store without identity fields. Rebuild it from the
        # durable projection before applying the ownership decision.
        record = await _durable_run_record(run_id, request, auth)
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        request.app.state.run_store[run_id] = record
    if record is None:
        record = await _durable_run_record(run_id, request, auth)
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        request.app.state.run_store[run_id] = record

    if _is_historical_waiting_run(record, request):
        raise HTTPException(status_code=404, detail="Record not found")

    projection_store = getattr(request.app.state, "execution_result_projection_store", None)
    projection = (
        projection_store.get(record.get("execution_id"))
        if projection_store is not None and record.get("execution_id")
        else None
    )
    if projection is not None and projection.run_id == run_id:
        record.update(
            _run_record_from_projection(
                projection,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        )
        request.app.state.run_store[run_id] = record
    # Projection refresh above may update the compatibility status from the
    # terminal Execution result. Re-apply the durable Run lifecycle last;
    # queued work and continuation state are not terminal until convergence.
    durable_latest = durable_get(run_id) if callable(durable_get) else None
    if durable_latest is not None:
        durable_record = _run_record_from_durable(durable_latest)
        for key in (
            "status",
            "error_code",
            "error",
            "execution_id",
            "approval_id",
            "performance",
            "run_id",
            "conversation_id",
            "user_id",
            "tenant_id",
            "created_at",
            "updated_at",
        ):
            if key in durable_record:
                record[key] = durable_record[key]
        request.app.state.run_store[run_id] = record
    # Read-time convergence for the external-worker deployment: Executions
    # complete in the Worker process without an API-side event, and a
    # terminal Execution that never re-enters AgentLoop (e.g. a permanent
    # tool failure) would otherwise leave the durable Run stuck in RUNNING.
    # Converge here so the panel poll sees the real terminal state instead
    # of spinning forever (design goal 0813 — failures are visible).
    if str(record.get("status") or "").upper() == "RUNNING":
        converge = getattr(request.app.state, "converge_run_status", None)
        if callable(converge):
            await converge(run_id=run_id)
            durable_latest = durable_get(run_id) if callable(durable_get) else None
            if durable_latest is not None:
                durable_record = _run_record_from_durable(durable_latest)
                for key in (
                    "status",
                    "error_code",
                    "error",
                    "performance",
                    "run_id",
                    "conversation_id",
                    "user_id",
                    "tenant_id",
                    "created_at",
                    "updated_at",
                ):
                    if key in durable_record:
                        record[key] = durable_record[key]
                request.app.state.run_store[run_id] = record
    projection_store = getattr(request.app.state, "execution_result_projection_store", None)
    list_getter = getattr(projection_store, "list_by_run_id", None)
    if callable(list_getter):
        projections = list_getter(run_id)
        if projections:
            record["content"] = await _final_response_projection(
                record,
                projections,
                request=request,
                auth=auth,
            )
            request.app.state.run_store[run_id] = record
    events = record.get("events", [])
    steps: list[dict[str, object]] = list(record.get("steps", []))
    for i, evt in enumerate(events):
        if steps:
            break
        if evt.get("event") in ("TOOL_CALL_STARTED", "TOOL_CALL_COMPLETED", "TOOL_CALL_FAILED"):
            data = evt.get("data", {})
            event_name = evt.get("event")
            steps.append({
                "step_id": data.get("tool_call_id", f"step-{i}"),
                "ordinal": i + 1,
                "kind": "TOOL",
                "tool_name": data.get("tool_name", ""),
                "label": data.get("tool_name", f"Step {i+1}"),
                "status": (
                    "RUNNING" if event_name == "TOOL_CALL_STARTED"
                    else "COMPLETED" if data.get("ok") else "FAILED"
                ),
                "output": data,
            })
    content = record.get("content", "")
    approval = None
    approval_id = record.get("approval_id")
    durable_approvals = getattr(request.app.state, "approval_runtime_service", None)
    if not approval_id and durable_approvals is not None and record.get("execution_id"):
        pending = await durable_approvals.get_for_execution(record["execution_id"])
        if pending is not None:
            approval_id = pending.approval_id
            record["approval_id"] = approval_id
    if approval_id:
        approval_record = None
        if durable_approvals is not None:
            approval_record = await durable_approvals.get_request(approval_id)
        if approval_record is not None:
            auth = _get_auth(request)
            if (
                approval_record.user_id == auth.user_id
                and approval_record.tenant_id == auth.tenant_id
            ):
                approval_payload = dict(approval_record.payload or {})
                approval = {
                    "approval_id": approval_id,
                    "action": approval_record.operation,
                    "status": approval_record.status.value,
                    "description": approval_record.message,
                    "preview": {
                        "resource_id": approval_record.resource_id,
                        **{
                            key: approval_payload[key]
                            for key in (
                                "draft_id",
                                "post_id",
                                "schedule_id",
                                "goal_id",
                                "task_id",
                                "step_id",
                                "target_type",
                                "target_title",
                                "run_at",
                                "timezone",
                            )
                            if approval_payload.get(key) not in (None, "")
                        },
                    },
                    "expires_at": "",
                    "expected_run_version": 0,
                }
        else:
            approval_record = request.app.state.approval_store.get(approval_id)
        if (
            approval is None
            and isinstance(approval_record, dict)
            and _conversation_belongs_to(_get_auth(request), approval_record)
        ):
            approval_payload = dict(approval_record.get("payload") or {})
            approval = {
                "approval_id": approval_id,
                "action": approval_record.get("operation", ""),
                "status": approval_record.get("status", "PENDING"),
                "description": approval_record.get("description", ""),
                "preview": {
                    "resource_id": approval_record.get("resource_id"),
                    **{
                        key: approval_payload[key]
                        for key in (
                            "draft_id",
                            "post_id",
                            "schedule_id",
                            "goal_id",
                            "task_id",
                            "step_id",
                            "target_type",
                            "target_title",
                            "run_at",
                            "timezone",
                        )
                        if approval_payload.get(key) not in (None, "")
                    },
                },
                "expires_at": approval_record.get("expires_at", ""),
                "expected_run_version": 0,
            }
    return RunResponse(
        run_id=record["run_id"],
        conversation_id=record["conversation_id"],
        execution_id=record.get("execution_id"),
        approval_id=record.get("approval_id"),
        goal=content[:120] if content else "",
        status=record["status"],
        summary=content[:200] if content else None,
        final_response=content,
        error_code=record.get("error_code"),
        error=record.get("error"),
        trace_id=record.get("trace_id"),
        budget={
            "model_calls": None, "max_model_calls": int(os.getenv("GREENBOOK_AGENT_MAX_ITERATIONS", "24")),
            "tool_calls": record.get("tool_rounds", 0), "max_tool_calls": 30,
            "replan_count": 0, "max_replans": 0,
        },
        timing={"queue_ms": None, "model_ms": None, "tool_ms": None, "dependency_wait_ms": None, "total_ms": (record.get("performance") or {}).get("total_latency_ms")},
        performance=dict(record.get("performance") or {}),
        steps=steps,
        artifacts=list(record.get("artifacts", [])),
        partial_results=dict(record.get("partial_results", {})),
        approval=approval,
        created_at=record.get("created_at", ""),
        updated_at=record.get("updated_at", ""),
    )


@router.get("/runs")
async def list_runs(request: Request, limit: int = 30) -> list[RunResponse]:
    auth = _get_auth(request)
    run_store = request.app.state.run_store
    durable_store = getattr(request.app.state, "agent_run_store", None)
    list_recent = getattr(durable_store, "list_recent", None)
    if callable(list_recent):
        for durable_run in list_recent(limit=limit):
            if (
                durable_run.user_id == auth.user_id
                and durable_run.tenant_id == auth.tenant_id
            ):
                if _is_historical_waiting_run(durable_run, request):
                    continue
                durable_record = _run_record_from_durable(durable_run)
                existing = run_store.get(durable_run.run_id)
                if existing is None:
                    run_store[durable_run.run_id] = durable_record
                else:
                    for key in (
                        "status",
                        "error_code",
                        "error",
                        "run_id",
                        "conversation_id",
                        "user_id",
                        "tenant_id",
                        "created_at",
                        "updated_at",
                    ):
                        if key in durable_record:
                            existing[key] = durable_record[key]
    owned: list[dict[str, Any]] = []
    for record in list(run_store.values()):
        if _conversation_belongs_to(auth, record):
            if _is_historical_waiting_run(record, request):
                continue
            owned.append(record)
            continue
        run_id = str(record.get("run_id") or "")
        if not run_id:
            continue
        projected = await _durable_run_record(run_id, request, auth)
        if projected is not None:
            run_store[run_id] = projected
            owned.append(projected)
    # Read-time convergence (same rationale as get_run): converge Runs whose
    # Executions already reached a terminal state in the Worker process.
    converge = getattr(request.app.state, "converge_run_status", None)
    if callable(converge):
        for record in list(run_store.values()):
            if not _conversation_belongs_to(auth, record):
                continue
            if _is_historical_waiting_run(record, request):
                continue
            if str(record.get("status") or "").upper() != "RUNNING":
                continue
            run_id = str(record.get("run_id") or "")
            if not run_id:
                continue
            await converge(run_id=run_id)
            projected = await _durable_run_record(run_id, request, auth)
            if projected is not None:
                run_store[run_id] = projected
    projection_store = getattr(request.app.state, "execution_result_projection_store", None)
    list_projection = getattr(projection_store, "list_by_run_id", None)
    if callable(list_projection):
        for record in list(owned):
            projections = list_projection(str(record.get("run_id") or ""))
            if projections:
                record["content"] = await _final_response_projection(
                    record,
                    projections,
                    request=request,
                    auth=auth,
                )
    owned.sort(
        key=lambda r: (r.get("updated_at") or r.get("created_at") or "", r.get("run_id", "")),
        reverse=True,
    )
    return [
        RunResponse(
            run_id=r["run_id"], conversation_id=r["conversation_id"],
            execution_id=r.get("execution_id"),
            approval_id=r.get("approval_id"),
            goal="", status=r["status"],
            summary=(r.get("content") or "")[:200],
            final_response=r.get("content"),
            error_code=r.get("error_code"),
            error=r.get("error"),
            trace_id=r.get("trace_id"),
            budget={"model_calls": None, "max_model_calls": int(os.getenv("GREENBOOK_AGENT_MAX_ITERATIONS", "24")), "tool_calls": r.get("tool_rounds", 0), "max_tool_calls": 30, "replan_count": 0, "max_replans": 0},
            timing={"queue_ms": None, "model_ms": None, "tool_ms": None, "dependency_wait_ms": None, "total_ms": (r.get("performance") or {}).get("total_latency_ms")},
            performance=dict(r.get("performance") or {}),
            steps=[],
            artifacts=list(r.get("artifacts", [])),
            partial_results=dict(r.get("partial_results", {})),
            created_at=r.get("created_at", ""),
            updated_at=r.get("updated_at", ""),
        )
        for r in owned[:limit]
    ]


@router.post("/executions/{execution_id}/approve")
async def approve_execution(
    execution_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    """Approve the pending step attached to a canonical Execution."""

    # Authenticate before checking service availability: an unauthenticated
    # caller must get 401, never 503 (consistent with the run approval path).
    auth = _get_auth(request)
    service = getattr(request.app.state, "approval_runtime_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Durable approval service unavailable")
    pending = await service.get_for_execution(execution_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if _is_historical_waiting_execution(execution_id, request):
        raise HTTPException(status_code=404, detail="Approval request not found")
    try:
        result = await service.decide(
            pending.approval_id,
            decision=(
                ApprovalRequestStatus.APPROVED
                if body.decision == "APPROVE"
                else ApprovalRequestStatus.REJECTED
            ),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Approval request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if body.decision != "APPROVE" and result.status == RUN_CANCELLED:
        await _project_rejected_approval_task(
            request,
            pending=pending,
            result=result,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    return {
        "execution_id": execution_id,
        "approval_id": pending.approval_id,
        "status": result.status,
    }


@router.post("/runs/{run_id}/approvals/{approval_id}")
async def decide_runtime_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> RunResponse:
    record = _auth_store_get("run_store", request, run_id)
    durable_store = getattr(request.app.state, "agent_run_store", None)
    durable_run = durable_store.get(run_id) if durable_store is not None else None
    if durable_run is not None and _is_historical_waiting_run(durable_run, request):
        raise HTTPException(status_code=404, detail="Record not found")
    service = getattr(request.app.state, "approval_runtime_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Durable approval service unavailable")
    auth = _get_auth(request)
    pending = await service.get_request(approval_id)
    try:
        result = await service.decide(
            approval_id,
            decision=(
                ApprovalRequestStatus.APPROVED
                if body.decision == "APPROVE"
                else ApprovalRequestStatus.REJECTED
            ),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Approval request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record.update({
        "status": result.status,
        "content": result.content,
        "execution_id": result.execution_id or record.get("execution_id"),
    })
    # A rejected approval is an explicit terminal user decision.  The
    # execution state is already cancelled by ApprovalRuntimeService, but the
    # durable Run row is the authority read by GET /runs and must converge in
    # the same request; otherwise its WAITING_APPROVAL status overwrites the
    # compatibility projection on the next read.
    if body.decision != "APPROVE" and result.status in {RUN_CANCELLED, RUN_FAILED}:
        durable_store = getattr(request.app.state, "agent_run_store", None)
        durable_run = durable_store.get(run_id) if durable_store is not None else None
        if durable_run is not None:
            durable_store.mark_status(
                run_id,
                result.status,
                error_code=result.error_code or "",
                error_message=result.error_message or "",
                expected_version=durable_run.version,
            )
        await _project_rejected_approval_task(
            request,
            pending=pending,
            result=result,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    request.app.state.run_store[run_id] = record
    return await get_run(run_id, request)


async def _project_rejected_approval_task(
    request: Request,
    *,
    pending: Any,
    result: RuntimeResult,
    user_id: str,
    tenant_id: str,
) -> None:
    """Close the owning Task projection when approval is rejected.

    Approval rejection cancels the durable Execution synchronously, but it
    does not pass through the queue completion publisher.  Without this small
    projection bridge, the Task keeps a WAITING_HUMAN execution ref and a
    stale active_execution_id; a later clarification continuation can then
    reuse the cancelled execution instead of creating the next approval.
    """

    provider = getattr(request.app.state, "task_provider", None)
    repository = getattr(request.app.state, "execution_repository", None)
    execution_id = str(getattr(result, "execution_id", "") or "")
    if provider is None or repository is None or not execution_id:
        return
    execution = repository.find_by_id(execution_id)
    if execution is None:
        return
    task_id = str(getattr(execution, "task_id", "") or "")
    if not task_id:
        return
    step = next(
        (
            item for item in (getattr(execution, "steps", ()) or ())
            if str(getattr(item, "goal_id", "") or "")
        ),
        None,
    )
    goal_id = str(getattr(step, "goal_id", "") or "") if step is not None else ""
    conversation_id = str(getattr(pending, "conversation_id", "") or "")
    if not conversation_id:
        return
    try:
        await provider.persist_completion_projection(
            TaskScope(
                user_id=str(user_id),
                tenant_id=str(tenant_id),
                conversation_id=conversation_id,
            ),
            task_id=task_id,
            execution_id=execution_id,
            status="CANCELLED",
            artifacts=[],
            goal_id=goal_id,
        )
    except Exception:
        logger.exception(
            "Rejected approval Task projection failed execution_id=%s task_id=%s",
            execution_id,
            task_id,
        )


# ── Helpers ──────────────────────────────────────────────────────

async def _sse_stream(events: Any):
    for event in events:
        event_type = event.get("event", "message")
        data = json.dumps(event.get("data", {}), ensure_ascii=False, default=str)
        yield f"event: {event_type}\ndata: {data}\n\n"
    yield "event: done\ndata: {}\n\n"
