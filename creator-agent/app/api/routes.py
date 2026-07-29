from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.creator.api.composition import CreatorApiRuntime


router = APIRouter()


@router.get("/actuator/health")
def health() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/actuator/health/live")
def liveness() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/actuator/health/ready")
async def readiness(request: Request) -> JSONResponse:
    runtime = getattr(request.app.state, "creator_api", None)
    if isinstance(runtime, CreatorApiRuntime):
        try:
            await runtime.database.ping()
            ready = True
        except Exception:
            ready = False
    else:
        ready = False
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "UP" if ready else "DOWN",
            "checks": {
                "creator_database": "UP" if ready else "DOWN",
            },
        },
    )
