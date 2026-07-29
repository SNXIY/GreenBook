import logging
import re
import uuid
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core._api import LangChainBetaWarning

from agents.moderation.graph import moderation_agent
from core import settings
from memory import initialize_database, initialize_store
from moderation.services.runtime import initialize_moderation_services
from service.routes.moderation import router as moderation_router

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)
_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def custom_generate_unique_id(route: APIRoute) -> str:
    """Generate stable operation IDs for the moderation API."""
    return route.name


def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Provide the configured AUTH_SECRET.", auto_error=False)),
    ],
) -> None:
    if not settings.AUTH_SECRET:
        return
    if not http_auth or http_auth.credentials != settings.AUTH_SECRET.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize the moderation graph and its infrastructure."""
    try:
        async with initialize_database() as saver, initialize_store() as store:
            if hasattr(saver, "setup"):
                await saver.setup()
            if hasattr(store, "setup"):
                await store.setup()

            if not settings.AUTH_SECRET:
                logger.warning(
                    "AUTH_SECRET is not configured; moderation APIs are unauthenticated."
                )

            moderation_agent.checkpointer = saver
            moderation_agent.store = store
            logger.info("Moderation graph initialized")

            async with initialize_moderation_services(moderation_agent) as services:
                app.state.moderation_services = services
                worker_loop = None
                if (
                    settings.MODERATION_ASYNC_ENABLED
                    and settings.MODERATION_EMBEDDED_WORKER_ENABLED
                ):
                    from moderation.services.worker import ModerationWorkerLoop

                    worker_loop = ModerationWorkerLoop(
                        services.workflow,
                    )
                    worker_loop.start()
                    services.callback_dispatcher.start()
                    logger.info("Embedded moderation worker started")
                try:
                    yield
                finally:
                    if worker_loop is not None:
                        await worker_loop.stop()
                        await services.callback_dispatcher.stop()
                    del app.state.moderation_services
    except Exception as exc:
        logger.error("Error during moderation service initialization: %s", exc)
        raise


app = FastAPI(
    title="Content Moderation Platform",
    description="Community content ingestion, automated moderation, and human review APIs.",
    lifespan=lifespan,
    generate_unique_id_function=custom_generate_unique_id,
)


@app.middleware("http")
async def trace_context(request, call_next):
    requested = request.headers.get("x-trace-id", "")
    request.state.trace_id = (
        requested if _TRACE_ID.fullmatch(requested) else str(uuid.uuid4())
    )
    response = await call_next(request)
    response.headers.setdefault("X-Trace-ID", request.state.trace_id)
    return response


api_router = APIRouter(dependencies=[Depends(verify_bearer)])
api_router.include_router(moderation_router)
app.include_router(api_router)


@app.get("/health", include_in_schema=False)
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
