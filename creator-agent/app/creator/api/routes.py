from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sse_starlette import EventSourceResponse, JSONServerSentEvent

from app.core.config import Settings, get_settings
from app.creator.api.catalog import creator_workspace_catalog
from app.creator.api.composition import CreatorApiRuntime
from app.creator.api.errors import CreatorApiError, CreatorApiUnavailableError
from app.creator.api.models import (
    CreatorApiErrorBody,
    CreatorApiErrorEnvelope,
    CreatorApiPrincipal,
    CreatorApiStatusResponse,
    CreatorArtifactDetail,
    CreatorArtifactSummary,
    CreatorBranchCreateRequest,
    CreatorBranchCreateResponse,
    CreatorChannelVariantCreateRequest,
    CreatorDecisionResponseRequest,
    CreatorDecisionView,
    CreatorDraftCreateRequest,
    CreatorDraftSummary,
    CreatorDraftUpdateRequest,
    CreatorDraftVersionView,
    CreatorDraftView,
    CreatorLocalSessionResponse,
    CreatorMaterialCreateRequest,
    CreatorProjectCreateRequest,
    CreatorPublicationHandoffRequest,
    CreatorPublicationHandoffView,
    CreatorRatingRequest,
    CreatorSuggestionApplyResponse,
    CreatorSuggestionCreateRequest,
    CreatorSuggestionRejectRequest,
    CreatorTaskAcceptedResponse,
    CreatorTaskCreateRequest,
    CreatorTaskMutationResponse,
    CreatorTaskPage,
    CreatorTaskSnapshot,
    CreatorTaskVersionRequest,
    CreatorWorkspaceCatalogResponse,
)
from app.creator.api.security import create_local_basic_session, current_creator
from app.creator.domain.errors import (
    CreatorDecisionNotFoundError,
    CreatorHarnessError,
    CreatorScopeViolationError,
    CreatorTaskNotFoundError,
)
from app.creator.domain.models import CreatorTaskKind, CreatorTaskStatus
from app.creator.drafts.errors import (
    CreatorDraftError,
    CreatorDraftNotFoundError,
    CreatorDraftScopeError,
    CreatorDraftTaskNotFoundError,
)
from app.creator.publication.errors import (
    CreatorPublicationError,
    CreatorPublicationLockedError,
    CreatorPublicationNotReadyError,
)
from app.creator.studio.errors import (
    CreatorStudioConflictError,
    CreatorStudioError,
    CreatorStudioModelError,
    CreatorStudioNotFoundError,
    CreatorStudioScopeError,
)
from app.creator.studio.models import (
    CreatorBranch,
    CreatorChannelVariant,
    CreatorFeedback,
    CreatorFeedbackSummary,
    CreatorMaterial,
    CreatorProject,
    CreatorSuggestion,
)
from app.creator.studio.service import CreatorStudioService

logger = logging.getLogger(__name__)


creator_router = APIRouter(prefix="/api/v1/creator", tags=["creator"])

_IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128),
]
_Principal = Annotated[CreatorApiPrincipal, Depends(current_creator)]


def get_creator_api_runtime(request: Request) -> CreatorApiRuntime:
    runtime = getattr(request.app.state, "creator_api", None)
    if not isinstance(runtime, CreatorApiRuntime):
        raise CreatorApiUnavailableError(
            "Creator API runtime is not available",
        )
    return runtime


_Runtime = Annotated[CreatorApiRuntime, Depends(get_creator_api_runtime)]
_Settings = Annotated[Settings, Depends(get_settings)]


def _require_creator_studio(runtime: CreatorApiRuntime) -> CreatorStudioService:
    if runtime.studio is None:
        raise CreatorApiUnavailableError("Creator Studio runtime is not available")
    return runtime.studio


@creator_router.post(
    "/local-session",
    response_model=CreatorLocalSessionResponse,
)
async def create_creator_local_session(
    request: Request,
    response: Response,
    runtime: _Runtime,
    settings: _Settings,
) -> CreatorLocalSessionResponse:
    if (
        not settings.creator_local_auto_login
        or settings.creator_identity_mode.strip().lower() != "basic"
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Local auto-login is disabled",
        )
    if request.client is None or not _is_loopback_host(request.client.host):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Local auto-login is only available on the loopback interface",
        )
    token, principal = create_local_basic_session(settings)
    response.headers["Cache-Control"] = "no-store"
    return CreatorLocalSessionResponse(
        status="READY",
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        actor_id=principal.actor_id,
        display_name=principal.display_name,
        execution_mode=runtime.execution_mode,
        model_provider=runtime.model_provider,
        model_name=runtime.model_name,
        token=token,
    )


@creator_router.get("/status", response_model=CreatorApiStatusResponse)
async def creator_status(
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorApiStatusResponse:
    diagnostics = getattr(runtime.dispatcher, "diagnostics", lambda: {})()
    return CreatorApiStatusResponse(
        status="READY",
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        actor_id=principal.actor_id,
        display_name=principal.display_name,
        execution_mode=runtime.execution_mode,
        model_provider=runtime.model_provider,
        model_name=runtime.model_name,
        dispatcher_alive=diagnostics.get("dispatcher_alive"),
        dispatcher_instance_id=diagnostics.get("dispatcher_instance_id"),
        dispatcher_last_heartbeat=diagnostics.get("dispatcher_last_heartbeat"),
        dispatcher_last_claim_at=diagnostics.get("dispatcher_last_claim_at"),
        dispatcher_last_error=diagnostics.get("dispatcher_last_error"),
        active_task_count=diagnostics.get("active_task_count"),
        active_task_stacks=diagnostics.get("active_task_stacks"),
    )


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@creator_router.get(
    "/workspace",
    response_model=CreatorWorkspaceCatalogResponse,
)
async def creator_workspace(
    principal: _Principal,
    runtime: _Runtime,
    settings: _Settings,
) -> CreatorWorkspaceCatalogResponse:
    del principal
    return creator_workspace_catalog(settings, runtime)


@creator_router.get("/projects", response_model=tuple[CreatorProject, ...])
async def list_creator_projects(
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorProject, ...]:
    return await _require_creator_studio(runtime).list_projects(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
    )


@creator_router.post(
    "/projects",
    response_model=CreatorProject,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_project(
    body: CreatorProjectCreateRequest,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorProject:
    return await _require_creator_studio(runtime).create_project(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        name=body.name,
        description=body.description,
    )


@creator_router.get("/materials", response_model=tuple[CreatorMaterial, ...])
async def list_creator_materials(
    principal: _Principal,
    runtime: _Runtime,
    project_id: Annotated[str | None, Query(max_length=64)] = None,
) -> tuple[CreatorMaterial, ...]:
    return await _require_creator_studio(runtime).list_materials(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        project_id=project_id,
    )


@creator_router.post(
    "/materials",
    response_model=CreatorMaterial,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_material(
    body: CreatorMaterialCreateRequest,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorMaterial:
    return await _require_creator_studio(runtime).create_material(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        project_id=body.project_id,
        title=body.title,
        kind=body.kind,
        content_text=body.content_text,
        source_url=body.source_url,
        tags=body.tags,
    )


@creator_router.post(
    "/tasks",
    response_model=CreatorTaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_creator_task(
    request: Request,
    body: CreatorTaskCreateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorTaskAcceptedResponse:
    return await runtime.workspace.create_task(
        principal,
        body,
        idempotency_key=idempotency_key,
        trace_id=str(getattr(request.state, "trace_id", ""))[:64] or None,
    )


@creator_router.get("/tasks", response_model=CreatorTaskPage)
async def list_creator_tasks(
    principal: _Principal,
    runtime: _Runtime,
    task_status: Annotated[
        CreatorTaskStatus | None,
        Query(alias="status"),
    ] = None,
    kind: CreatorTaskKind | None = None,
    project_id: Annotated[str | None, Query(max_length=64)] = None,
    cursor: Annotated[str | None, Query(max_length=1_024)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> CreatorTaskPage:
    return await runtime.query.list_tasks(
        principal,
        status=task_status,
        kind=kind,
        project_id=project_id,
        cursor=cursor,
        limit=limit,
    )


@creator_router.get(
    "/tasks/{task_id}",
    response_model=CreatorTaskSnapshot,
)
async def get_creator_task(
    task_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorTaskSnapshot:
    return await runtime.query.get_task_snapshot(principal, task_id)


@creator_router.post(
    "/tasks/{task_id}/cancel",
    response_model=CreatorTaskMutationResponse,
)
async def cancel_creator_task(
    task_id: str,
    body: CreatorTaskVersionRequest,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorTaskMutationResponse:
    return await runtime.workspace.cancel_task(
        principal,
        task_id=task_id,
        request=body,
    )


@creator_router.post(
    "/tasks/{task_id}/retry",
    response_model=CreatorTaskMutationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_creator_task(
    task_id: str,
    body: CreatorTaskVersionRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorTaskMutationResponse:
    return await runtime.workspace.retry_task(
        principal,
        task_id=task_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@creator_router.get(
    "/tasks/{task_id}/decisions",
    response_model=tuple[CreatorDecisionView, ...],
)
async def list_creator_decisions(
    task_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorDecisionView, ...]:
    return await runtime.query.list_decisions(principal, task_id)


@creator_router.get(
    "/tasks/{task_id}/decisions/{decision_id}",
    response_model=CreatorDecisionView,
)
async def get_creator_decision(
    task_id: str,
    decision_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorDecisionView:
    return await runtime.query.get_decision(principal, task_id, decision_id)


@creator_router.post(
    "/tasks/{task_id}/decisions/{decision_id}/responses",
    response_model=CreatorTaskMutationResponse,
)
async def respond_to_creator_decision(
    task_id: str,
    decision_id: str,
    body: CreatorDecisionResponseRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorTaskMutationResponse:
    return await runtime.workspace.submit_decision(
        principal,
        task_id=task_id,
        decision_id=decision_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@creator_router.get(
    "/tasks/{task_id}/artifacts",
    response_model=tuple[CreatorArtifactSummary, ...],
)
async def list_creator_artifacts(
    task_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorArtifactSummary, ...]:
    return await runtime.query.list_artifacts(principal, task_id)


@creator_router.get(
    "/tasks/{task_id}/artifacts/{artifact_id}",
    response_model=CreatorArtifactDetail,
)
async def get_creator_artifact(
    task_id: str,
    artifact_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorArtifactDetail:
    return await runtime.query.get_artifact(principal, task_id, artifact_id)


@creator_router.get(
    "/tasks/{task_id}/drafts",
    response_model=tuple[CreatorDraftSummary, ...],
)
async def list_creator_task_drafts(
    task_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorDraftSummary, ...]:
    return await runtime.query.list_task_drafts(principal, task_id)


@creator_router.post(
    "/tasks/{task_id}/drafts",
    response_model=CreatorDraftView,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_draft(
    task_id: str,
    body: CreatorDraftCreateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorDraftView:
    return await runtime.workspace.create_draft(
        principal,
        task_id=task_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@creator_router.get(
    "/tasks/{task_id}/publication-handoffs",
    response_model=tuple[CreatorPublicationHandoffView, ...],
)
async def list_creator_publication_handoffs(
    task_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorPublicationHandoffView, ...]:
    return await runtime.workspace.list_publication_handoffs(
        principal,
        task_id=task_id,
    )


@creator_router.post(
    "/tasks/{task_id}/publication-handoffs",
    response_model=CreatorPublicationHandoffView,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_publication_handoff(
    task_id: str,
    body: CreatorPublicationHandoffRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorPublicationHandoffView:
    return await runtime.workspace.create_publication_handoff(
        principal,
        task_id=task_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@creator_router.get(
    "/drafts/{draft_id}",
    response_model=CreatorDraftView,
)
async def get_creator_draft(
    draft_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorDraftView:
    return await runtime.query.get_draft(principal, draft_id)


@creator_router.get(
    "/drafts/{draft_id}/versions",
    response_model=tuple[CreatorDraftVersionView, ...],
)
async def list_creator_draft_versions(
    draft_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorDraftVersionView, ...]:
    return await runtime.query.list_draft_versions(principal, draft_id)


@creator_router.post(
    "/drafts/{draft_id}/versions",
    response_model=CreatorDraftView,
)
async def update_creator_draft(
    draft_id: str,
    body: CreatorDraftUpdateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorDraftView:
    return await runtime.workspace.update_draft(
        principal,
        draft_id=draft_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@creator_router.get(
    "/drafts/{draft_id}/suggestions",
    response_model=tuple[CreatorSuggestion, ...],
)
async def list_creator_suggestions(
    draft_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorSuggestion, ...]:
    return await _require_creator_studio(runtime).list_suggestions(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        draft_id=draft_id,
    )


@creator_router.post(
    "/drafts/{draft_id}/suggestions",
    response_model=CreatorSuggestion,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_suggestion(
    draft_id: str,
    body: CreatorSuggestionCreateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorSuggestion:
    return await _require_creator_studio(runtime).create_suggestion(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        draft_id=draft_id,
        expected_version=body.expected_version,
        kind=body.kind,
        instruction=body.instruction,
        original_text=body.original_text,
        prefix_context=body.prefix_context,
        suffix_context=body.suffix_context,
        idempotency_key=idempotency_key,
    )


@creator_router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=CreatorSuggestionApplyResponse,
)
async def accept_creator_suggestion(
    suggestion_id: str,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorSuggestionApplyResponse:
    suggestion, result = await _require_creator_studio(runtime).accept_suggestion(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        actor_id=principal.actor_id,
        suggestion_id=suggestion_id,
        idempotency_key=idempotency_key,
    )
    draft = await runtime.query.get_draft(principal, result.draft.id)
    return CreatorSuggestionApplyResponse(suggestion=suggestion, draft=draft)


@creator_router.post(
    "/suggestions/{suggestion_id}/reject",
    response_model=CreatorSuggestion,
)
async def reject_creator_suggestion(
    suggestion_id: str,
    body: CreatorSuggestionRejectRequest,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorSuggestion:
    return await _require_creator_studio(runtime).reject_suggestion(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        suggestion_id=suggestion_id,
        reason=body.reason,
    )


@creator_router.get(
    "/drafts/{draft_id}/branches",
    response_model=tuple[CreatorBranch, ...],
)
async def list_creator_branches(
    draft_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorBranch, ...]:
    return await _require_creator_studio(runtime).list_branches(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        draft_id=draft_id,
    )


@creator_router.post(
    "/drafts/{draft_id}/branches",
    response_model=CreatorBranchCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_branch(
    draft_id: str,
    body: CreatorBranchCreateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorBranchCreateResponse:
    branch, result = await _require_creator_studio(runtime).create_branch(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        actor_id=principal.actor_id,
        draft_id=draft_id,
        source_version=body.source_version,
        name=body.name,
        idempotency_key=idempotency_key,
    )
    draft = await runtime.query.get_draft(principal, result.draft.id)
    return CreatorBranchCreateResponse(branch=branch, draft=draft)


@creator_router.get(
    "/drafts/{draft_id}/channel-variants",
    response_model=tuple[CreatorChannelVariant, ...],
)
async def list_creator_channel_variants(
    draft_id: str,
    principal: _Principal,
    runtime: _Runtime,
) -> tuple[CreatorChannelVariant, ...]:
    return await _require_creator_studio(runtime).list_channel_variants(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        draft_id=draft_id,
    )


@creator_router.post(
    "/drafts/{draft_id}/channel-variants",
    response_model=CreatorChannelVariant,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_channel_variant(
    draft_id: str,
    body: CreatorChannelVariantCreateRequest,
    idempotency_key: _IdempotencyKey,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorChannelVariant:
    return await _require_creator_studio(runtime).create_channel_variant(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        draft_id=draft_id,
        expected_version=body.expected_version,
        channel=body.channel,
        instruction=body.instruction,
        idempotency_key=idempotency_key,
    )


@creator_router.post(
    "/feedback",
    response_model=CreatorFeedback,
    status_code=status.HTTP_201_CREATED,
)
async def create_creator_rating(
    body: CreatorRatingRequest,
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorFeedback:
    return await _require_creator_studio(runtime).record_rating(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        task_id=body.task_id,
        draft_id=body.draft_id,
        score=body.score,
        reason=body.reason,
    )


@creator_router.get(
    "/feedback/summary",
    response_model=CreatorFeedbackSummary,
)
async def get_creator_feedback_summary(
    principal: _Principal,
    runtime: _Runtime,
) -> CreatorFeedbackSummary:
    return await _require_creator_studio(runtime).feedback_summary(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
    )


@creator_router.get("/tasks/{task_id}/events")
async def stream_creator_events(
    task_id: str,
    request: Request,
    principal: _Principal,
    runtime: _Runtime,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID", max_length=256),
    ] = None,
) -> EventSourceResponse:
    await runtime.query.get_task_snapshot(principal, task_id)
    if last_event_id:
        await runtime.query.list_events(
            principal,
            task_id,
            after_event_id=last_event_id,
            limit=1,
        )
    return EventSourceResponse(
        _creator_event_stream(
            request=request,
            runtime=runtime,
            principal=principal,
            task_id=task_id,
            last_event_id=last_event_id,
        ),
        ping=None,
        send_timeout=runtime.sse_send_timeout_seconds,
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _creator_event_stream(
    *,
    request: Request,
    runtime: CreatorApiRuntime,
    principal: CreatorApiPrincipal,
    task_id: str,
    last_event_id: str | None,
) -> AsyncIterator[JSONServerSentEvent]:
    cursor = last_event_id
    snapshot = await runtime.query.get_task_snapshot(principal, task_id)
    yield JSONServerSentEvent(
        event="task.snapshot",
        data=snapshot.model_dump(mode="json"),
    )
    last_heartbeat = time.monotonic()
    terminal = {
        CreatorTaskStatus.COMPLETED,
        CreatorTaskStatus.FAILED,
        CreatorTaskStatus.CANCELLED,
    }
    while True:
        if await request.is_disconnected():
            return
        events = await runtime.query.list_events(
            principal,
            task_id,
            after_event_id=cursor,
        )
        for event in events:
            cursor = event.event_id
            yield JSONServerSentEvent(
                event=event.type,
                id=event.event_id,
                data=event.model_dump(mode="json"),
            )
        if len(events) >= 250:
            continue
        snapshot = await runtime.query.get_task_snapshot(principal, task_id)
        if snapshot.status in terminal:
            yield JSONServerSentEvent(
                event="stream.closed",
                data={
                    "task_id": task_id,
                    "status": snapshot.status.value,
                    "last_event_id": cursor,
                },
            )
            return
        now = time.monotonic()
        if now - last_heartbeat >= runtime.sse_heartbeat_seconds:
            yield JSONServerSentEvent(
                event="heartbeat",
                data={
                    "task_id": task_id,
                    "status": snapshot.status.value,
                    "last_event_id": cursor,
                },
            )
            last_heartbeat = now
        await asyncio.sleep(runtime.sse_poll_seconds)


def install_creator_api_handlers(app: FastAPI) -> None:
    @app.exception_handler(CreatorHarnessError)
    async def creator_harness_error_handler(
        request: Request,
        exc: CreatorHarnessError,
    ) -> JSONResponse:
        return _error_response(request, exc, _creator_error_status(exc))

    @app.exception_handler(CreatorDraftError)
    async def creator_draft_error_handler(
        request: Request,
        exc: CreatorDraftError,
    ) -> JSONResponse:
        return _error_response(request, exc, _draft_error_status(exc))

    @app.exception_handler(CreatorPublicationError)
    async def creator_publication_error_handler(
        request: Request,
        exc: CreatorPublicationError,
    ) -> JSONResponse:
        status_code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, CreatorPublicationNotReadyError):
            status_code = status.HTTP_409_CONFLICT
        if isinstance(exc, CreatorPublicationLockedError):
            status_code = status.HTTP_409_CONFLICT
        return _error_response(request, exc, status_code)

    @app.exception_handler(CreatorStudioError)
    async def creator_studio_error_handler(
        request: Request,
        exc: CreatorStudioError,
    ) -> JSONResponse:
        status_code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, CreatorStudioNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, CreatorStudioScopeError):
            status_code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, CreatorStudioConflictError):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, CreatorStudioModelError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return _error_response(request, exc, status_code)

    @app.exception_handler(CreatorApiError)
    async def creator_api_error_handler(
        request: Request,
        exc: CreatorApiError,
    ) -> JSONResponse:
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.retryable
            else status.HTTP_400_BAD_REQUEST
        )
        if exc.code.endswith("_NOT_FOUND"):
            status_code = status.HTTP_404_NOT_FOUND
        if exc.code == "EVENT_CURSOR_INVALID":
            status_code = status.HTTP_409_CONFLICT
        return _error_response(request, exc, status_code)

    @app.exception_handler(RequestValidationError)
    async def creator_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        if not request.url.path.startswith("/api/v1/creator"):
            return await request_validation_exception_handler(request, exc)
        details = {
            "violations": [
                {
                    "location": [str(value) for value in item.get("loc", ())],
                    "message": str(item.get("msg") or "Invalid value"),
                    "type": str(item.get("type") or "validation_error"),
                }
                for item in exc.errors()[:20]
            ]
        }
        logger.warning(
            "Creator request validation failed path=%s errors=%s body=%s",
            request.url.path,
            exc.errors(),
            exc.body,
        )
        api_error = CreatorApiError(
            "Creator API request validation failed",
            details=details,
        )
        api_error.code = "VALIDATION_ERROR"
        return _error_response(
            request,
            api_error,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(HTTPException)
    async def creator_http_error_handler(
        request: Request,
        exc: HTTPException,
    ):
        if not request.url.path.startswith("/api/v1/creator"):
            return await http_exception_handler(request, exc)
        code = (
            "AUTHENTICATION_REQUIRED"
            if exc.status_code == status.HTTP_401_UNAUTHORIZED
            else (
                "AUTHORIZATION_DENIED"
                if exc.status_code == status.HTTP_403_FORBIDDEN
                else "HTTP_ERROR"
            )
        )
        api_error = CreatorApiError(str(exc.detail))
        api_error.code = code
        return _error_response(request, api_error, exc.status_code)


def _creator_error_status(exc: CreatorHarnessError) -> int:
    if isinstance(exc, (CreatorTaskNotFoundError, CreatorDecisionNotFoundError)):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, CreatorScopeViolationError):
        return status.HTTP_404_NOT_FOUND
    if exc.retryable:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_409_CONFLICT


def _draft_error_status(exc: CreatorDraftError) -> int:
    if isinstance(
        exc,
        (
            CreatorDraftNotFoundError,
            CreatorDraftTaskNotFoundError,
            CreatorDraftScopeError,
        ),
    ):
        return status.HTTP_404_NOT_FOUND
    if exc.retryable:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_409_CONFLICT


def _error_response(
    request: Request,
    exc,
    status_code: int,
) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", None) or str(uuid.uuid4())
    envelope = CreatorApiErrorEnvelope(
        error=CreatorApiErrorBody(
            code=str(getattr(exc, "code", "CREATOR_API_ERROR")),
            message=str(exc),
            retryable=bool(getattr(exc, "retryable", False)),
            trace_id=trace_id,
            details=dict(getattr(exc, "details", {}) or {}),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Trace-ID": trace_id},
    )
