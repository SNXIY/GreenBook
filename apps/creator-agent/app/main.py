from __future__ import annotations

import re
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.creator.api.composition import open_creator_api_runtime
from app.creator.api.routes import creator_router, install_creator_api_handlers
from app.creator.observability import (
    configure_creator_telemetry,
    instrument_creator_fastapi,
    shutdown_creator_telemetry,
)


_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "Creator API/Worker runtime build_commit=%s instance_id=%s "
        "worker_instance_id=%s revision_budget=%s provider=%s model=%s "
        "queue_namespace=%s database_identifier=%s",
        settings.creator_build_commit,
        settings.creator_instance_id,
        settings.creator_api_worker_id,
        settings.creator_max_writer_revisions,
        settings.ai_provider,
        settings.deepseek_model,
        settings.creator_queue_namespace,
        settings.creator_database_url.split("@")[-1],
    )
    configure_creator_telemetry(settings)
    async with open_creator_api_runtime(settings) as creator_api:
        app.state.creator_api = creator_api
        try:
            yield
        finally:
            del app.state.creator_api
            shutdown_creator_telemetry()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MindFlow Creator Intelligence Agent",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def no_cache_frontend_assets(request, call_next):
        requested_trace_id = request.headers.get("x-trace-id", "")
        request.state.trace_id = (
            requested_trace_id
            if _TRACE_ID.fullmatch(requested_trace_id)
            else str(uuid.uuid4())
        )
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("X-Trace-ID", request.state.trace_id)
        return response

    app.include_router(router)
    app.include_router(creator_router)
    install_creator_api_handlers(app)
    instrument_creator_fastapi(app)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
