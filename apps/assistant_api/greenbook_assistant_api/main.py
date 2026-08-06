"""GreenBook Community Assistant API — FastAPI application.

Provides:
  POST /api/v1/assistant/conversations
  POST /api/v1/assistant/conversations/{id}/messages
  GET  /api/v1/assistant/runs/{runId}
  GET  /api/v1/assistant/runs/{runId}/events
  POST /api/v1/assistant/approvals/{id}/approve
  POST /api/v1/assistant/approvals/{id}/reject

Start:
  .venv-v2\\Scripts\\python -m uvicorn apps.assistant_api.greenbook_assistant_api.main:create_app --factory --host 127.0.0.1 --port 8094

All config is read from the project-root .env file via python-dotenv.
No manual env vars needed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env from project root before anything else
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ENV_FILE)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from greenbook_contracts.identity import AuthContext  # noqa: E402
from greenbook_creator_client.client import CreatorClient  # noqa: E402
from greenbook_java_client.client import JavaClient  # noqa: E402
from greenbook_mcp_server.server import GreenBookMCPServer  # noqa: E402
from greenbook_security.auth_context import AuthContextResolver  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from .api.routes import router  # noqa: E402

logger = logging.getLogger(__name__)


class _DevAuthMiddleware(BaseHTTPMiddleware):
    """Injects AuthContext from Authorization header in dev/test mode.

    Production JWKS validation runs in AuthContextResolver as a FastAPI dependency.
    This middleware runs BEFORE the dependency, setting request.state.auth_context
    so that route helpers that call _get_auth(request) can find it.
    """

    async def dispatch(self, request: Request, call_next):
        # If already set by AuthContextResolver (production path), keep it
        if not hasattr(request.state, "auth_context") or request.state.auth_context is None:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                # Dev mode: token is "<user_id>:<tenant_id>" or a real JWT
                if ":" in token:
                    parts = token.split(":", 1)
                    request.state.auth_context = AuthContext(
                        user_id=parts[0],
                        tenant_id=parts[1],
                        raw_access_token=token,
                    )
                else:
                    # Real JWT — AuthContextResolver will set it later via DI
                    pass
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    java_base = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080")
    creator_base = os.getenv("GREENBOOK_CREATOR_BASE_URL", "http://127.0.0.1:8093")
    jwks_url = os.getenv("GREENBOOK_JAVA_JWKS_URL", "http://127.0.0.1:8080/.well-known/jwks.json")
    issuer = os.getenv("GREENBOOK_JAVA_ISSUER", "zhiguang")
    audience = os.getenv("GREENBOOK_JAVA_AUDIENCE", "community-assistant-agent")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")

    if not deepseek_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. "
            "Set either environment variable."
        )

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

    app.state.conversation_store = {}  # type: ignore[misc]
    app.state.run_store = {}  # type: ignore[misc]
    app.state.approval_store = {}  # type: ignore[misc]

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

    app.add_middleware(_DevAuthMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        java_ok = False
        creator_ok = False
        try:
            j: JavaClient = request.app.state.java
            java_ok = j.http.base_url != ""
        except Exception:
            pass
        try:
            c = request.app.state.creator
            creator_ok = c.http.base_url != ""
        except Exception:
            pass
        return {
            "status": "UP",
            "version": "2.0.0",
            "javaConfigured": java_ok,
            "creatorConfigured": creator_ok,
        }

    return app
