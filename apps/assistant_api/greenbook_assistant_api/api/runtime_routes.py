"""User-facing Execution Runtime status and event streaming API."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator
from inspect import isawaitable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from greenbook_assistant_core.execution.event_stream import subscribe_execution_events
from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.models import ExecutionStatus
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.retry_manager import RetryManager
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from pydantic import BaseModel, Field

router = APIRouter()


class ExecutionStatusResponse(BaseModel):
    execution_id: str
    status: str
    current_step: str = ""
    progress: float = Field(ge=0.0, le=1.0)
    total_steps: int = 0
    completed_steps: int = 0
    created_at: str
    updated_at: str


class StepExecutionResponse(BaseModel):
    step_execution_id: str
    step_id: str
    capability: str
    status: str
    retry_count: int
    error_code: str
    error_message: str
    started_at: str
    completed_at: str


class ExecutionStepsResponse(BaseModel):
    """Runtime step snapshot for one execution.

    The execution id is repeated in the envelope so clients cannot
    accidentally render a response for a different execution when requests
    complete out of order.
    """

    execution_id: str
    steps: list[StepExecutionResponse]


class ExecutionEventsResponse(BaseModel):
    """Historical Runtime events read from the canonical EventStore."""

    execution_id: str
    events: list[ExecutionEvent]


class ExecutionListItem(BaseModel):
    execution_id: str
    task_id: str
    plan_id: str
    status: str
    current_step: str = ""
    progress: float = Field(ge=0.0, le=1.0)
    total_steps: int = 0
    completed_steps: int = 0
    created_at: str
    updated_at: str


class ExecutionListResponse(BaseModel):
    items: list[ExecutionListItem]
    next_cursor: str | None = None


async def _is_authorized(request: Request, execution) -> bool:
    """Evaluate the configured ownership policy for one Runtime resource."""
    auth_context = getattr(request.state, "auth_context", None)
    if auth_context is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    authorizer = getattr(request.app.state, "execution_authorizer", None)
    if authorizer is None:
        raise HTTPException(
            status_code=403,
            detail="Execution authorization is not configured",
        )

    allowed = authorizer(auth_context, execution)
    if isawaitable(allowed):
        allowed = await allowed
    return bool(allowed)


async def _require_execution_access(request: Request, execution) -> None:
    """Require authentication and ownership for a Runtime execution."""
    if not await _is_authorized(request, execution):
        raise HTTPException(status_code=403, detail="Execution access denied")


async def _require_control_access(request: Request, execution) -> None:
    """Backward-compatible name for the Runtime ownership check."""
    await _require_execution_access(request, execution)


def _manager(request: Request) -> RuntimeManager:
    configured = getattr(request.app.state, "execution_runtime_manager", None)
    if configured is not None:
        return configured
    configured_state = getattr(request.app.state, "execution_state_manager", None)
    if configured_state is not None:
        return RuntimeManager(
            configured_state,
            checkpoint_store=getattr(request.app.state, "execution_checkpoint_store", None),
        )
    persistence = getattr(request.app.state, "runtime_persistence", None)
    if persistence is not None:
        state = ExecutionStateManager(
            persistence.execution_repository,
            event_store=persistence.execution_event_store,
        )
        return RuntimeManager(
            state,
            checkpoint_store=persistence.checkpoint_store,
        )
    repository = getattr(request.app.state, "execution_repository", None)
    event_store = getattr(request.app.state, "execution_event_store", None)
    state = ExecutionStateManager(repository or ExecutionRepository(), event_store=event_store)
    return RuntimeManager(
        state,
        checkpoint_store=getattr(request.app.state, "execution_checkpoint_store", None),
    )


def _execution_or_404(manager: RuntimeManager, execution_id: str):
    try:
        return manager.get_execution(execution_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Execution not found") from exc


def _current_step(execution) -> str:
    ordered = sorted(execution.steps, key=lambda item: item.ordinal)
    active = next(
        (
            step for step in sorted(execution.steps, key=lambda item: item.ordinal)
            if step.status.value in {
                "RUNNING", "PENDING", "WAITING_APPROVAL", "WAITING_HUMAN",
            }
        ),
        None,
    )
    if active is None:
        active = next(
            (step for step in ordered if step.status.value in {"FAILED", "FAILED_RETRYABLE"}),
            None,
        )
    return active.step_id if active else ""


def _execution_progress(execution) -> float:
    total = execution.total_step_count
    return execution.completed_step_count / total if total else 1.0


def _execution_cursor(execution) -> str:
    payload = json.dumps(
        {"updated_at": execution.updated_at, "execution_id": execution.execution_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_execution_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        )
        updated_at = payload["updated_at"]
        execution_id = payload["execution_id"]
        if not isinstance(updated_at, str) or not isinstance(execution_id, str):
            raise ValueError
        return updated_at, execution_id
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise HTTPException(status_code=400, detail="Invalid execution cursor") from exc


async def _authorize_execution_list(request: Request, execution) -> bool:
    """Require policy configuration and filter out other users' executions."""
    return await _is_authorized(request, execution)


@router.get("/executions", response_model=ExecutionListResponse)
async def list_executions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> ExecutionListResponse:
    """List authorized Runtime executions without consulting legacy storage."""
    manager = _manager(request)
    page_limit = limit if isinstance(limit, int) else 20
    page_cursor = cursor if isinstance(cursor, str) else None
    executions = manager.list_executions()
    executions.sort(
        key=lambda execution: (execution.updated_at, execution.execution_id),
        reverse=True,
    )

    if page_cursor is not None:
        cursor_updated_at, cursor_execution_id = _decode_execution_cursor(page_cursor)
        executions = [
            execution
            for execution in executions
            if (execution.updated_at, execution.execution_id)
            < (cursor_updated_at, cursor_execution_id)
        ]

    visible = []
    for execution in executions:
        if await _authorize_execution_list(request, execution):
            visible.append(execution)

    page = visible[:page_limit]
    next_cursor = _execution_cursor(page[-1]) if len(visible) > page_limit else None
    return ExecutionListResponse(
        items=[
            ExecutionListItem(
                execution_id=execution.execution_id,
                task_id=execution.task_id,
                plan_id=execution.plan_id,
                status=execution.status.value,
            current_step=_current_step(execution),
            progress=_execution_progress(execution),
            total_steps=execution.total_step_count,
            completed_steps=execution.completed_step_count,
                created_at=execution.created_at,
                updated_at=execution.updated_at,
            )
            for execution in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/executions/{execution_id}", response_model=ExecutionStatusResponse)
async def get_execution_status(execution_id: str, request: Request) -> ExecutionStatusResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_execution_access(request, execution)
    total = execution.total_step_count
    progress = execution.completed_step_count / total if total else 1.0
    return ExecutionStatusResponse(
        execution_id=execution.execution_id,
        status=execution.status.value,
        current_step=_current_step(execution),
        progress=progress,
        total_steps=total,
        completed_steps=execution.completed_step_count,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


@router.get("/executions/{execution_id}/steps", response_model=ExecutionStepsResponse)
async def get_execution_steps(
    execution_id: str,
    request: Request,
) -> ExecutionStepsResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_execution_access(request, execution)
    return ExecutionStepsResponse(
        execution_id=execution_id,
        steps=[
            StepExecutionResponse(
                step_execution_id=step.step_execution_id,
                step_id=step.step_id,
                capability=step.capability,
                status=step.status.value,
                retry_count=step.retry_count,
                error_code=step.error_code,
                error_message=step.error_message,
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
            for step in manager.list_steps(execution_id)
        ],
    )


@router.get("/executions/{execution_id}/events", response_model=ExecutionEventsResponse)
async def get_execution_events(
    execution_id: str,
    request: Request,
) -> ExecutionEventsResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_execution_access(request, execution)
    return ExecutionEventsResponse(
        execution_id=execution_id,
        events=manager.list_events(execution_id),
    )


async def _control_execution(
    request: Request,
    execution_id: str,
    operation,
) -> ExecutionStatusResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_control_access(request, execution)
    try:
        updated = operation(manager, execution_id)
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=409, detail=message) from exc
    total = updated.total_step_count
    progress = updated.completed_step_count / total if total else 1.0
    return ExecutionStatusResponse(
        execution_id=updated.execution_id,
        status=updated.status.value,
        current_step=_current_step(updated),
        progress=progress,
        total_steps=total,
        completed_steps=updated.completed_step_count,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.post("/executions/{execution_id}/pause", response_model=ExecutionStatusResponse)
async def pause_execution(execution_id: str, request: Request) -> ExecutionStatusResponse:
    return await _control_execution(
        request,
        execution_id,
        lambda manager, value: manager.pause_execution(value),
    )


@router.post("/executions/{execution_id}/resume", response_model=ExecutionStatusResponse)
async def resume_execution(execution_id: str, request: Request) -> ExecutionStatusResponse:
    return await _control_execution(
        request,
        execution_id,
        lambda manager, value: manager.resume_execution(value),
    )


@router.post("/executions/{execution_id}/cancel", response_model=ExecutionStatusResponse)
async def cancel_execution(execution_id: str, request: Request) -> ExecutionStatusResponse:
    return await _control_execution(
        request,
        execution_id,
        lambda manager, value: manager.cancel_execution(value),
    )


def _retry_manager(request: Request, manager: RuntimeManager) -> RetryManager:
    configured = getattr(request.app.state, "execution_retry_manager", None)
    if configured is not None:
        return configured
    state_manager = getattr(request.app.state, "execution_state_manager", None)
    if state_manager is None:
        state_manager = getattr(manager, "_state", None)
    return RetryManager(state_manager=state_manager, runtime_manager=manager)


@router.post(
    "/executions/{execution_id}/steps/{step_id}/retry",
    response_model=StepExecutionResponse,
)
async def retry_execution_step(
    execution_id: str,
    step_id: str,
    request: Request,
) -> StepExecutionResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_control_access(request, execution)
    try:
        step = _retry_manager(request, manager).retry_step(execution_id, step_id)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message.lower() else 409
        raise HTTPException(status_code=status_code, detail=message) from exc

    if step.status.value not in {"PENDING", "RUNNING"}:
        raise HTTPException(
            status_code=409,
            detail=f"Step {step_id} is not retryable",
        )
    return StepExecutionResponse(
        step_execution_id=step.step_execution_id,
        step_id=step.step_id,
        capability=step.capability,
        status=step.status.value,
        retry_count=step.retry_count,
        error_code=step.error_code,
        error_message=step.error_message,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


def _sse(event: ExecutionEvent) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"event: {event.event_type.value}\ndata: {payload}\n\n"


@router.get("/executions/{execution_id}/stream")
async def stream_execution_events(execution_id: str, request: Request) -> StreamingResponse:
    manager = _manager(request)
    execution = _execution_or_404(manager, execution_id)
    await _require_execution_access(request, execution)
    # A stream is a replayable Runtime feed, not a reduced tool-only feed.
    # Keep every lifecycle event visible so a client that connects after the
    # POST can reconstruct the complete task card and retry history.
    event_types = {event_type.value for event_type in EventType}

    async def disconnected() -> bool:
        return await request.is_disconnected()

    def status_getter() -> ExecutionStatus:
        return manager.get_execution(execution_id).status

    async def body() -> AsyncIterator[str]:
        async for event in subscribe_execution_events(
            execution_id,
            manager.event_store,
            status_getter,
            is_disconnected=disconnected,
            event_types=event_types,
        ):
            yield _sse(event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "router",
    "get_execution_status",
    "get_execution_steps",
    "get_execution_events",
    "ExecutionStepsResponse",
    "ExecutionEventsResponse",
    "stream_execution_events",
    "pause_execution",
    "resume_execution",
    "cancel_execution",
    "retry_execution_step",
]
