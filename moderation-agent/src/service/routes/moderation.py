from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from moderation.repositories import (
    PolicyConflictError,
    TaskNotFoundError,
    TaskStateConflictError,
)
from moderation.schemas import (
    HumanReviewResult,
    HumanReviewSubmit,
    ModerationActionLogRead,
    ModerationCallbackDeliveryView,
    ModerationPolicyCreate,
    ModerationPolicyRead,
    ModerationStatistics,
    ModerationTaskAccepted,
    ModerationTaskCreate,
    ModerationTaskDetail,
    ModerationTaskStatus,
    ModerationTaskSummary,
    RiskType,
)
from moderation.services.runtime import ModerationServiceContainer

router = APIRouter(prefix="/moderation", tags=["moderation"])


def services(request: Request) -> ModerationServiceContainer:
    container = getattr(request.app.state, "moderation_services", None)
    if container is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Moderation services are not initialized",
        )
    return container


@router.post(
    "/tasks",
    response_model=ModerationTaskAccepted,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": ModerationTaskAccepted,
            "description": "Task accepted for asynchronous moderation.",
        }
    },
)
async def create_moderation_task(
    input: ModerationTaskCreate,
    request: Request,
) -> ModerationTaskAccepted | JSONResponse:
    from core import settings

    request_trace_id = str(getattr(request.state, "trace_id", ""))
    effective_input = (
        input
        if input.trace_id
        else input.model_copy(update={"trace_id": request_trace_id})
    )
    accepted = await services(request).workflow.create_task(effective_input)
    if (
        settings.MODERATION_ASYNC_ENABLED
        and accepted.task.status
        in {ModerationTaskStatus.PENDING, ModerationTaskStatus.RUNNING}
    ):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=accepted.model_dump(mode="json"),
        )
    return accepted


@router.get("/tasks", response_model=list[ModerationTaskSummary])
async def list_moderation_tasks(
    request: Request,
    task_status: ModerationTaskStatus | None = Query(default=None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ModerationTaskSummary]:
    return await services(request).workflow.list_tasks(
        status=task_status,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=ModerationTaskDetail)
async def get_moderation_task(task_id: UUID, request: Request) -> ModerationTaskDetail:
    try:
        return await services(request).workflow.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/logs", response_model=list[ModerationActionLogRead])
async def list_moderation_task_logs(
    task_id: UUID, request: Request
) -> list[ModerationActionLogRead]:
    try:
        return await services(request).workflow.list_logs(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/review", response_model=HumanReviewResult)
async def submit_human_review(
    task_id: UUID,
    input: HumanReviewSubmit,
    request: Request,
) -> HumanReviewResult:
    try:
        container = services(request)
        task, case_created = await container.workflow.submit_review(task_id, input)
        return HumanReviewResult(task=task, case_created=case_created)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TaskStateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/callbacks",
    response_model=list[ModerationCallbackDeliveryView],
)
async def list_moderation_callbacks(
    request: Request,
    callback_status: str | None = Query(default=None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ModerationCallbackDeliveryView]:
    allowed = {None, "PENDING", "DELIVERING", "RETRYING", "DELIVERED", "DEAD"}
    normalized = callback_status.upper() if callback_status else None
    if normalized not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported callback status",
        )
    return await services(request).callback_dispatcher.list_deliveries(
        status=normalized,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/policies",
    response_model=ModerationPolicyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_moderation_policy(
    input: ModerationPolicyCreate,
    request: Request,
) -> ModerationPolicyRead:
    try:
        return await services(request).policies.create(input)
    except PolicyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/policies", response_model=list[ModerationPolicyRead])
async def list_moderation_policies(
    request: Request,
    platform: str | None = None,
    risk_type: RiskType | None = None,
    enabled_only: bool = False,
) -> list[ModerationPolicyRead]:
    return await services(request).policies.list(
        platform=platform,
        risk_type=risk_type,
        enabled_only=enabled_only,
    )


@router.get("/statistics", response_model=ModerationStatistics)
async def get_moderation_statistics(request: Request) -> ModerationStatistics:
    return await services(request).statistics.get()
