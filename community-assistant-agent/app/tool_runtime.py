from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.artifact_contracts import ArtifactBinder, artifact_binder
from app.domain import ResolvedTargetView, TargetBinding
from app.tools import (
    RiskLevel,
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
    TransportType,
)


# ---------------------------------------------------------------------------
# Argument preparation (existing adapter — unchanged public API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRuntimeContext:
    prompt: str
    context_post_id: str | None
    context_comment_id: str | None
    resolved_targets: dict[
        str, TargetBinding | ResolvedTargetView | dict[str, Any]
    ] | None = None


class ToolAdapterRuntime:
    """Prepare a Tool invocation from declarative defaults and artifacts."""

    def __init__(self, binder: ArtifactBinder = artifact_binder) -> None:
        self.binder = binder

    def prepare_arguments(
        self,
        *,
        definition,
        planner_arguments: dict[str, Any],
        artifacts: list[dict[str, Any]],
        context: ToolRuntimeContext,
        binding_sources: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        arguments = {
            key: value
            for key, value in planner_arguments.items()
            if key not in definition.runtime_bound_arguments
        }
        for key, value in definition.argument_defaults.items():
            arguments.setdefault(key, value)
        if definition.prompt_argument:
            arguments[definition.prompt_argument] = str(
                arguments.get(definition.prompt_argument) or context.prompt
            )
        context_values = {
            "context_post_id": context.context_post_id,
            "context_comment_id": context.context_comment_id,
        }
        for argument, source in definition.context_arguments.items():
            arguments[argument] = str(
                arguments.get(argument) or context_values.get(source) or ""
            )
        return self.binder.bind(
            bindings=definition.artifact_bindings,
            arguments=arguments,
            artifacts=artifacts,
            binding_sources=binding_sources,
            resolved_targets=context.resolved_targets,
            required_target_roles=definition.required_target_roles,
            optional_target_roles=definition.optional_target_roles,
        )


tool_adapter_runtime = ToolAdapterRuntime()


# ---------------------------------------------------------------------------
# Phase 5 Step 1 — unified invocation contracts
# ---------------------------------------------------------------------------


class ToolInvocationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    DENIED = "DENIED"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    UNKNOWN = "UNKNOWN"


class ToolErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    CAPABILITY_EXHAUSTED = "CAPABILITY_EXHAUSTED"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_UPSTREAM = "TRANSIENT_UPSTREAM"
    PERMANENT_UPSTREAM = "PERMANENT_UPSTREAM"
    OUTPUT_SCHEMA_ERROR = "OUTPUT_SCHEMA_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolInvocationContext(BaseModel):
    """Reusable invocation identity. Never carries capability token plaintext."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_id: str | None = None
    user_id: str
    tenant_id: str | None = None
    conversation_id: str | None = None

    request_id: str
    operation_key: str | None = None
    idempotency_key: str | None = None

    attempt: int = 1
    deadline_at: datetime | None = None

    workload_lane: str | None = None
    trace_metadata: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolInvocationStatus
    output: dict[str, Any] | None = None

    error_code: str | None = None
    error_message: str | None = None

    attempts: int
    duration_ms: int

    side_effect_id: str | None = None
    replayed: bool = False

    trace_id: str


@dataclass
class ToolAttemptTrace:
    attempt: int
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    status: str = "STARTED"
    error_code: str | None = None
    http_status: int | None = None
    retry_after_ms: int | None = None
    internal_call_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCredentials:
    """Per-invocation secrets. Never stored on ToolInvocationContext or Trace."""

    access_token: str
    trace_id: str | None = None


@dataclass
class ToolInvocationTrace:
    """Logical invocation trace. One trace_id per logical call; attempts share it."""

    trace_id: str
    run_id: str
    step_id: str | None
    tool_name: str
    transport: str
    request_id: str
    operation_key: str | None

    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None

    attempt_count: int = 1
    status: str = "STARTED"
    error_code: str | None = None
    replayed: bool = False
    argument_summary: dict[str, Any] = field(default_factory=dict)
    attempts: list[ToolAttemptTrace] = field(default_factory=list)
    budget_requested: int | None = None
    internal_calls_consumed: int | None = None


# Tools still routed through Worker._dispatch_builtin_tool. Remove an entry
# when that tool gains a first-class transport in a later step.
LEGACY_BUILTIN_MIGRATION_BACKLOG: frozenset[str] = frozenset(
    {
        "community.get_post",
        "community.analyze_engagement",
        "community.list_active_users",
        "community.list_posts_by_users",
        "community.aggregate_post_topics",
        "community.summarize_post",
        "community.get_own_draft",
        "publication.schedule_batch",
        "publication.publish_now",
        "community.reply_comment",
        "community.delete_post",
        "community.delete_own_posts_batch",
    }
)

MIGRATED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "community.list_own_posts",
        "community.search_posts",
        "publication.get_schedule",
    }
)

MIGRATED_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "publication.update_schedule",
        "publication.schedule",
        "publication.cancel_schedule",
        "creator.create_draft",
        "creator.revise_draft",
    }
)


class UnknownSideEffectError(RuntimeError):
    """Write path timed out or lost confirmation; do not blindly retry."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = ToolErrorCode.UNKNOWN_SIDE_EFFECT.value,
        operation_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.operation_key = operation_key


class ToolRuntimeError(RuntimeError):
    """Structured tool failure raised at the Runtime boundary when needed."""

    def __init__(
        self,
        message: str,
        *,
        status: ToolInvocationStatus,
        error_code: str,
        attempts: int = 1,
        duration_ms: int = 0,
        trace_id: str | None = None,
        side_effect_id: str | None = None,
        operation_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.attempts = attempts
        self.duration_ms = duration_ms
        self.trace_id = trace_id
        self.side_effect_id = side_effect_id
        self.operation_key = operation_key


def _safe_argument_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Schema-safe metadata only — never capability tokens or raw secrets."""
    summary: dict[str, Any] = {"keys": sorted(arguments.keys())}
    for key in ("query", "limit", "post_id", "draft_id", "schedule_id", "goal_id"):
        if key in arguments and arguments[key] is not None:
            value = arguments[key]
            if isinstance(value, str) and len(value) > 120:
                summary[key] = value[:117] + "..."
            else:
                summary[key] = value
    return summary


def classify_tool_exception(
    error: BaseException,
    *,
    definition: ToolDefinition | None = None,
    side_effecting: bool | None = None,
) -> tuple[ToolInvocationStatus, ToolErrorCode]:
    """Map legacy exceptions to structured Runtime status + error code."""
    write = (
        side_effecting
        if side_effecting is not None
        else bool(definition and definition.side_effecting)
    )

    if isinstance(error, UnknownSideEffectError):
        return ToolInvocationStatus.UNKNOWN, ToolErrorCode.UNKNOWN_SIDE_EFFECT

    if isinstance(error, LookupError):
        return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.CONFLICT

    if isinstance(error, ValidationError):
        return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.VALIDATION_ERROR

    if isinstance(error, PermissionError):
        message = str(error).lower()
        if "capability" in message and (
            "exhaust" in message or "budget" in message or "uses" in message
        ):
            return ToolInvocationStatus.DENIED, ToolErrorCode.CAPABILITY_EXHAUSTED
        if "capability" in message:
            return ToolInvocationStatus.DENIED, ToolErrorCode.CAPABILITY_DENIED
        return ToolInvocationStatus.DENIED, ToolErrorCode.POLICY_DENIED

    # ApprovalRequired / DependencyPending are control-flow; callers re-raise.
    error_name = type(error).__name__
    if error_name == "ApprovalRequired":
        return ToolInvocationStatus.DENIED, ToolErrorCode.POLICY_DENIED
    if error_name == "DependencyPending":
        return ToolInvocationStatus.WAITING_DEPENDENCY, ToolErrorCode.INTERNAL_ERROR

    if isinstance(error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        if write:
            return ToolInvocationStatus.UNKNOWN, ToolErrorCode.UNKNOWN_SIDE_EFFECT
        return ToolInvocationStatus.RETRYABLE_FAILURE, ToolErrorCode.TIMEOUT

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {401, 403}:
            body = ""
            try:
                body = (error.response.text or "").lower()
            except Exception:
                body = ""
            if "exhaust" in body or "max_uses" in body or "budget" in body:
                return ToolInvocationStatus.DENIED, ToolErrorCode.CAPABILITY_EXHAUSTED
            if "capability" in body:
                return ToolInvocationStatus.DENIED, ToolErrorCode.CAPABILITY_DENIED
            return ToolInvocationStatus.DENIED, ToolErrorCode.AUTHENTICATION_ERROR
        if status == 404:
            return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.NOT_FOUND
        if status in {409, 412}:
            return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.CONFLICT
        if status == 429:
            return ToolInvocationStatus.RETRYABLE_FAILURE, ToolErrorCode.RATE_LIMITED
        if status in {502, 503, 504} or status >= 500:
            if write and status in {502, 503, 504}:
                # Upstream may have applied the write before failing the response.
                return ToolInvocationStatus.UNKNOWN, ToolErrorCode.UNKNOWN_SIDE_EFFECT
            return (
                ToolInvocationStatus.RETRYABLE_FAILURE,
                ToolErrorCode.TRANSIENT_UPSTREAM,
            )
        return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.PERMANENT_UPSTREAM

    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        if write:
            return ToolInvocationStatus.UNKNOWN, ToolErrorCode.UNKNOWN_SIDE_EFFECT
        return ToolInvocationStatus.RETRYABLE_FAILURE, ToolErrorCode.TRANSIENT_UPSTREAM

    message = str(error).lower()
    if "output schema" in message or "output_schema" in message:
        return (
            ToolInvocationStatus.PERMANENT_FAILURE,
            ToolErrorCode.OUTPUT_SCHEMA_ERROR,
        )
    if "idempoten" in message:
        return (
            ToolInvocationStatus.PERMANENT_FAILURE,
            ToolErrorCode.IDEMPOTENCY_CONFLICT,
        )

    # Legacy TransientToolError on write paths that already marked UNKNOWN
    # in the ledger must not be auto-retried as a fresh write.
    if error_name == "TransientToolError" and write:
        return ToolInvocationStatus.UNKNOWN, ToolErrorCode.UNKNOWN_SIDE_EFFECT
    if error_name == "TransientToolError":
        return (
            ToolInvocationStatus.RETRYABLE_FAILURE,
            ToolErrorCode.TRANSIENT_UPSTREAM,
        )

    return ToolInvocationStatus.PERMANENT_FAILURE, ToolErrorCode.INTERNAL_ERROR


LegacyDispatchFn = Callable[..., Awaitable[dict[str, Any]]]


class LegacyBuiltinTransport:
    """Migration bridge: ToolRuntime.invoke → existing Worker dispatch.

    Not a long-term authority. Each migrated tool leaves
    LEGACY_BUILTIN_MIGRATION_BACKLOG and gains a first-class transport.
    """

    transport_type = TransportType.LEGACY_BUILTIN

    def __init__(self, dispatch: LegacyDispatchFn) -> None:
        self._dispatch = dispatch

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        run: Any,
        ordinal: int,
        timeout_seconds: int,
        continuation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation_key = context.operation_key or (
            f"assistant-read-{context.run_id}-{ordinal}"
        )
        return await self._dispatch(
            run=run,
            tool=tool_name,
            args=arguments,
            ordinal=ordinal,
            timeout_seconds=timeout_seconds,
            operation_key=operation_key,
            continuation=continuation,
        )


class ToolRuntime:
    """Per-Worker / per-App tool execution runtime with instance-local handlers."""

    def __init__(
        self,
        *,
        definitions: ToolRegistry,
        legacy_dispatch: LegacyDispatchFn | None = None,
        legacy_executor: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        trace_recorder: Callable[[ToolInvocationTrace], Awaitable[None] | None]
        | None = None,
        capability_provider: Any | None = None,
        policy_gate: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.definitions = definitions
        self._handlers: dict[str, ToolHandler] = {}
        self._transports: dict[TransportType, Any] = {}
        self._legacy_executor = legacy_executor
        self._trace_recorder = trace_recorder
        self._capability_provider = capability_provider
        self._policy_gate = policy_gate
        self._traces: dict[str, ToolInvocationTrace] = {}
        if legacy_dispatch is not None:
            self._transports[TransportType.LEGACY_BUILTIN] = LegacyBuiltinTransport(
                legacy_dispatch
            )

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        self.definitions.get(tool_name)
        if tool_name in self._handlers:
            raise ValueError(f"Duplicate tool handler: {tool_name}")
        self._handlers[tool_name] = handler

    def register_or_replace_handler(
        self, tool_name: str, handler: ToolHandler
    ) -> None:
        self.definitions.get(tool_name)
        self._handlers[tool_name] = handler

    def handler_for(self, tool_name: str) -> ToolHandler | None:
        self.definitions.get(tool_name)
        return self._handlers.get(tool_name)

    def adopt_staged_handlers(self, registry: ToolRegistry | None = None) -> int:
        """Move staged handlers from a definition registry into this runtime."""
        source = registry or self.definitions
        staged = source.drain_handlers()
        for name, handler in staged.items():
            self.register_or_replace_handler(name, handler)
        return len(staged)

    def set_legacy_executor(
        self, executor: Callable[..., Awaitable[dict[str, Any]]]
    ) -> None:
        self._legacy_executor = executor

    def set_legacy_dispatch(self, dispatch: LegacyDispatchFn) -> None:
        self._transports[TransportType.LEGACY_BUILTIN] = LegacyBuiltinTransport(
            dispatch
        )

    def set_capability_provider(self, provider: Any) -> None:
        self._capability_provider = provider

    def set_policy_gate(
        self, gate: Callable[..., Awaitable[None]] | None
    ) -> None:
        self._policy_gate = gate

    def last_trace(self, trace_id: str) -> ToolInvocationTrace | None:
        return self._traces.get(trace_id)

    async def reconcile(
        self,
        invocation_context: ToolInvocationContext,
        side_effect: Any,
    ) -> ToolInvocationResult | None:
        """Reserved for Step 3+ automatic reconciliation. Not implemented yet."""
        del invocation_context, side_effect
        return None

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        run: Any | None = None,
        ordinal: int | None = None,
        continuation: dict[str, Any] | None = None,
        credentials: ToolCredentials | None = None,
        skip_input_validation: bool = False,
        skip_policy: bool = False,
        raise_on_failure: bool = True,
    ) -> ToolInvocationResult:
        """Single formal tool invocation entry point."""
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc)
        trace_id = str(
            context.trace_metadata.get("trace_id")
            or context.request_id
            or uuid.uuid4()
        )
        definition = self.definitions.get(tool_name)
        transport_name = definition.transport.value
        trace = ToolInvocationTrace(
            trace_id=trace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            tool_name=tool_name,
            transport=transport_name,
            request_id=context.request_id,
            operation_key=context.operation_key,
            started_at=started_at,
            attempt_count=0,
            argument_summary=_safe_argument_summary(arguments),
            budget_requested=definition.capability_budget.max_internal_calls,
        )
        self._traces[trace_id] = trace
        attempts = 0
        deadline_at = context.deadline_at or (
            started_at + timedelta(seconds=definition.timeout_seconds)
        )

        try:
            if skip_input_validation:
                validated = arguments
            else:
                try:
                    validated = self.definitions.validate(tool_name, arguments)
                except (ValidationError, ValueError, TypeError) as exc:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    result = ToolInvocationResult(
                        status=ToolInvocationStatus.PERMANENT_FAILURE,
                        error_code=ToolErrorCode.VALIDATION_ERROR.value,
                        error_message=str(exc),
                        attempts=0,
                        duration_ms=duration_ms,
                        trace_id=trace_id,
                    )
                    await self._complete_trace(
                        trace, result.status.value, result.error_code, duration_ms
                    )
                    if raise_on_failure:
                        raise ToolRuntimeError(
                            str(exc),
                            status=result.status,
                            error_code=ToolErrorCode.VALIDATION_ERROR.value,
                            attempts=0,
                            duration_ms=duration_ms,
                            trace_id=trace_id,
                            operation_key=context.operation_key,
                        ) from exc
                    return result

            if (
                not skip_policy
                and self._policy_gate is not None
                and tool_name in (MIGRATED_READ_TOOLS | MIGRATED_WRITE_TOOLS)
            ):
                await self._policy_gate(
                    tool_name=tool_name,
                    arguments=validated,
                    context=context,
                    run=run,
                    definition=definition,
                )

            replayed = False
            if tool_name in MIGRATED_WRITE_TOOLS:
                output, attempts, replayed = await self._invoke_migrated_write(
                    definition=definition,
                    tool_name=tool_name,
                    arguments=validated,
                    context=context,
                    credentials=credentials,
                    deadline_at=deadline_at,
                    trace=trace,
                    ordinal=ordinal if ordinal is not None else 0,
                )
            elif tool_name in MIGRATED_READ_TOOLS:
                output, attempts = await self._invoke_migrated_read(
                    definition=definition,
                    tool_name=tool_name,
                    arguments=validated,
                    context=context,
                    credentials=credentials,
                    deadline_at=deadline_at,
                    trace=trace,
                )
            else:
                attempts = 1
                output = await self._dispatch_transport(
                    definition=definition,
                    tool_name=tool_name,
                    arguments=validated,
                    context=context,
                    run=run,
                    ordinal=ordinal if ordinal is not None else 0,
                    continuation=continuation,
                )

            try:
                checked = self.definitions.validate_output(
                    tool_name,
                    output,
                    validated,
                    run_id=context.run_id,
                )
            except (ValidationError, ValueError, TypeError) as exc:
                duration_ms = int((time.perf_counter() - started) * 1000)
                result = ToolInvocationResult(
                    status=ToolInvocationStatus.PERMANENT_FAILURE,
                    error_code=ToolErrorCode.OUTPUT_SCHEMA_ERROR.value,
                    error_message=str(exc),
                    attempts=max(attempts, 1),
                    duration_ms=duration_ms,
                    trace_id=trace_id,
                )
                await self._complete_trace(
                    trace, result.status.value, result.error_code, duration_ms
                )
                if raise_on_failure:
                    raise ToolRuntimeError(
                        str(exc),
                        status=result.status,
                        error_code=ToolErrorCode.OUTPUT_SCHEMA_ERROR.value,
                        attempts=result.attempts,
                        duration_ms=duration_ms,
                        trace_id=trace_id,
                        operation_key=context.operation_key,
                    ) from exc
                return result

            duration_ms = int((time.perf_counter() - started) * 1000)
            trace.attempt_count = max(attempts, 1)
            trace.internal_calls_consumed = sum(
                item.internal_call_count for item in trace.attempts
            )
            result = ToolInvocationResult(
                status=ToolInvocationStatus.SUCCESS,
                output=checked,
                attempts=max(attempts, 1),
                duration_ms=duration_ms,
                trace_id=trace_id,
                replayed=replayed
                or bool(context.trace_metadata.get("replayed")),
                side_effect_id=(
                    str(context.trace_metadata["side_effect_id"])
                    if context.trace_metadata.get("side_effect_id")
                    else None
                ),
            )
            await self._complete_trace(trace, result.status.value, None, duration_ms)
            return result

        except ToolRuntimeError:
            raise
        except BaseException as exc:
            if type(exc).__name__ in {"ApprovalRequired", "DependencyPending"}:
                duration_ms = int((time.perf_counter() - started) * 1000)
                status, code = classify_tool_exception(exc, definition=definition)
                await self._complete_trace(
                    trace, status.value, code.value, duration_ms
                )
                raise

            status, code = classify_tool_exception(exc, definition=definition)
            duration_ms = int((time.perf_counter() - started) * 1000)
            trace.attempt_count = max(attempts, context.attempt, len(trace.attempts))
            trace.internal_calls_consumed = sum(
                item.internal_call_count for item in trace.attempts
            )
            result = ToolInvocationResult(
                status=status,
                error_code=code.value,
                error_message=str(exc),
                attempts=max(attempts, context.attempt, 1),
                duration_ms=duration_ms,
                trace_id=trace_id,
                side_effect_id=(
                    str(context.trace_metadata["side_effect_id"])
                    if context.trace_metadata.get("side_effect_id")
                    else None
                ),
            )
            await self._complete_trace(
                trace, result.status.value, result.error_code, duration_ms
            )
            if raise_on_failure:
                raise ToolRuntimeError(
                    str(exc),
                    status=status,
                    error_code=code.value,
                    attempts=result.attempts,
                    duration_ms=duration_ms,
                    trace_id=trace_id,
                    operation_key=context.operation_key
                    or getattr(exc, "operation_key", None),
                    side_effect_id=result.side_effect_id,
                ) from exc
            return result

    async def _invoke_migrated_write(
        self,
        *,
        definition: ToolDefinition,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        credentials: ToolCredentials | None,
        deadline_at: datetime,
        trace: ToolInvocationTrace,
        ordinal: int,
    ) -> tuple[dict[str, Any], int, bool]:
        """Write tools: no blind retry; handler owns SideEffect + reconcile."""

        del definition
        handler = self.handler_for(tool_name)
        if handler is None:
            raise ValueError(f"No execution handler registered for tool: {tool_name}")
        if credentials is None or not credentials.access_token:
            raise PermissionError(f"{tool_name} requires delegated credentials")

        remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("tool invocation deadline exceeded")

        attempt_trace = ToolAttemptTrace(
            attempt=1,
            started_at=datetime.now(timezone.utc),
        )
        trace.attempts.append(attempt_trace)
        try:
            raw = await asyncio.wait_for(
                handler(
                    arguments=arguments,
                    context=context,
                    definition=self.definitions.get(tool_name),
                    capability=None,
                    credentials=credentials,
                    deadline_at=deadline_at,
                    attempt_trace=attempt_trace,
                    ordinal=ordinal,
                ),
                timeout=max(0.01, remaining),
            )
            replayed = bool(raw.pop("_runtime_replayed", False))
            reconciled = bool(raw.pop("_runtime_reconciled", False))
            if attempt_trace.metadata.get("side_effect_id"):
                context.trace_metadata["side_effect_id"] = attempt_trace.metadata[
                    "side_effect_id"
                ]
            attempt_trace.status = "SUCCESS"
            attempt_trace.completed_at = datetime.now(timezone.utc)
            attempt_trace.duration_ms = int(
                (
                    attempt_trace.completed_at - attempt_trace.started_at
                ).total_seconds()
                * 1000
            )
            attempt_trace.metadata["reconciled"] = reconciled
            return raw, 1, replayed or reconciled
        except BaseException as exc:
            status, code = classify_tool_exception(
                exc, definition=self.definitions.get(tool_name)
            )
            attempt_trace.status = status.value
            attempt_trace.error_code = code.value
            attempt_trace.completed_at = datetime.now(timezone.utc)
            attempt_trace.duration_ms = int(
                (
                    attempt_trace.completed_at - attempt_trace.started_at
                ).total_seconds()
                * 1000
            )
            if attempt_trace.metadata.get("side_effect_id"):
                context.trace_metadata["side_effect_id"] = attempt_trace.metadata[
                    "side_effect_id"
                ]
            raise

    async def _invoke_migrated_read(
        self,
        *,
        definition: ToolDefinition,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        credentials: ToolCredentials | None,
        deadline_at: datetime,
        trace: ToolInvocationTrace,
    ) -> tuple[dict[str, Any], int]:
        handler = self.handler_for(tool_name)
        if handler is None:
            raise ValueError(f"No execution handler registered for tool: {tool_name}")

        policy = definition.retry_policy
        max_attempts = max(1, int(policy.max_attempts))
        last_error: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise TimeoutError("tool invocation deadline exceeded")

            attempt_trace = ToolAttemptTrace(
                attempt=attempt,
                started_at=datetime.now(timezone.utc),
            )
            trace.attempts.append(attempt_trace)
            grant = None
            needs_capability = (
                definition.capability_budget.max_internal_calls > 0
                or definition.capability_budget.base_uses > 0
            )
            try:
                if needs_capability:
                    if self._capability_provider is not None:
                        if credentials is None or not credentials.access_token:
                            raise PermissionError(
                                f"{tool_name} requires delegated credentials"
                            )
                        max_uses = max(
                            1, int(definition.capability_budget.max_internal_calls)
                        )
                        grant = await self._capability_provider.issue(
                            action=tool_name,
                            resources=[],
                            max_uses=max_uses,
                            ttl_seconds=120,
                            context=context,
                            credentials=credentials,
                        )
                    else:
                        from app.clients import CapabilityGrant as _Grant

                        grant = _Grant(
                            token="test-capability",
                            capability_id="test",
                            expires_at="2099-01-01T00:00:00Z",
                        )

                output = await asyncio.wait_for(
                    handler(
                        arguments=arguments,
                        context=context,
                        definition=definition,
                        capability=grant,
                        credentials=credentials
                        or ToolCredentials(access_token="", trace_id=None),
                        deadline_at=deadline_at,
                        attempt_trace=attempt_trace,
                    ),
                    timeout=max(0.01, remaining),
                )
                attempt_trace.status = "SUCCESS"
                attempt_trace.completed_at = datetime.now(timezone.utc)
                attempt_trace.duration_ms = int(
                    (
                        attempt_trace.completed_at - attempt_trace.started_at
                    ).total_seconds()
                    * 1000
                )
                return output, attempt
            except BaseException as exc:
                last_error = exc
                status, code = classify_tool_exception(exc, definition=definition)
                attempt_trace.status = status.value
                attempt_trace.error_code = code.value
                attempt_trace.completed_at = datetime.now(timezone.utc)
                attempt_trace.duration_ms = int(
                    (
                        attempt_trace.completed_at - attempt_trace.started_at
                    ).total_seconds()
                    * 1000
                )
                if isinstance(exc, httpx.HTTPStatusError):
                    attempt_trace.http_status = exc.response.status_code
                    retry_after = _parse_retry_after_ms(exc.response)
                    attempt_trace.retry_after_ms = retry_after

                retryable = _is_read_retryable(
                    status=status,
                    code=code,
                    policy=policy,
                    http_status=attempt_trace.http_status,
                )
                if not retryable or attempt >= max_attempts:
                    raise

                delay_ms = _next_backoff_ms(
                    attempt=attempt,
                    policy=policy,
                    retry_after_ms=attempt_trace.retry_after_ms,
                    deadline_at=deadline_at,
                )
                if delay_ms is None:
                    raise
                await asyncio.sleep(delay_ms / 1000.0)

        assert last_error is not None
        raise last_error

    async def _dispatch_transport(
        self,
        *,
        definition: ToolDefinition,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        run: Any,
        ordinal: int,
        continuation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Migrated tools never enter legacy_executor.
        if tool_name in MIGRATED_READ_TOOLS or tool_name in MIGRATED_WRITE_TOOLS:
            raise RuntimeError(
                f"{tool_name} must use migrated ToolRuntime path, not legacy dispatch"
            )

        if self._legacy_executor is not None:
            return await self._legacy_executor(
                tool_name=tool_name,
                arguments=arguments,
                context=context,
                run=run,
                ordinal=ordinal,
                continuation=continuation,
            )

        if definition.transport == TransportType.LEGACY_BUILTIN:
            transport = self._transports.get(TransportType.LEGACY_BUILTIN)
            if transport is not None:
                return await transport.invoke(
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    run=run,
                    ordinal=ordinal,
                    timeout_seconds=definition.timeout_seconds,
                    continuation=continuation,
                )

        handler = self.handler_for(tool_name)
        if handler is None:
            raise ValueError(f"No execution handler registered for tool: {tool_name}")
        operation_key = context.operation_key or (
            f"assistant-read-{context.run_id}-{ordinal}"
        )
        return await handler(
            run=run,
            tool=tool_name,
            args=arguments,
            ordinal=ordinal,
            timeout_seconds=definition.timeout_seconds,
            operation_key=operation_key,
            continuation=continuation,
        )

    async def _complete_trace(
        self,
        trace: ToolInvocationTrace,
        status: str,
        error_code: str | None,
        duration_ms: int,
    ) -> None:
        trace.status = status
        trace.error_code = error_code
        trace.duration_ms = duration_ms
        trace.completed_at = datetime.now(timezone.utc)
        if self._trace_recorder is not None:
            maybe = self._trace_recorder(trace)
            if asyncio.iscoroutine(maybe):
                await maybe


def _parse_retry_after_ms(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0, int(float(raw) * 1000))
    except ValueError:
        return None


def _is_read_retryable(
    *,
    status: ToolInvocationStatus,
    code: ToolErrorCode,
    policy: Any,
    http_status: int | None,
) -> bool:
    if status != ToolInvocationStatus.RETRYABLE_FAILURE:
        return False
    if code in {
        ToolErrorCode.VALIDATION_ERROR,
        ToolErrorCode.POLICY_DENIED,
        ToolErrorCode.CAPABILITY_DENIED,
        ToolErrorCode.CAPABILITY_EXHAUSTED,
        ToolErrorCode.AUTHENTICATION_ERROR,
        ToolErrorCode.NOT_FOUND,
        ToolErrorCode.OUTPUT_SCHEMA_ERROR,
        ToolErrorCode.UNKNOWN_SIDE_EFFECT,
    }:
        return False
    if http_status == 401:
        return False
    if http_status is not None and http_status in set(
        policy.retryable_http_statuses or ()
    ):
        return True
    if code.value in set(policy.retryable_error_codes or ()):
        return True
    return code in {
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.RATE_LIMITED,
        ToolErrorCode.TRANSIENT_UPSTREAM,
    }


def _next_backoff_ms(
    *,
    attempt: int,
    policy: Any,
    retry_after_ms: int | None,
    deadline_at: datetime,
) -> int | None:
    remaining_ms = int(
        (deadline_at - datetime.now(timezone.utc)).total_seconds() * 1000
    )
    if remaining_ms <= 0:
        return None
    if retry_after_ms is not None:
        delay = retry_after_ms
    else:
        delay = min(
            int(policy.max_backoff_ms),
            int(policy.initial_backoff_ms) * (2 ** max(0, attempt - 1)),
        )
    if delay >= remaining_ms:
        return None
    return max(0, delay)


def create_tool_runtime(
    *,
    definitions: ToolRegistry,
    legacy_dispatch: LegacyDispatchFn | None = None,
) -> ToolRuntime:
    """Factory for a fresh, instance-isolated ToolRuntime."""
    return ToolRuntime(definitions=definitions, legacy_dispatch=legacy_dispatch)
