"""GreenBook Community Assistant API — FastAPI application.

Provides:
  POST /api/v1/assistant/conversations
  POST /api/v1/assistant/conversations/{id}/messages
  GET  /api/v1/assistant/runs/{runId}
  GET  /api/v1/assistant/runs/{runId}/events
  POST /api/v1/assistant/approvals/{id}/approve
  POST /api/v1/assistant/approvals/{id}/reject
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from greenbook_java_client.client import JavaClient
from greenbook_creator_client.client import CreatorClient
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_security.auth_context import AuthContextResolver
from openai import AsyncOpenAI

from .api.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    java_base = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080")
    creator_base = os.getenv("GREENBOOK_CREATOR_BASE_URL", "http://127.0.0.1:8093")
    jwks_url = os.getenv("GREENBOOK_JAVA_JWKS_URL", "http://127.0.0.1:8080/.well-known/jwks.json")
    issuer = os.getenv("GREENBOOK_JAVA_ISSUER", "zhiguang")
    audience = os.getenv("GREENBOOK_JAVA_AUDIENCE", "community-assistant-agent")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")

    app.state.java = JavaClient(base_url=java_base)
    app.state.creator = CreatorClient(base_url=creator_base)
    app.state.mcp = GreenBookMCPServer(java=app.state.java, creator=app.state.creator)
    app.state.auth_resolver = AuthContextResolver(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
    )
    app.state.llm = AsyncOpenAI(api_key=deepseek_key, base_url=deepseek_base)
    app.state.model = llm_model

    app.state.conversation_store: dict[str, dict] = {}
    app.state.run_store: dict[str, dict] = {}
    app.state.approval_store: dict[str, dict] = {}

    logger.info("Assistant API ready java=%s model=%s", java_base, llm_model)

    try:
        yield
    finally:
        await app.state.java.close()
        await app.state.creator.close()
        await app.state.llm.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="GreenBook Community Operations Assistant API",
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "2.0.0"}

    return app
