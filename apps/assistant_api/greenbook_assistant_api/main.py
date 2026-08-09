"""FastAPI entry point for the GreenBook Assistant service.

The Assistant validates Java-issued access tokens, owns in-memory Assistant
state for the local runtime, and dispatches business operations through the
in-process MCP adapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from inspect import isawaitable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from greenbook_creator_client.client import CreatorClient
from greenbook_java_client.client import JavaClient
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_security.auth_context import AuthContextResolver, _extract_bearer
from greenbook_security.jwt import JwtValidationError, validate_access_token
from openai import AsyncOpenAI
from starlette.middleware.base import BaseHTTPMiddleware

from .api.routes import router
from .services.runtime_agent_service import RuntimeAgentService

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)


class _JwtAuthMiddleware(BaseHTTPMiddleware):
    """Validate the Java access token before route handlers run.

    Tests may inject an explicit validator through ``app.state.auth_validator``.
    Production never interprets user-controlled strings as an identity.
    """

    async def dispatch(self, request: Request, call_next):
        if getattr(request.state, "auth_context", None) is None:
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                logger.info(
                    "auth_failure code=missing_authorization_header path=%s",
                    request.url.path,
                )
            else:
                token = _extract_bearer(auth_header)
                if not token:
                    logger.info(
                        "auth_failure code=malformed_bearer_token path=%s",
                        request.url.path,
                    )
                else:
                    try:
                        test_validator: Callable[[str], Any] | None = getattr(
                            request.app.state, "auth_validator", None
                        )
                        if test_validator is not None:
                            auth_context = test_validator(token)
                            request.state.auth_context = (
                                await auth_context if isawaitable(auth_context) else auth_context
                            )
                        else:
                            resolver: AuthContextResolver = request.app.state.auth_resolver
                            request.state.auth_context = await validate_access_token(
                                token,
                                jwks_url=resolver._jwks_url,
                                issuer=resolver._issuer,
                                audience=resolver._audience,
                            )
                        logger.info(
                            "auth_validated user_id=%s path=%s",
                            request.state.auth_context.user_id,
                            request.url.path,
                        )
                    except JwtValidationError as exc:
                        logger.warning(
                            "auth_failure code=%s path=%s",
                            exc.code,
                            request.url.path,
                        )
                    except Exception:
                        logger.exception(
                            "auth_failure code=jwks_fetch_failed path=%s",
                            request.url.path,
                        )
        return await call_next(request)


def _env_first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    java_base = _env_first(
        "ASSISTANT_JAVA_BASE_URL",
        "GREENBOOK_JAVA_BASE_URL",
        default="http://127.0.0.1:8080",
    )
    creator_base = _env_first(
        "ASSISTANT_CREATOR_BASE_URL",
        "GREENBOOK_CREATOR_BASE_URL",
        default="http://127.0.0.1:8092",
    )
    jwks_url = _env_first(
        "ASSISTANT_IDENTITY_JWKS_URL",
        "GREENBOOK_JAVA_JWKS_URL",
        default="http://127.0.0.1:8080/.well-known/jwks.json",
    )
    issuer = _env_first(
        "ASSISTANT_IDENTITY_ISSUER",
        "GREENBOOK_JAVA_ISSUER",
        "JWT_ISSUER",
        default="http://127.0.0.1:8080",
    )
    audience = _env_first(
        "ASSISTANT_IDENTITY_AUDIENCE",
        "GREENBOOK_JAVA_AUDIENCE",
        default="community-assistant-agent",
    )
    deepseek_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")

    if not deepseek_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. Set either environment variable."
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

    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}

    execution_repository = ExecutionRepository()
    execution_event_store = ExecutionEventStore()
    execution_state_manager = ExecutionStateManager(
        repository=execution_repository,
        event_store=execution_event_store,
    )
    execution_runtime_manager = RuntimeManager(
        state_manager=execution_state_manager,
    )
    runtime_agent_service = RuntimeAgentService(
        repository=execution_repository,
        event_store=execution_event_store,
    )

    app.state.execution_repository = execution_repository
    app.state.execution_event_store = execution_event_store
    app.state.execution_state_manager = execution_state_manager
    app.state.execution_runtime_manager = execution_runtime_manager
    app.state.runtime_agent_service = runtime_agent_service

    logger.info(
        "Assistant API ready java=%s creator=%s issuer=%s audience=%s model=%s",
        java_base,
        creator_base,
        issuer,
        audience,
        llm_model,
    )

    try:
        yield
    finally:
        await app.state.java.close()
        await app.state.creator.close()
        await app.state.llm.close()


def create_app(*, auth_validator: Callable[[str], Any] | None = None) -> FastAPI:
    app = FastAPI(
        title="GreenBook Community Operations Assistant API",
        version="2.0.0",
        lifespan=lifespan,
    )

    if auth_validator is not None:
        app.state.auth_validator = auth_validator
    app.add_middleware(_JwtAuthMiddleware)
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
        java_base = ""
        creator_base = ""
        with suppress(Exception):
            java_base = str(request.app.state.java.http.base_url).rstrip("/")
        with suppress(Exception):
            creator_base = str(request.app.state.creator.http.base_url).rstrip("/")

        async def probe(base_url: str, path: str) -> bool:
            if not base_url:
                return False
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{base_url}{path}")
                return 200 <= response.status_code < 300
            except httpx.HTTPError:
                return False

        java_ok, creator_ok = await asyncio.gather(
            probe(java_base, "/actuator/health"),
            probe(creator_base, "/actuator/health"),
        )
        return {
            "status": "UP" if java_ok and creator_ok else "DEGRADED",
            "version": "2.0.0",
            "javaConfigured": bool(java_base),
            "creatorConfigured": bool(creator_base),
            "javaReachable": java_ok,
            "creatorReachable": creator_ok,
        }

    return app
