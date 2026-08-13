"""FastAPI entry point for the GreenBook Agent API.

The Agent API validates Java-issued access tokens and owns runtime composition
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
from greenbook_agent_core.agent import AgentLoop
from greenbook_agent_core.command import CommandInterpreter
from greenbook_agent_core.compatibility.history import RunExecutionAdapter
from greenbook_agent_core.conversation import (
    ConversationService,
    MemoryUserPreferenceProvider,
)
from greenbook_agent_core.db.connection import dispose_engine, session_ctx
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.execution.operation_tracking import ExternalOperationTracker
from greenbook_agent_core.execution.retry_manager import RetryManager
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.retry_worker import RetryBackgroundWorker
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.goal import GoalCompiler, GoalDecomposer
from greenbook_agent_core.human import PostgresApprovalRequestStore
from greenbook_agent_core.memory import MemoryRetriever, PostgresMemoryRepository
from greenbook_agent_core.memory.manager import MemoryManager
from greenbook_agent_core.observability.metrics import MemoryMetricsCollector
from greenbook_agent_core.planning.dynamic import DynamicPlanner
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.manager import TaskManager
from greenbook_creator_client.client import CreatorClient
from greenbook_java_client.client import JavaClient
from greenbook_mcp_server import tool_registry as mcp_tool_registry
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_security.auth_context import AuthContextResolver, _extract_bearer
from greenbook_security.jwt import JwtValidationError, validate_access_token
from greenbook_security.policy import SecurityPolicy
from openai import AsyncOpenAI
from starlette.middleware.base import BaseHTTPMiddleware

from .api.routes import router
from .api.runtime_routes import router as runtime_router
from .services.approval_runtime_service import ApprovalRuntimeService
from .services.conversation_control_service import ConversationControlService
from .services.conversation_runtime_adapter import ConversationRuntimeAdapter
from .services.execution_authorizer import ExecutionAuthorizer
from .services.execution_completion_publisher import ExecutionCompletionPublisher
from .services.execution_credential_broker import ExecutionCredentialBroker
from .services.queue_execution_handler import RuntimeExecutionQueueHandler
from .services.runtime_agent_service import RuntimeAgentService
from .services.task_provider import TaskProvider

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_IDENTITY_AUDIENCE = "greenbook-agent-runtime"


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
                        credential_broker = getattr(
                            request.app.state,
                            "execution_credential_broker",
                            None,
                        )
                        if credential_broker is not None:
                            credential_broker.register(request.state.auth_context)
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


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    java_base = _env_first(
        "GREENBOOK_JAVA_BASE_URL",
        default="http://127.0.0.1:8080",
    )
    creator_base = _env_first(
        "GREENBOOK_CREATOR_BASE_URL",
        default="http://127.0.0.1:8092",
    )
    jwks_url = _env_first(
        "GREENBOOK_AGENT_IDENTITY_JWKS_URL",
        default="http://127.0.0.1:8080/.well-known/jwks.json",
    )
    issuer = _env_first(
        "GREENBOOK_AGENT_IDENTITY_ISSUER",
        default="http://127.0.0.1:8080",
    )
    audience = _env_first(
        "GREENBOOK_AGENT_IDENTITY_AUDIENCE",
        default=DEFAULT_AGENT_IDENTITY_AUDIENCE,
    )
    deepseek_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    execution_mode = _env_first(
        "GREENBOOK_AGENT_EXECUTION_MODE",
        default="runtime",
    ).strip().lower()
    runtime_flag = os.getenv("GREENBOOK_AGENT_RUNTIME_ENABLED")
    if runtime_flag is not None:
        runtime_enabled = runtime_flag.strip().lower() in {
            "1", "true", "yes", "on", "runtime",
        }
        execution_mode = "runtime" if runtime_enabled else "legacy"
    else:
        runtime_enabled = execution_mode in {
            "1", "true", "yes", "on", "runtime",
        }

    if not deepseek_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. Set either environment variable."
        )

    app.state.java = JavaClient.from_env(base_url=java_base)
    app.state.creator = CreatorClient.from_env(base_url=creator_base)
    runtime_container = RuntimeContainer.from_env(
        tool_registry=mcp_tool_registry,
        security_policy=SecurityPolicy(),
    )
    app.state.mcp = GreenBookMCPServer(
        java=app.state.java,
        creator=app.state.creator,
        capability_registry=runtime_container.capability_registry,
    )
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

    runtime_persistence = runtime_container.persistence
    metrics_collector = MemoryMetricsCollector()
    dispatch_mode = _env_first(
        "GREENBOOK_AGENT_EXECUTION_DISPATCH",
        default=("queue" if runtime_persistence.storage == "postgres" else "direct"),
    ).strip().lower()
    if dispatch_mode not in {"direct", "queue"}:
        raise RuntimeError(
            "GREENBOOK_AGENT_EXECUTION_DISPATCH must be 'direct' or 'queue'"
        )
    execution_repository = runtime_persistence.execution_repository
    execution_event_store = runtime_persistence.execution_event_store
    execution_state_manager = runtime_container.execution_state_manager
    execution_runtime_manager = RuntimeManager(
        state_manager=execution_state_manager,
        checkpoint_store=runtime_persistence.checkpoint_store,
    )
    execution_retry_scheduler = RetryScheduler(
        task_store=runtime_persistence.retry_task_store,
    )
    durable_memory_repository = None
    if runtime_persistence.storage == "postgres":
        durable_memory_repository = PostgresMemoryRepository(session_ctx)
        await durable_memory_repository.ensure_storage()
    memory_manager = MemoryManager(durable_repository=durable_memory_repository)
    memory_retriever = MemoryRetriever(
        durable_memory_repository or memory_manager.store,
    )
    preference_provider = MemoryUserPreferenceProvider(memory_manager)
    task_provider = TaskProvider()
    await task_provider.ensure_storage()
    logger.info("Task persistence ready")
    task_manager = TaskManager(task_provider.canonical_repository())
    runtime_agent_service = RuntimeAgentService(
        container=runtime_container,
        repository=execution_repository,
        event_store=execution_event_store,
        checkpoint_store=runtime_persistence.checkpoint_store,
        operation_tracker=ExternalOperationTracker(
            store=runtime_persistence.external_operation_store,
        ),
        execution_queue=runtime_persistence.execution_queue,
        artifact_store=runtime_persistence.artifact_store,
        dispatch_mode=dispatch_mode,
        metrics_collector=metrics_collector,
        retry_scheduler=execution_retry_scheduler,
        memory_manager=memory_manager,
        task_manager=task_manager,
    )
    execution_retry_manager = RetryManager(
        state_manager=execution_state_manager,
        runtime_manager=execution_runtime_manager,
        metrics_collector=metrics_collector,
    )
    conversation_service = ConversationService()
    await conversation_service.ensure_storage()
    logger.info("Conversation context persistence ready")
    control_service = ConversationControlService(
        runtime_manager=execution_runtime_manager,
        retry_manager=execution_retry_manager,
        execution_queue=runtime_persistence.execution_queue,
    )
    approval_runtime_service = ApprovalRuntimeService(
        store=PostgresApprovalRequestStore(),
        runtime_manager=execution_runtime_manager,
        state_manager=execution_state_manager,
        execution_queue=runtime_persistence.execution_queue,
        conversation_service=conversation_service,
        direct_resume=lambda approval_id, decision: (
            runtime_agent_service.resume_human_interaction(
                approval_id,
                "",
                decision=decision,
            )
        ),
    )
    conversation_runtime_adapter = ConversationRuntimeAdapter(
        command_runtime=CommandInterpreter(
            llm=app.state.llm,
            model=llm_model,
            capability_registry=runtime_container.capability_registry,
        ),
        goal_decomposer=GoalDecomposer(
            llm=app.state.llm,
            model=llm_model,
            capability_registry=runtime_container.capability_registry,
        ),
        agent_loop=AgentLoop(
            llm=app.state.llm,
            model=llm_model,
            dynamic_planner=DynamicPlanner(
                llm=app.state.llm,
                model=llm_model,
            ),
            task_manager=task_manager,
            goal_compiler=GoalCompiler(
                registry=runtime_container.capability_registry,
            ),
        ),
        task_provider=task_provider,
        task_manager=task_manager,
        runtime_service=runtime_agent_service,
        execution_repository=execution_repository,
        container=runtime_container,
        control_service=control_service,
        approval_service=approval_runtime_service,
        preference_provider=preference_provider,
        conversation_service=conversation_service,
        memory_retriever=memory_retriever,
    )
    execution_authorizer = ExecutionAuthorizer(task_provider=task_provider)

    app.state.execution_repository = execution_repository
    app.state.execution_event_store = execution_event_store
    app.state.execution_checkpoint_store = runtime_persistence.checkpoint_store
    app.state.external_operation_store = runtime_persistence.external_operation_store
    app.state.retry_task_store = runtime_persistence.retry_task_store
    app.state.execution_queue = runtime_persistence.execution_queue
    app.state.artifact_store = runtime_persistence.artifact_store
    app.state.execution_result_projection_store = (
        runtime_persistence.result_projection_store
    )
    app.state.execution_lease_manager = runtime_persistence.lease_manager
    app.state.runtime_persistence = runtime_persistence
    app.state.runtime_container = runtime_container
    app.state.security_policy = runtime_container.security_policy
    app.state.execution_state_manager = execution_state_manager
    app.state.execution_runtime_manager = execution_runtime_manager
    app.state.execution_retry_manager = execution_retry_manager
    app.state.execution_retry_scheduler = execution_retry_scheduler
    app.state.runtime_agent_service = runtime_agent_service
    app.state.task_provider = task_provider
    app.state.task_manager = task_manager
    app.state.conversation_service = conversation_service
    app.state.preference_provider = preference_provider
    app.state.approval_runtime_service = approval_runtime_service
    app.state.conversation_control_service = control_service
    app.state.execution_authorizer = execution_authorizer
    app.state.conversation_runtime_adapter = conversation_runtime_adapter
    app.state.run_execution_adapter = RunExecutionAdapter()
    app.state.execution_mode = execution_mode
    app.state.runtime_enabled = runtime_enabled
    app.state.execution_dispatch_mode = dispatch_mode
    app.state.metrics_collector = metrics_collector

    execution_queue_worker: ExecutionQueueWorker | None = None
    retry_background_worker: RetryBackgroundWorker | None = None
    background_tasks: list[asyncio.Task[Any]] = []
    in_process_worker = (
        dispatch_mode == "queue"
        and _env_bool("GREENBOOK_AGENT_IN_PROCESS_WORKER", default=False)
    )
    app.state.execution_credential_broker = None
    app.state.in_process_worker = in_process_worker
    completion_publisher = ExecutionCompletionPublisher(
        conversation_service=conversation_service,
        run_store=app.state.run_store,
        artifact_store=runtime_persistence.artifact_store,
        result_projection_store=runtime_persistence.result_projection_store,
        task_provider=task_provider,
        approval_service=approval_runtime_service,
    )
    app.state.execution_completion_publisher = completion_publisher
    reconciled_projections = 0
    for queued_message in runtime_persistence.execution_queue.list()[-100:]:
        persisted_execution = execution_repository.find_by_id(
            queued_message.execution_id
        )
        if persisted_execution is None:
            continue
        try:
            if await completion_publisher.reconcile(
                queued_message,
                persisted_execution,
            ):
                reconciled_projections += 1
        except Exception:
            logger.warning(
                "Queued completion projection reconciliation failed execution_id=%s",
                queued_message.execution_id,
                exc_info=True,
            )
    if reconciled_projections:
        logger.info(
            "Restored queued Agent completion projections count=%s",
            reconciled_projections,
        )
    if in_process_worker:
        worker_id = _env_first(
            "GREENBOOK_AGENT_RETRY_WORKER_ID",
            default="agent-api-worker",
        )
        poll_interval = float(
            _env_first("GREENBOOK_AGENT_RETRY_POLL_INTERVAL_SECONDS", default="1")
        )
        batch_size = int(_env_first("GREENBOOK_AGENT_RETRY_BATCH_SIZE", default="20"))
        lease_seconds = int(
            _env_first("GREENBOOK_AGENT_RETRY_LEASE_SECONDS", default="60")
        )
        credential_broker = ExecutionCredentialBroker()
        app.state.execution_credential_broker = credential_broker
        queue_handler = RuntimeExecutionQueueHandler(
            service=runtime_agent_service,
            mcp=app.state.mcp,
            credential_resolver=credential_broker.resolve,
            completion_publisher=completion_publisher,
            llm=app.state.llm,
            model=llm_model,
        )
        execution_queue_worker = ExecutionQueueWorker(
            queue=runtime_persistence.execution_queue,
            execution_handler=queue_handler,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            poll_interval_seconds=poll_interval,
            batch_size=batch_size,
            lease_manager=runtime_persistence.lease_manager,
        )
        retry_background_worker = RetryBackgroundWorker(
            scheduler=execution_retry_scheduler,
            retry_manager=execution_retry_manager,
            poll_interval_seconds=poll_interval,
            batch_size=batch_size,
            worker_id=worker_id,
            execution_queue=runtime_persistence.execution_queue,
        )
        background_tasks = [
            asyncio.create_task(
                retry_background_worker.run(),
                name="agent-api-retry-consumer",
            ),
            asyncio.create_task(
                execution_queue_worker.run(),
                name="agent-api-execution-consumer",
            ),
        ]
        logger.info(
            "API-managed Runtime consumers started worker_id=%s",
            worker_id,
        )

    logger.info(
        "GreenBook Agent API ready java=%s creator=%s issuer=%s audience=%s model=%s storage=%s dispatch=%s",
        java_base,
        creator_base,
        issuer,
        audience,
        llm_model,
        runtime_persistence.storage,
        dispatch_mode,
    )
    logger.info("Runtime API started dispatch=%s storage=%s", dispatch_mode, runtime_persistence.storage)

    try:
        yield
    finally:
        if retry_background_worker is not None:
            retry_background_worker.request_shutdown()
        if execution_queue_worker is not None:
            execution_queue_worker.request_shutdown()
        for task in background_tasks:
            if not task.done():
                task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        runtime_container.close()
        await app.state.java.close()
        await app.state.creator.close()
        await app.state.llm.close()
        await dispose_engine()


def create_app(*, auth_validator: Callable[[str], Any] | None = None) -> FastAPI:
    app = FastAPI(
        title="GreenBook Agent API",
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
    app.include_router(runtime_router, prefix="/api/v1")

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
            "executionDispatch": getattr(
                request.app.state,
                "execution_dispatch_mode",
                "unknown",
            ),
            "executionStorage": getattr(
                getattr(request.app.state, "runtime_persistence", None),
                "storage",
                "unknown",
            ),
            "executionConsumer": (
                "in_process"
                if getattr(request.app.state, "in_process_worker", False)
                else (
                    "external"
                    if getattr(
                        request.app.state,
                        "execution_dispatch_mode",
                        "direct",
                    ) == "queue"
                    else "disabled"
                )
            ),
        }

    return app
