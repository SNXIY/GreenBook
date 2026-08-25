"""FastAPI entry point for the GreenBook Agent API.

The Agent API validates Java-issued access tokens and owns runtime composition
state for the local runtime, and dispatches business operations through the
in-process MCP adapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
from inspect import isawaitable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from greenbook_agent_core.activity import UserActivityPublisher
from greenbook_agent_core.command import CommandInterpreter
from greenbook_agent_core.command.models import Command
from greenbook_agent_core.compatibility.history import RunExecutionAdapter
from greenbook_agent_core.conversation import (
    ConversationService,
    MemoryUserPreferenceProvider,
)
from greenbook_agent_core.db.connection import dispose_engine, session_ctx
from greenbook_agent_core.execution.action_observation import (
    ActionObservation,
    ActionObservationWriter,
)
from greenbook_agent_core.execution.completion_publisher import ExecutionCompletionPublisher
from greenbook_agent_core.execution.execution_queue import recover_unqueued_executions
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    ExternalOperationTracker,
)
from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler
from greenbook_agent_core.execution.retry_manager import RetryManager
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.retry_worker import RetryBackgroundWorker
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.execution.topology import validate_single_consumer
from greenbook_agent_core.human import PostgresApprovalRequestStore
from greenbook_agent_core.human.approval_runtime_service import ApprovalRuntimeService
from greenbook_agent_core.memory import (
    MemoryRetriever,
    PostgresMemoryRepository,
    PreferenceMemoryService,
)
from greenbook_agent_core.memory.manager import MemoryManager
from greenbook_agent_core.observability.metrics import MemoryMetricsCollector
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.manager import TaskManager
from greenbook_agent_core.task.provider import TaskProvider, TaskScope
from greenbook_contracts.events import EVENT_ACTION_COMPLETED, EVENT_PARTIAL_RESULT
from greenbook_java_client.client import JavaClient
from greenbook_mcp_server import tool_registry as mcp_tool_registry
from greenbook_mcp_server.client import GreenBookMCPClient
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_security.auth_context import AuthContextResolver, _extract_bearer
from greenbook_security.jwt import JwtValidationError, validate_access_token
from greenbook_security.policy import SecurityPolicy
from openai import AsyncOpenAI
from starlette.middleware.base import BaseHTTPMiddleware

from .api.routes import router
from .api.runtime_routes import router as runtime_router
from .api.debug_routes import router as debug_router
from .runner import (
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_SEMANTIC_ACTION,
    EVENT_WAITING_APPROVAL,
    RUN_ACCEPTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL_SUCCESS,
    RUN_RUNNING,
    RUN_TERMINAL,
    RUN_WAITING,
    RUN_WAITING_APPROVAL,
    performance_projection,
    project_progressive_event,
)


def _reconciled_artifacts_from_operation(
    operation: Any,
    *,
    execution: Any | None = None,
) -> list[dict[str, Any]]:
    """Project only authoritative reconciliation refs into a result envelope.

    A lost MCP acknowledgement can leave the normal RuntimeResult without the
    artifact that the Java write already created.  Reconciliation has the
    authoritative resource ref in its evidence; carrying that ref through the
    existing completion projection lets the Task/Objective reducer converge
    without issuing another write.
    """

    evidence = getattr(operation, "evidence", None)
    refs = list(getattr(evidence, "resource_refs", None) or [])
    resource_type = str(getattr(operation, "resource_type", "") or "").upper()
    resource_id = str(getattr(operation, "resource_id", "") or "")
    if resource_id and not any(
        str(item.get("resource_id") or "") == resource_id
        for item in refs
        if isinstance(item, dict)
    ):
        refs.append({"kind": resource_type, "resource_id": resource_id})
    if not refs:
        return []

    expected = getattr(operation, "expected_postcondition", None) or {}
    arguments = expected.get("arguments") if isinstance(expected, dict) else {}
    if not isinstance(arguments, dict):
        arguments = {}
    semantic_action = str(getattr(operation, "semantic_action", "") or "").upper()
    status_by_action = {
        "CREATE_DRAFT": "DRAFT",
        "GENERATE_CONTENT": "DRAFT",
        "UPDATE_DRAFT": "DRAFT",
        "CREATE_SCHEDULE": "SCHEDULED",
        "SCHEDULE_PUBLISH": "SCHEDULED",
        "UPDATE_SCHEDULE": "SCHEDULED",
        "CANCEL_SCHEDULE": "CANCELLED",
        "PUBLISH_NOW": "PUBLISHED",
        "DELETE_POST": "DELETED",
    }
    fallback_step_id = ""
    for step in list(getattr(execution, "steps", None) or []):
        step_status = str(getattr(getattr(step, "status", None), "value", getattr(step, "status", "")))
        if step_status not in {"COMPLETED", "FAILED", "SKIPPED"}:
            fallback_step_id = str(getattr(step, "step_id", "") or "")
            break
        if not fallback_step_id:
            fallback_step_id = str(getattr(step, "step_id", "") or "")

    artifacts: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        rid = str(ref.get("resource_id") or "")
        if not rid:
            continue
        kind = str(ref.get("kind") or resource_type or "").upper()
        if not kind:
            continue
        artifact_status = status_by_action.get(semantic_action)
        item: dict[str, Any] = {
            "type": kind,
            "artifact_type": kind,
            "resource_type": kind,
            "resource_id": rid,
            "step_id": fallback_step_id,
            "status": artifact_status,
        }
        title = arguments.get("title") or arguments.get("name")
        if title:
            item["title"] = str(title)
        run_at = arguments.get("run_at") or arguments.get("scheduled_at")
        if run_at:
            item["run_at"] = str(run_at)
        timezone = arguments.get("timezone")
        if timezone:
            item["timezone"] = str(timezone)
        artifacts.append(item)
    return artifacts
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from .services.conversation_control_service import ConversationControlService
from .services.conversation_runtime_adapter import ConversationRuntimeAdapter
from .services.execution_authorizer import ExecutionAuthorizer
from .services.execution_credential_broker import ExecutionCredentialBroker
from .services.turn_coordinator import TurnCoordinator

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_IDENTITY_AUDIENCE = "greenbook-agent-runtime"


def _recover_unqueued_executions(runtime_persistence: Any) -> int:
    """Compatibility wrapper for the shared queue recovery utility."""

    return recover_unqueued_executions(
        runtime_persistence.execution_repository,
        runtime_persistence.execution_queue,
    )


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


def _emit_run_event(app: Any, run_id: str, event_type: str, payload: dict) -> None:
    store = getattr(app.state, "agent_run_event_store", None)
    if store is not None and run_id:
        store.append(run_id, event_type, payload)


def _project_and_emit_run_event(
    app: Any,
    run_id: str,
    event_type: str,
    event_payload: dict,
) -> None:
    """Project one Runtime fact, then retain the legacy Run event for debug.

    The durable Activity projection is the ordinary-user business feed.  The
    historical Run event stream below remains a compatibility/debug source
    only and must not decide whether a user-visible action completed.
    """
    # The legacy Run event feed remains available for compatibility/debugging,
    # but ordinary user progress is always projected through the durable,
    # public-safe Activity store.  Project the *original* Runtime fact before
    # the old feed rewrites it into its historical envelope.
    activity_publisher = getattr(app.state, "user_activity_publisher", None)
    run_store = getattr(app.state, "agent_run_store", None)
    if activity_publisher is not None and run_store is not None and run_id:
        run = run_store.get(run_id)
        if run is not None:
            try:
                activity_publisher.publish_runtime_event(
                    event_type,
                    event_payload,
                    conversation_id=str(run.conversation_id),
                    user_id=str(run.user_id),
                    tenant_id=str(run.tenant_id),
                    run_id=run_id,
                )
            except Exception:
                # Activity delivery must not turn a completed business action
                # into a retried side effect.  The Runtime fact remains
                # durable and can be reconciled separately.
                logger.exception(
                    "User activity projection failed run_id=%s event=%s",
                    run_id,
                    event_type,
                )

    run_event_store = getattr(app.state, "agent_run_event_store", None)
    if run_event_store is None:
        return
    if event_type == EVENT_ACTION_COMPLETED:
        action = str(event_payload.get("semantic_action") or "")
        ok = bool(event_payload.get("ok"))
        ownership = {
            key: event_payload.get(key)
            for key in ("goal_id", "task_id", "semantic_action")
            if event_payload.get(key)
        }
        run_event_store.append(
            run_id,
            EVENT_SEMANTIC_ACTION,
            ownership | {"phase": "SUCCEEDED" if ok else "FAILED"},
        )
        if ok:
            partial = project_progressive_event(
                action,
                event_payload.get("result") or {},
            )
            if partial is not None:
                run_event_store.append(
                    run_id,
                    EVENT_PARTIAL_RESULT,
                    {**ownership, **partial},
                )
        return
    run_event_store.append(run_id, event_type, event_payload)


def _observe_turn_outcome(run: Any, final_status: str, result: Any) -> None:
    """Record turn metrics + trace when a Run converges to a terminal state."""
    try:
        from greenbook_agent_core.observability.bus import observability

        ob = observability()
        outcome = str(final_status or "").upper()
        ob.turn_total().inc(outcome=outcome)
        trace_id = str(getattr(result, "trace_id", "") or getattr(run, "trace_id", "") or run_id_of(run))
        ob.record_trace(
            "turn_" + ("completed" if outcome == "COMPLETED" else "failed"),
            trace_id=trace_id,
            conversation_id=str(getattr(run, "conversation_id", "") or ""),
            task_id=str(getattr(result, "task_id", "") or ""),
            execution_id=str(getattr(result, "execution_id", "") or ""),
            status=outcome,
            error_code=str(getattr(result, "error_code", "") or ""),
        )
    except Exception:  # noqa: BLE001 - observability must never break convergence
        pass


def run_id_of(run: Any) -> str:
    return str(getattr(run, "run_id", "") or "")


async def _reconcile_agent_run_status(
    *,
    app: Any,
    run_id: str,
    result: Any | None = None,
    queue_execution_id: str | None = None,
) -> None:
    """Converge a Run only after its queued work and observations settle.

    A Runtime result with status ``QUEUED`` is an intermediate acceptance, not
    a completed Run.  The queue message carries the durable Run ownership;
    ActionObservation carries the continuation boundary.  Both are checked
    before publishing a terminal Run event, so an out-of-order Worker or
    continuation cannot close a Run that still has independent work.
    """

    run_store = getattr(app.state, "agent_run_store", None)
    persistence = getattr(app.state, "runtime_persistence", None)
    if run_store is None or persistence is None or not run_id:
        return

    if queue_execution_id:
        queue = getattr(persistence, "execution_queue", None)
        if queue is None or queue.get_by_execution_id(queue_execution_id) is None:
            # Direct completion projection is already handled by the ordinary
            # result path; only a durable queue message can close this hook.
            return

    run = run_store.get(run_id)
    if run is None:
        return

    if run.status in RUN_TERMINAL:
        return

    queue = getattr(persistence, "execution_queue", None)
    repository = getattr(persistence, "execution_repository", None)
    messages = list(queue.list()) if queue is not None else []
    owned_messages = [
        message
        for message in messages
        if str(message.payload.get("run_id") or "") == run_id
    ]
    if queue_execution_id and not any(
        message.execution_id == queue_execution_id for message in owned_messages
    ):
        return
    if not owned_messages:
        # No queued Execution ever belonged to this Run: the runner handled
        # the outcome directly (e.g. an AgentLoop-internal failure that never
        # dispatched).  Converging here would overwrite the durable terminal
        # state with a guessed COMPLETED (observed: a STRUCTURED_OUTPUT_INVALID
        # Run was shown as COMPLETED with no error).  The durable state is the
        # runner's authority; leave it alone.
        return

    active = 0
    completed = 0
    failed = 0
    waiting = 0
    waiting_approval_execution_ids: list[str] = []
    for message in owned_messages:
        execution = (
            repository.find_by_id(message.execution_id)
            if repository is not None
            else None
        )
        if execution is None:
            # Unknown durable state is not safe to interpret as complete.
            active += 1
            continue
        execution_status = str(
            getattr(getattr(execution, "status", None), "value", "")
            or getattr(execution, "status", "")
        ).upper()
        if execution_status == "COMPLETED":
            completed += 1
        elif execution_status in {"FAILED", "CANCELLED"}:
            failed += 1
        elif execution_status in {
            "WAITING_APPROVAL",
            "WAITING_HUMAN",
            "PAUSED",
        }:
            waiting += 1
            if execution_status == "WAITING_APPROVAL":
                waiting_approval_execution_ids.append(message.execution_id)
        else:
            active += 1

    observation_store = getattr(persistence, "observation_store", None)
    pending_observations = []
    if observation_store is not None:
        pending_observations = [
            observation
            for observation in observation_store.list_pending()
            if str(observation.run_id or "") == run_id
        ]

    result_status = str(getattr(result, "status", "") or "").upper()
    # Run terminality is downstream of the canonical Task/Objective reducer.
    # A completed Execution (or a stale RuntimeResult) cannot close a Run while
    # its Task still has pending/running Objectives or work.
    task_id = str(
        getattr(result, "task_id", "")
        or (run.payload or {}).get("task_id")
        or ""
    )
    if not task_id:
        task_id = next(
            (
                str(message.payload.get("task_id") or "")
                for message in owned_messages
                if str(message.payload.get("task_id") or "")
            ),
            "",
        )
    waiting_state = str((run.payload or {}).get("waiting_state") or "").upper()
    task_provider = getattr(app.state, "task_provider", None)
    canonical_task_status = ""
    task_has_failed_sibling = False
    if task_id and task_provider is not None:
        try:
            from greenbook_agent_core.task.provider import TaskScope

            task = await task_provider.get_task(
                TaskScope(
                    user_id=str(
                        getattr(run, "user_id", "")
                        or (owned_messages[0].payload.get("user_id") if owned_messages else "")
                        or ""
                    ),
                    tenant_id=str(
                        getattr(run, "tenant_id", "")
                        or (owned_messages[0].payload.get("tenant_id") if owned_messages else "")
                        or ""
                    ),
                    conversation_id=str(
                        getattr(run, "conversation_id", "")
                        or (owned_messages[0].payload.get("conversation_id") if owned_messages else "")
                        or ""
                    ),
                ),
                task_id,
            )
        except Exception:
            task = None
        if task is not None:
            canonical_task_status = str(
                getattr(getattr(task, "status", None), "value", "")
                or getattr(task, "status", "")
            ).upper()
            # The queue counters above cover only executions still represented
            # by this Run's queue messages.  A resumed sibling continuation can
            # legitimately have one COMPLETED child here while another
            # Objective's earlier failure is already a terminal Task ref.  Keep
            # that history visible as PARTIAL_SUCCESS instead of collapsing the
            # successful current sibling to RUN_FAILED.
            task_has_failed_sibling = any(
                str(getattr(ref, "status", "") or "").upper()
                in {"FAILED", "ERROR", "CANCELLED"}
                for ref in (getattr(task, "execution_refs", ()) or ())
            )
        waiting_result = result_status in {
            "WAITING_APPROVAL",
            "WAITING_HUMAN",
            "ASK_USER",
        }
        if task_id and task_provider is not None:
            # A queue-owned Run is Task-bound even when a legacy projection
            # has no Objective rows.  Never infer a terminal Run from one
            # completed child while the canonical Task is unavailable or
            # still non-terminal.  In particular, a transient read gap here
            # must not close the Run before ActionLoop continuation submits
            # its next legitimate queue message; the queue stale-run guard
            # must remain strict.
            if task is None:
                if run.status != RUN_RUNNING:
                    run_store.mark_status(run_id, RUN_RUNNING, expected_version=run.version)
                return
            if (
                canonical_task_status not in {"COMPLETED", "FAILED", "CANCELLED"}
                and not waiting
                and not waiting_result
            ):
                if run.status != RUN_RUNNING:
                    run_store.mark_status(run_id, RUN_RUNNING, expected_version=run.version)
                return
    if active or pending_observations or result_status in {
        "RUNNING",
        "QUEUED",
        "SUBMITTED",
    }:
        if run.status != RUN_RUNNING:
            run_store.mark_status(
                run_id,
                RUN_RUNNING,
                expected_version=run.version,
            )
        return

    projection_payload: dict[str, Any] | None = None
    result_approval_id = str(getattr(result, "approval_id", "") or "")
    waiting_projection = waiting_state in {
        "WAITING_APPROVAL",
        "WAITING_HUMAN",
        "ASK_USER",
    } and not completed and not failed
    if waiting or waiting_projection or result_status in {
        "WAITING_APPROVAL",
        "WAITING_HUMAN",
        "ASK_USER",
    }:
        final_status = RUN_WAITING_APPROVAL if waiting_approval_execution_ids else RUN_WAITING
        event_type = EVENT_WAITING_APPROVAL
        if waiting_approval_execution_ids:
            execution_id = waiting_approval_execution_ids[0]
            projection_payload = dict(run.payload or {})
            projection_payload["execution_id"] = execution_id
            projection_payload["waiting_state"] = "WAITING_APPROVAL"
            approval_service = getattr(app.state, "approval_runtime_service", None)
            if approval_service is not None:
                pending = await approval_service.get_for_execution(execution_id)
                # The durable approval row is the identity boundary.  A
                # RuntimeResult may arrive before capture_result commits it
                # (or while an API poll races that commit); do not persist an
                # incomplete WAITING_APPROVAL Run in that window.  The normal
                # completion callback retries this projection after capture.
                if pending is None:
                    return
                projection_payload["approval_id"] = pending.approval_id
            elif result_approval_id:
                # Unit/in-process embedders without the durable service may
                # still provide the complete identity in the result envelope.
                projection_payload["approval_id"] = result_approval_id
            else:
                return
    elif canonical_task_status == "FAILED":
        # Objective aggregation is authoritative for a Task-bound Run.  A
        # single child result cannot turn mixed Objective success/failure into
        # all-success, even if its own Runtime envelope says COMPLETED.
        final_status = (
            RUN_PARTIAL_SUCCESS
            if completed and (failed or task_has_failed_sibling)
            else RUN_FAILED
        )
        event_type = EVENT_RUN_COMPLETED
    elif canonical_task_status == "CANCELLED":
        final_status = RUN_CANCELLED
        event_type = EVENT_RUN_FAILED
    elif canonical_task_status == "COMPLETED":
        final_status = RUN_PARTIAL_SUCCESS if failed else RUN_COMPLETED
        event_type = EVENT_RUN_COMPLETED
    elif failed and completed:
        final_status = RUN_PARTIAL_SUCCESS
        event_type = EVENT_RUN_COMPLETED
    elif (
        failed
        or result_status in {"FAILED", "CANCELLED", "MAX_ITERATIONS"}
        or str(getattr(result, "error_code", "") or "") in {
            "AGENT_MAX_ITERATIONS",
            "STRUCTURED_OUTPUT_INVALID",
        }
    ):
        final_status = RUN_FAILED
        event_type = EVENT_RUN_FAILED
    else:
        run_error_code = str(getattr(result, "error_code", "") or "")
        if run_error_code:
            # Invariant: a terminal COMPLETED must never carry a fatal error.
            # Surface the partial/failed truth instead of pretending success.
            final_status = RUN_PARTIAL_SUCCESS if (completed or not failed) else RUN_FAILED
            event_type = EVENT_RUN_COMPLETED
        else:
            final_status = RUN_COMPLETED
            event_type = EVENT_RUN_COMPLETED

    _observe_turn_outcome(run, final_status, result)
    # The initial runner result can be merely a durable write acceptance.
    # Refresh the same per-Run projection when the continuation truly reaches
    # terminal state, otherwise total_latency_ms understates user-visible work.
    projection_payload = dict(projection_payload or run.payload or {})
    performance = performance_projection(run, result or RuntimeResult())
    projection_payload["performance"] = performance
    changed = run_store.mark_status(
        run_id,
        final_status,
        error_code=getattr(result, "error_code", "") or "",
        error_message=(
            getattr(result, "error_message", "")
            or getattr(result, "error", "")
            or ""
        ),
        expected_version=run.version,
        payload=projection_payload,
    )
    if not changed:
        return
    _emit_run_event(
        app,
        run_id,
        event_type,
        {
            "run_id": run_id,
            "partial_success": final_status == RUN_PARTIAL_SUCCESS,
            "waiting_tasks": waiting,
            "failed_tasks": failed,
        },
    )


def observation_opens_continuation(observation: ActionObservation) -> bool:
    """Only committed business results open another ActionLoop turn."""

    return observation.status.upper() == "COMPLETED"


def _runtime_result_opens_continuation(result: Any) -> bool:
    """Only a terminal runtime result may open Task continuation.

    ``WAITING_EXTERNAL`` is often returned with ``success=True`` because the
    durable submission succeeded. That flag is not a completion signal and
    must not wake ActionLoop before evidence settles.
    """

    status = str(getattr(result, "status", "") or "").upper()
    return bool(getattr(result, "success", False)) and status in {
        "COMPLETED",
        "SUCCESS",
    }


async def _continuation_consumer_loop(
    *,
    executor: Any,
    observation_store: Any,
    mcp: Any,
    llm: Any,
    model: str,
    poll_interval_seconds: float,
    batch_size: int,
    max_concurrency: int = 4,
    app: Any = None,
) -> None:
    """Consume durable ActionObservations and resume ActionLoop.

    DB-backed and crash-safe: the observation is persisted by the Worker
    before this loop runs; a crash between claim and mark_done is recovered
    by the store's dispatch timeout on the next poll.
    """
    from greenbook_contracts.identity import AuthContext

    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    task_locks: dict[str, asyncio.Lock] = {}

    async def process_observation(observation: ActionObservation) -> None:
        # Continuations for one Task converge in order, while unrelated Tasks
        # use independent locks and can resume concurrently.  The durable
        # observation claim remains the cross-process duplicate guard.
        task_key = str(observation.task_id or observation.conversation_id or observation.observation_id)
        lock = task_locks.setdefault(task_key, asyncio.Lock())
        async with semaphore:
            async with lock:
                try:
                    # Corrupt/test-leftover observations (e.g. a non-UUID
                    # conversation_id) can never resume: mark them done once
                    # instead of failing the continuation poll forever.
                    if not _is_valid_uuid(observation.conversation_id):
                        observation_store.mark_done(observation.observation_id)
                        logger.warning(
                            "ActionObservation dropped invalid conversation_id "
                            "observation_id=%s execution_id=%s",
                            observation.observation_id,
                            observation.execution_id,
                        )
                        return
                    # FAILED/CANCELLED observations are durable terminal
                    # evidence. Worker retry/reconciliation owns recovery;
                    # resuming ActionLoop here would submit the same side
                    # effect again after a permanent input failure.
                    if not observation_opens_continuation(observation):
                        observation_store.mark_done(observation.observation_id)
                        return
                    credential_broker = getattr(
                        app.state,
                        "execution_credential_broker",
                        None,
                    )
                    continuation_result = await _continue_one_observation(
                        executor=executor,
                        observation=observation,
                        mcp=mcp,
                        llm=llm,
                        model=model,
                        auth_context=AuthContext,
                        credential_resolver=(
                            credential_broker.resolve_identity
                            if credential_broker is not None
                            else None
                        ),
                        activity_callback=(
                            lambda event_type, payload, run_id=observation.run_id: (
                                _project_and_emit_run_event(app, run_id, event_type, payload)
                            )
                        ),
                    )
                    observation_store.mark_done(observation.observation_id)
                    await _reconcile_agent_run_status(
                        app=app,
                        run_id=str(observation.run_id or ""),
                        result=continuation_result,
                    )
                except Exception:
                    logger.warning(
                        "ActionObservation continuation failed observation_id=%s execution_id=%s",
                        observation.observation_id,
                        observation.execution_id,
                        exc_info=True,
                    )
            if not lock.locked():
                task_locks.pop(task_key, None)

    while True:
        try:
            claimed = observation_store.claim_pending(
                min(max(1, batch_size), max(1, max_concurrency))
            )
            if claimed:
                await asyncio.gather(
                    *(process_observation(observation) for observation in claimed)
                )
        except Exception:
            logger.warning(
                "ActionObservation continuation poll failed",
                exc_info=True,
            )
        await asyncio.sleep(max(0.2, poll_interval_seconds))


async def _reconciliation_loop(worker: ReconciliationWorker) -> None:
    interval = max(
        10.0,
        float(_env_first("GREENBOOK_AGENT_RECONCILE_INTERVAL_SECONDS", default="60")),
    )
    while True:
        try:
            await worker.reconcile_due()
        except Exception:
            logger.exception("API reconciliation scan failed")
        await asyncio.sleep(interval)


async def _continue_one_observation(
    *,
    executor: Any,
    observation: ActionObservation,
    mcp: Any,
    llm: Any,
    model: str,
    auth_context: Any,
    credential_resolver: Any = None,
    activity_callback: Any = None,
) -> Any:
    session = observation.payload.get("session") or {}
    user_id = str(session.get("user_id") or "")
    tenant_id = str(session.get("tenant_id") or "")
    if not user_id or not tenant_id:
        logger.warning(
            "ActionObservation missing identity execution_id=%s; keeping observation for recovery",
            observation.execution_id,
        )
        raise RuntimeError("ActionObservation has no user/tenant identity")
    # Durable continuation identity: reconstruct the same user's validated
    # credential from the process-local broker (keyed by immutable identity),
    # never from a raw token in the observation/queue/Postgres.  If the broker
    # has no live credential (restart or expiry), the continuation still runs
    # with an empty token and Java-authenticated tools fail closed.
    resolved_auth = None
    if credential_resolver is not None:
        try:
            resolved_auth = credential_resolver(
                user_id,
                tenant_id,
                timezone=str(session.get("timezone") or "Asia/Shanghai"),
            )
        except Exception:
            logger.warning(
                "continuation credential resolve failed user_id=%s tenant_id=%s",
                user_id,
                tenant_id,
                exc_info=True,
            )
            resolved_auth = None
    auth = auth_context(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=list(
            session.get("roles")
            or getattr(resolved_auth, "roles", None)
            or []
        ),
        timezone=str(session.get("timezone") or "Asia/Shanghai"),
        raw_access_token=str(
            getattr(resolved_auth, "raw_access_token", "") or ""
        ),
    )
    resume = getattr(executor, "resume_task", None)
    if not callable(resume):
        raise RuntimeError("ActionLoop continuation executor is unavailable")
    return await resume(
        task_id=str(observation.task_id or ""),
        conversation_id=observation.conversation_id,
        user_id=user_id,
        tenant_id=tenant_id,
        trace_id=str(getattr(observation, "trace_id", "") or observation.execution_id or ""),
        session=session,
        timezone=str(session.get("timezone") or "Asia/Shanghai"),
        # The continuation belongs to the Run that submitted the observed
        # Execution: a new Run id would split the conversation's history
        # projection and break terminal convergence (design goal 0813 — the
        # Run must reach one terminal state, not two half-states).
        run_id=str(observation.run_id or ""),
        mcp=mcp,
        auth=auth,
        activity_callback=activity_callback,
    )


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value or ""))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    java_base = _env_first(
        "GREENBOOK_JAVA_BASE_URL",
        default="http://127.0.0.1:8080",
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
    if not deepseek_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY or OPENAI_API_KEY is required. Set either environment variable."
        )

    app.state.java = JavaClient.from_env(base_url=java_base)
    app.state.llm = AsyncOpenAI(api_key=deepseek_key, base_url=deepseek_base)
    app.state.model = llm_model
    runtime_container = RuntimeContainer.from_env(
        tool_registry=mcp_tool_registry,
        security_policy=SecurityPolicy(),
    )
    app.state.local_mcp = GreenBookMCPServer(
        java=app.state.java,
        capability_registry=runtime_container.capability_registry,
        llm=app.state.llm,
        model=llm_model,
    )
    app.state.mcp = GreenBookMCPClient(
        app.state.local_mcp,
        base_url=_env_first(
            "GREENBOOK_BUSINESS_MCP_BASE_URL",
            default="",
        ),
        transport_mode=_env_first("GREENBOOK_MCP_TRANSPORT", default="mcp"),
        runtime_token=os.getenv("GREENBOOK_MCP_RUNTIME_TOKEN", ""),
    )
    app.state.auth_resolver = AuthContextResolver(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
    )

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
    preference_memory_service = PreferenceMemoryService(memory_manager)
    memory_retriever = MemoryRetriever(
        durable_memory_repository or memory_manager.store,
    )
    app.state.memory_store = durable_memory_repository or memory_manager.store
    app.state.preference_memory_service = preference_memory_service
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
        # OperationLedger is the durable operation owner; the tracker is an
        # in-process audit/cache only.
        operation_tracker=ExternalOperationTracker(
            store=ExternalOperationStore(),
        ),
        execution_queue=runtime_persistence.execution_queue,
        artifact_store=runtime_persistence.artifact_store,
        dispatch_mode=dispatch_mode,
        metrics_collector=metrics_collector,
        retry_scheduler=execution_retry_scheduler,
        memory_manager=memory_manager,
        task_manager=task_manager,
        # Phase 4B.1: the Execution Runtime owns durable operation dedupe/claim.
        operation_ledger=OperationLedger(runtime_persistence.external_operation_store),
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
    command_runtime = CommandInterpreter(
        llm=app.state.llm,
        model=llm_model,
        capability_registry=runtime_container.capability_registry,
    )
    conversation_runtime_adapter = ConversationRuntimeAdapter(
        command_runtime=command_runtime,
        task_provider=task_provider,
        task_manager=task_manager,
        runtime_service=runtime_agent_service,
        execution_repository=execution_repository,
        external_operation_store=runtime_persistence.external_operation_store,
        observation_store=runtime_persistence.observation_store,
        container=runtime_container,
        control_service=control_service,
        approval_service=approval_runtime_service,
        preference_provider=preference_provider,
        conversation_service=conversation_service,
        memory_retriever=memory_retriever,
        max_concurrent_work_per_conversation=int(
            _env_first(
                "GREENBOOK_AGENT_MAX_CONCURRENT_WORK_PER_CONVERSATION",
                default="3",
            )
        ),
        max_concurrent_direct_tools=int(
            _env_first(
                "GREENBOOK_AGENT_MAX_CONCURRENT_DIRECT_TOOLS",
                default="6",
            )
        ),
    )
    execution_authorizer = ExecutionAuthorizer(task_provider=task_provider)

    # Phase 3A/3B: unified Turn entry.  Fast Path reads/writes and CLARIFY/CHAT
    # are handled by TurnCoordinator.  COMPLEX requests are driven by the
    # ActionLoop (Phase 3B).
    from greenbook_agent_core.context.builder import ContextBuilder
    from greenbook_agent_core.turn import ContextAssembler

    from .services.action_loop_executor import ActionLoopExecutor

    turn_context_assembler = ContextAssembler(
        ContextBuilder(
            conversation_source=conversation_service,
            task_provider=task_provider,
            task_manager=task_manager,
            execution_repository=execution_repository,
            external_operation_store=runtime_persistence.external_operation_store,
            artifact_store=runtime_persistence.artifact_store,
            observation_store=runtime_persistence.observation_store,
            memory_retriever=memory_retriever,
            preference_provider=preference_provider,
            task_scope_factory=TaskScope,
        )
    )
    action_loop_executor = ActionLoopExecutor(
        adapter=conversation_runtime_adapter,
        context_assembler=turn_context_assembler,
        task_manager=task_manager,
        llm=app.state.llm,
        model=llm_model,
        max_iterations=int(
            _env_first("GREENBOOK_AGENT_ACTION_LOOP_MAX_ITERATIONS", default="8")
        ),
        decision_event_store=execution_event_store,
    )
    logger.info("complex_runtime=action_loop")
    app.state.action_loop_executor = action_loop_executor
    turn_coordinator = TurnCoordinator(
        command_runtime=command_runtime,
        tool_registry=runtime_container.tool_registry,
        complex_path=conversation_runtime_adapter,
        action_loop_executor=action_loop_executor,
        task_manager=task_manager,
        context_assembler=turn_context_assembler,
    )
    app.state.turn_coordinator = turn_coordinator

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
    app.state.user_activity_store = runtime_persistence.user_activity_store
    app.state.user_activity_publisher = UserActivityPublisher(
        runtime_persistence.user_activity_store,
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
    app.state.execution_dispatch_mode = dispatch_mode
    app.state.metrics_collector = metrics_collector
    # Read-time Run convergence for the external-worker deployment: Executions
    # complete in the Worker process without an API-side event, and a terminal
    # Execution that never re-enters ActionLoop (e.g. a permanent tool
    # failure) would otherwise leave the durable Run stuck in RUNNING.
    # get_run/list_runs call this hook so failures are visible on the next
    # poll instead of spinning forever (design goal 0813).
    app.state.converge_run_status = partial(
        _reconcile_agent_run_status,
        app=app,
    )

    execution_queue_worker: ExecutionQueueWorker | None = None
    retry_background_worker: RetryBackgroundWorker | None = None
    background_tasks: list[asyncio.Task[Any]] = []
    in_process_worker = (
        dispatch_mode == "queue"
        and _env_bool("GREENBOOK_AGENT_IN_PROCESS_WORKER", default=False)
    )
    validate_single_consumer(
        dispatch_mode=dispatch_mode,
        in_process_worker=in_process_worker,
        health_file=_env_first(
            "GREENBOOK_AGENT_WORKER_HEALTH_FILE",
            default=".runtime/agent-worker-health.json",
        ),
        max_age_seconds=float(
            _env_first(
                "GREENBOOK_AGENT_WORKER_HEALTH_MAX_AGE_SECONDS",
                default="30",
            )
        ),
    )
    app.state.execution_credential_broker = None
    app.state.in_process_worker = in_process_worker
    async def _resume_after_execution(message, result) -> None:
        # Phase E2E: after a durable write reaches a terminal state, resume the
        # owning Task's ActionLoop so multi-step objectives continue (e.g. a
        # created draft is followed by scheduling).  Idempotent per terminal
        # completion; RESULT_UNKNOWN/in-flight tasks are not resumed.
        payload = message.payload or {}
        run_id = str(payload.get("run_id") or "")
        from greenbook_agent_core.context import SessionContext
        from greenbook_contracts.identity import AuthContext

        executor = getattr(app.state, "action_loop_executor", None)
        task_id = str(payload.get("task_id") or "")
        continuation_result = None
        # The completion callback and the durable ActionObservation consumer
        # are both valid delivery paths.  They must, however, share the same
        # predecessor claim before either path enters ActionLoop.  The
        # observation row is keyed by the verified predecessor execution, so a
        # later legitimate repeat has a different claim rather than being
        # blocked by ``(task_id, semantic_action)``.
        observation_store = getattr(app.state, "observation_store", None)
        observation_present = False
        claimed_observation = None
        if observation_store is not None and str(message.execution_id or ""):
            # Fail closed when the durable store cannot be inspected: allowing
            # the callback to continue would reintroduce the two-caller race.
            observation_present = True
            try:
                get_observation = getattr(observation_store, "get_by_execution", None)
                claim_continuation = getattr(
                    observation_store,
                    "claim_continuation",
                    None,
                )
                existing_observation = (
                    get_observation(message.execution_id)
                    if callable(get_observation)
                    else None
                )
                if existing_observation is None:
                    observation_present = False
                elif callable(claim_continuation):
                    claimed_observation = claim_continuation(message.execution_id)
                    if claimed_observation is None:
                        logger.info(
                            "action_loop continuation no-op: predecessor already claimed "
                            "task_id=%s execution_id=%s source=completion_callback",
                            task_id,
                            message.execution_id,
                        )
                    else:
                        logger.info(
                            "action_loop continuation claimed "
                            "task_id=%s execution_id=%s source=completion_callback",
                            task_id,
                            message.execution_id,
                        )
                else:
                    logger.warning(
                        "action_loop continuation claim unavailable "
                        "task_id=%s execution_id=%s",
                        task_id,
                        message.execution_id,
                    )
            except Exception:  # the completion path must remain projection-safe
                logger.warning(
                    "action_loop continuation claim failed "
                    "task_id=%s execution_id=%s",
                    task_id,
                    message.execution_id,
                    exc_info=True,
                )
        if (
            executor is not None
            and _runtime_result_opens_continuation(result)
            and task_id
            and (not observation_present or claimed_observation is not None)
        ):
            user_id = str(payload.get("user_id") or "")
            tenant_id = str(payload.get("tenant_id") or "")
            timezone = str(payload.get("timezone") or "Asia/Shanghai")
            # Queue payloads intentionally do not carry bearer tokens. Reuse
            # the process-local validated credential registered by auth
            # middleware so a post-write continuation can call Java tools.
            credential_broker = getattr(app.state, "execution_credential_broker", None)
            auth = (
                credential_broker.resolve_identity(
                    user_id, tenant_id, timezone=timezone
                )
                if credential_broker is not None
                else None
            )
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage(
                    "resume_auth",
                    {
                        "user_id": user_id,
                        "tenant_id": tenant_id,
                        "broker_present": credential_broker is not None,
                        "resolved": auth is not None,
                        "token_present": bool(getattr(auth, "raw_access_token", None)),
                    },
                )
            except Exception:  # diagnostics must never affect execution
                pass
            if auth is None:
                auth = AuthContext(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    roles=[str(role) for role in (payload.get("roles") or [])],
                    timezone=timezone,
                    raw_access_token=str(payload.get("access_token") or ""),
                )
            session = SessionContext.model_validate(payload.get("session") or {})
            try:
                # Resume BEFORE converging the Run so the reconcile sees the
                # next queued Execution and keeps the Run non-terminal until the
                # whole GoalTree completes (a single completed write must not
                # flip the Run to COMPLETED while a follow-up objective is still
                # in flight).
                continuation_result = await executor.resume_task(
                    task_id=task_id,
                    conversation_id=str(payload.get("conversation_id") or ""),
                    user_id=auth.user_id,
                    tenant_id=auth.tenant_id,
                    run_id=run_id or message.execution_id,
                    trace_id=str(payload.get("trace_id") or message.execution_id),
                    session=session,
                    timezone=auth.timezone,
                    mcp=app.state.mcp,
                    auth=auth,
                    activity_callback=lambda event_type, event_payload: _project_and_emit_run_event(
                        app, str(payload.get("run_id") or ""), event_type, dict(event_payload)
                    ),
                )
                if claimed_observation is not None:
                    try:
                        observation_store.mark_done(claimed_observation.observation_id)
                    except Exception:  # recovery can reclaim a still-dispatched row
                        logger.warning(
                            "action_loop continuation mark_done failed "
                            "task_id=%s execution_id=%s",
                            task_id,
                            message.execution_id,
                            exc_info=True,
                        )
            except Exception:  # noqa: BLE001 - a resume failure must not kill the worker
                logger.exception(
                    "action_loop resume failed task_id=%s execution_id=%s",
                    task_id,
                    message.execution_id,
                )
        await _reconcile_agent_run_status(
            app=app,
            run_id=run_id,
            # The predecessor's COMPLETED result is no longer authoritative
            # when resume_task submitted the next objective.  Converging with
            # it here would close the Run before the follow-up approval/write
            # settles and the next queue message would be rejected as stale.
            result=(
                continuation_result
                if continuation_result is not None
                else result
            ),
            queue_execution_id=message.execution_id,
        )

    completion_publisher = ExecutionCompletionPublisher(
        conversation_service=conversation_service,
        run_store=app.state.run_store,
        artifact_store=runtime_persistence.artifact_store,
        result_projection_store=runtime_persistence.result_projection_store,
        task_provider=task_provider,
        approval_service=approval_runtime_service,
        user_activity_publisher=app.state.user_activity_publisher,
        after_execution=_resume_after_execution,
    )
    app.state.execution_completion_publisher = completion_publisher
    # Recover the narrow submit window in which PlanExecution was durably
    # created but the queue row was not yet written.  The exact sanitized
    # dispatch envelope is stored in the existing first-step checkpoint, so
    # this is a queue reconciliation, not a second planner/outbox/runtime.
    try:
        recovered_unqueued = _recover_unqueued_executions(runtime_persistence)
    except Exception:
        logger.warning(
            "Unqueued execution recovery scan failed",
            exc_info=True,
        )
    if recovered_unqueued:
        logger.info(
            "Recovered unqueued durable executions count=%s",
            recovered_unqueued,
        )
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
        observation_writer = ActionObservationWriter(
            store=runtime_persistence.observation_store,
            artifact_store=runtime_persistence.artifact_store,
        )
        queue_handler = RuntimeExecutionQueueHandler(
            service=runtime_agent_service,
            mcp=app.state.mcp,
            credential_resolver=credential_broker.resolve,
            completion_publisher=completion_publisher,
            # Queue delivery is the canonical Worker boundary for writes.
            # Give it the same durable ledger used by Runtime submission so
            # resumed executions create/claim their external operation before
            # invoking Java.
            operation_ledger=OperationLedger(
                runtime_persistence.external_operation_store
            ),
            # The durable AgentRun store is initialized below the queue
            # wiring. Resolve it lazily so orphaned queue messages can be
            # closed after startup without coupling queue construction to
            # runner initialization order.
            run_store=lambda: getattr(app.state, "agent_run_store", None),
            llm=app.state.llm,
            model=llm_model,
            observation_writer=observation_writer,
            user_activity_publisher=app.state.user_activity_publisher,
        )
        execution_queue_worker = ExecutionQueueWorker(
            queue=runtime_persistence.execution_queue,
            execution_handler=queue_handler,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            poll_interval_seconds=poll_interval,
            batch_size=batch_size,
            max_concurrency=int(
                _env_first("GREENBOOK_AGENT_EXECUTION_WORKER_CONCURRENCY", default="4")
            ),
            lease_manager=runtime_persistence.lease_manager,
            resource_access_provider=runtime_persistence.execution_repository.list_all,
        )
        retry_background_worker = RetryBackgroundWorker(
            scheduler=execution_retry_scheduler,
            retry_manager=execution_retry_manager,
            poll_interval_seconds=poll_interval,
            batch_size=batch_size,
            worker_id=worker_id,
            execution_queue=runtime_persistence.execution_queue,
        )

        async def _project_reconciled_operation(operation, status) -> None:
            """Route authoritative RESULT_UNKNOWN recovery through completion."""
            if status.value not in {"SUCCEEDED", "FAILED"}:
                return
            evidence = getattr(operation, "evidence", None)
            execution_id = str(
                getattr(evidence, "execution_id", "") or operation.execution_id
            )
            execution = runtime_persistence.execution_repository.find_by_id(
                execution_id
            )
            if execution is None:
                return
            state_manager = app.state.execution_state_manager
            steps = state_manager.list_steps(execution_id)
            matching = [
                step for step in steps
                if operation.step_id
                and operation.step_id in {step.step_id, step.step_execution_id}
            ]
            if not matching:
                non_terminal = [
                    step for step in steps
                    if getattr(getattr(step, "status", None), "value", step.status)
                    not in {"COMPLETED", "FAILED", "SKIPPED"}
                ]
                matching = non_terminal if len(non_terminal) == 1 else []
            execution_status = str(
                getattr(getattr(execution, "status", None), "value", execution.status)
            )
            if len(matching) != 1 and execution_status not in {
                "COMPLETED", "FAILED", "CANCELLED",
            }:
                logger.warning(
                    "Cannot project reconciled operation execution_id=%s operation_id=%s",
                    execution_id,
                    operation.operation_id,
                )
                return
            if matching:
                step = matching[0]
                if status.value == "SUCCEEDED":
                    state_manager.reconcile_step_succeeded(
                        execution_id,
                        step.step_execution_id,
                        operation_id=operation.operation_id,
                    )
                else:
                    state_manager.reconcile_step_failed(
                        execution_id,
                        step.step_execution_id,
                        error_code="EXTERNAL_OPERATION_FAILED",
                        error_message="External operation status is FAILED.",
                        operation_id=operation.operation_id,
                    )
            execution = runtime_persistence.execution_repository.find_by_id(
                execution_id
            )
            message = runtime_persistence.execution_queue.get_by_execution_id(
                execution_id
            )
            if message is None or execution is None:
                return
            from greenbook_contracts.identity import AuthContext

            identity = message.payload.get("auth_context") or {}
            reconciled_result = RuntimeResult(
                success=status.value == "SUCCEEDED",
                status=("COMPLETED" if status.value == "SUCCEEDED" else "FAILED"),
                run_id=str(message.payload.get("run_id") or ""),
                task_id=str(message.payload.get("task_id") or execution.task_id or ""),
                execution_id=execution_id,
                trace_id=str(message.payload.get("trace_id") or message.trace_id),
                error_message=(
                    "External operation status is FAILED."
                    if status.value == "FAILED" else ""
                ),
                artifacts=_reconciled_artifacts_from_operation(
                    operation,
                    execution=execution,
                ),
            )
            await completion_publisher.reconcile(
                message,
                execution,
                result=reconciled_result,
            )
            await observation_writer(
                message,
                reconciled_result,
                AuthContext(
                    user_id=str(identity.get("user_id") or message.payload.get("user_id") or ""),
                    tenant_id=str(identity.get("tenant_id") or message.payload.get("tenant_id") or ""),
                    roles=[str(role) for role in (identity.get("roles") or [])],
                    timezone=str(identity.get("timezone") or message.payload.get("timezone") or "Asia/Shanghai"),
                    raw_access_token="",
                ),
                execution=execution,
            )
            await completion_publisher.after_execution(message, reconciled_result)

        from greenbook_agent_core.execution.reconciliation_adapters import (
            JavaReconciliationAdapter,
        )

        def _reconcile_token(operation) -> str:
            evidence = getattr(operation, "evidence", None)
            execution_id = str(
                getattr(evidence, "execution_id", "") or operation.execution_id
            )
            message = runtime_persistence.execution_queue.get_by_execution_id(
                execution_id
            )
            if message is None:
                return ""
            identity = message.payload.get("auth_context") or {}
            auth = credential_broker.resolve_identity(
                str(identity.get("user_id") or message.payload.get("user_id") or ""),
                str(identity.get("tenant_id") or message.payload.get("tenant_id") or ""),
                timezone=str(identity.get("timezone") or message.payload.get("timezone") or ""),
            )
            return str(getattr(auth, "raw_access_token", "") or "")

        reconciliation_worker = ReconciliationWorker(
            OperationLedger(runtime_persistence.external_operation_store),
            adapter=JavaReconciliationAdapter(
                app.state.java,
                token_provider=_reconcile_token,
            ),
            on_reconciled=_project_reconciled_operation,
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
            asyncio.create_task(
                _reconciliation_loop(reconciliation_worker),
                name="agent-api-reconciliation-consumer",
            ),
        ]
        logger.info(
            "API-managed Runtime consumers started worker_id=%s",
            worker_id,
        )

    observation_store = runtime_persistence.observation_store
    app.state.observation_store = observation_store
    if (
        dispatch_mode == "queue"
        and observation_store is not None
        and _env_bool("GREENBOOK_AGENT_CONTINUATION_CONSUMER", default=True)
    ):
        background_tasks.append(asyncio.create_task(
            _continuation_consumer_loop(
                executor=action_loop_executor,
                observation_store=observation_store,
                mcp=app.state.mcp,
                llm=app.state.llm,
                model=llm_model,
                poll_interval_seconds=float(
                    _env_first("GREENBOOK_AGENT_CONTINUATION_POLL_SECONDS", default="1")
                ),
                batch_size=int(
                    _env_first("GREENBOOK_AGENT_CONTINUATION_BATCH", default="4")
                ),
                max_concurrency=int(
                    _env_first(
                        "GREENBOOK_AGENT_CONTINUATION_CONCURRENCY",
                        default="4",
                    )
                ),
                app=app,
            ),
            name="agent-api-continuation-consumer",
        ))
        logger.info("ActionObservation continuation consumer started")

    # ── Immediate-accept Agent runner (design target 0813) ──────────────
    # The POST path persists a durable Run (ACCEPTED) and returns 202 without
    # waiting for the first-turn LLM reasoning; this in-process runner claims
    # ACCEPTED Runs (lease-protected, crash-recoverable) and executes the
    # canonical adapter path in the background. Real business activity is
    # pushed to /runs/{run_id}/events via the run event store.
    agent_runner = None
    agent_runner_task = None
    if runtime_persistence.bind is not None:
        from greenbook_agent_core.context import SessionContext

        from .api.routes import handle_run_result
        from .runner import (
            AgentRunEventStore,
            AgentRunner,
            AgentRunStore,
        )

        run_store = AgentRunStore(runtime_persistence.bind, create_tables=True)
        run_event_store = AgentRunEventStore()
        app.state.agent_run_store = run_store
        app.state.agent_run_event_store = run_event_store
        app.state.immediate_accept = True

        # A Task CAS and the existing AgentRun state live in separate storage
        # calls.  Reconcile the narrow crash window before starting the runner:
        # a confirmed Task with its old semantic-confirmation Run still in
        # WAITING_USER is converted to ACCEPTED with the same typed resume
        # marker.  This reuses AgentRun lease recovery; no new outbox/queue is
        # introduced for Semantic Confirmation.
        from greenbook_agent_core.task.semantic_confirmation import confirmation_identity

        for waiting_run in run_store.list_recent(limit=500):
            if waiting_run.status != RUN_WAITING:
                continue
            confirmation_partial = dict(
                (waiting_run.payload or {}).get("partial_results") or {}
            )
            confirmation = confirmation_partial.get("semantic_confirmation")
            if not isinstance(confirmation, dict):
                continue
            task_ids = [str(item) for item in (waiting_run.payload or {}).get("task_ids") or [] if item]
            task_id = str(confirmation.get("task_id") or (task_ids[0] if task_ids else ""))
            if not task_id:
                continue
            task = await task_manager.get_task(
                task_id,
                conversation_id=waiting_run.conversation_id,
                user_id=waiting_run.user_id,
                tenant_id=waiting_run.tenant_id,
            )
            if task is None:
                continue
            state = str(getattr(getattr(task, "confirmation_state", ""), "value", "") or "").upper()
            if state != "CONFIRMED" or int(getattr(task, "confirmed_version", 0) or 0) != int(
                confirmation.get("confirmation_version", 0) or 0
            ):
                continue
            marker = {
                "task_id": task.task_id,
                "confirmation_id": confirmation_identity(task),
                "confirmation_version": int(task.confirmation_version or 0),
                "task_version": int(task.version or 0),
            }
            payload = dict(waiting_run.payload or {})
            payload["semantic_confirmation_resume"] = marker
            payload["task_ids"] = list(dict.fromkeys(task_ids + [task.task_id]))
            run_store.mark_status(
                waiting_run.run_id,
                RUN_ACCEPTED,
                expected_version=waiting_run.version,
                payload=payload,
            )

        async def _runner_execute(run):
            from greenbook_contracts.identity import AuthContext

            payload = dict(run.payload or {})
            auth = AuthContext(
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                roles=[str(role) for role in (payload.get("roles") or [])],
                timezone=str(payload.get("timezone") or "Asia/Shanghai"),
                raw_access_token=str(payload.get("access_token") or ""),
            )
            session = SessionContext.model_validate(payload.get("session") or {})
            command_override = None
            if payload.get("command"):
                raw_command = payload["command"]
                if isinstance(raw_command, dict) and "type" in raw_command:
                    # Target clarification continuations carry the already
                    # interpreted semantic Command.  Re-enter the existing
                    # complex path with that Command; do not reinterpret the
                    # user's label and risk selecting a sibling resource.
                    command_override = Command.model_validate(raw_command)
                else:
                    from greenbook_agent_core.conversation import ExecutionControlCommand

                    command_override = ExecutionControlCommand.model_validate(raw_command)
            trace_id = str(payload.get("trace_id") or run.run_id)

            def emit_activity(event_type: str, event_payload: dict):
                # Project real Observations into user-facing partial results
                # (ACTION_COMPLETED -> ACTION_SUCCEEDED/FAILED + PARTIAL_RESULT).
                _project_and_emit_run_event(app, run.run_id, event_type, dict(event_payload))
                return None

            activity_cb = lambda event_type, event_payload: emit_activity(  # noqa: E731
                event_type, dict(event_payload)
            )
            confirmation_resume = payload.get("semantic_confirmation_resume")
            if isinstance(confirmation_resume, dict):
                executor = getattr(app.state, "action_loop_executor", None)
                resume = getattr(executor, "resume_task", None)
                if callable(resume):
                    return await resume(
                        task_id=str(
                            confirmation_resume.get("task_id")
                            or (payload.get("task_ids") or [""])[0]
                        ),
                        conversation_id=run.conversation_id,
                        user_id=run.user_id,
                        tenant_id=run.tenant_id,
                        run_id=run.run_id,
                        trace_id=trace_id,
                        session=session,
                        timezone=str(payload.get("timezone") or "Asia/Shanghai"),
                        mcp=app.state.mcp,
                        auth=auth,
                        activity_callback=activity_cb,
                        command=None,
                        expected_confirmation_id=str(
                            confirmation_resume.get("confirmation_id") or ""
                        ),
                        expected_confirmation_version=int(
                            confirmation_resume.get("confirmation_version", 0) or 0
                        ),
                        expected_task_version=int(
                            confirmation_resume.get("task_version", 0) or 0
                        ),
                    )
            turn_coordinator = getattr(app.state, "turn_coordinator", None)
            if turn_coordinator is not None:
                return await turn_coordinator.execute(
                    conversation_id=run.conversation_id,
                    user_id=run.user_id,
                    tenant_id=run.tenant_id,
                    message=str(payload.get("message") or ""),
                    history=None,
                    session=session,
                    timezone=str(payload.get("timezone") or "Asia/Shanghai"),
                    run_id=run.run_id,
                    trace_id=trace_id,
                    mcp=app.state.mcp,
                    llm=app.state.llm,
                    model=llm_model,
                    auth=auth,
                    command_override=command_override,
                    idempotency_key=str(payload.get("idempotency_key") or ""),
                    activity_callback=activity_cb,
                )
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=run.run_id,
                trace_id=trace_id,
                execution_path="action_loop",
                error_code="CANONICAL_RUNTIME_INCOMPLETE",
            )

        async def _runner_result_handler(run, result):
            from greenbook_agent_core.context import SessionContext
            from greenbook_contracts.identity import AuthContext

            payload = dict(run.payload or {})
            auth = AuthContext(
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                roles=[str(role) for role in (payload.get("roles") or [])],
                timezone=str(payload.get("timezone") or "Asia/Shanghai"),
                raw_access_token="",
            )
            session = SessionContext.model_validate(payload.get("session") or {})
            await handle_run_result(
                app,
                result,
                conversation_id=run.conversation_id,
                auth=auth,
                session=session,
                message_content=str(payload.get("message") or ""),
                trace_id=str(payload.get("trace_id") or run.run_id),
            )

        agent_runner = AgentRunner(
            run_store=run_store,
            event_store=run_event_store,
            execute=_runner_execute,
            result_handler=_runner_result_handler,
            worker_id="agent-runner",
            poll_interval_seconds=float(
                _env_first("GREENBOOK_AGENT_RUNNER_POLL_SECONDS", default="0.5")
            ),
            lease_seconds=int(_env_first("GREENBOOK_AGENT_RUNNER_LEASE_SECONDS", default="300")),
            max_concurrent_runs=int(
                _env_first("GREENBOOK_AGENT_MAX_CONCURRENT_RUNS", default="4")
            ),
            max_concurrent_per_conversation=int(
                _env_first(
                    "GREENBOOK_AGENT_MAX_CONCURRENT_RUNS_PER_CONVERSATION",
                    default="2",
                )
            ),
        )
        agent_runner_task = asyncio.create_task(
            agent_runner.run(),
            name="agent-immediate-accept-runner",
        )
        background_tasks.append(agent_runner_task)
        logger.info("Immediate-accept Agent runner started")
    else:
        logger.error("Immediate-accept Agent runner skipped: runtime persistence bind is unavailable")

    logger.info(
        "GreenBook Agent API ready java=%s issuer=%s audience=%s model=%s storage=%s dispatch=%s",
        java_base,
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
    app.include_router(debug_router)

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        java_ok = False
        java_base = ""
        with suppress(Exception):
            java_base = str(request.app.state.java.http.base_url).rstrip("/")

        async def probe(base_url: str, path: str) -> bool:
            if not base_url:
                return False
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{base_url}{path}")
                return 200 <= response.status_code < 300
            except httpx.HTTPError:
                return False

        java_ok = await probe(java_base, "/actuator/health")
        return {
            "status": "UP" if java_ok else "DEGRADED",
            "version": "2.0.0",
            "javaConfigured": bool(java_base),
            "javaReachable": java_ok,
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
