"""ToolRuntime — unified tool invocation with timeout, idempotency, audit.

Phase 4.3: sits between CapabilityExecutor and the raw MCP tool_handler.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ..evidence import ExecutionEvidence
from .invocation_context import ToolInvocationContext
from .ledger import ToolExecutionLedger

logger = logging.getLogger(__name__)

# The raw handler signature: (tool_name, tool_args) → dict
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class AsyncTaskHandle:
    """Non-blocking result returned by a long-running tool.

    ``awaitable`` is consumed exactly once by ToolRuntime.  The handle itself
    is deliberately small so an MCP adapter can return it without coupling
    the execution state model to a Creator-specific task implementation.
    """

    task_id: str
    awaitable: Awaitable[dict[str, Any]]
    status: str = "RUNNING"
    metadata: dict[str, Any] = field(default_factory=dict)
    # Absolute UTC deadline for the downstream task.  When omitted, the
    # invocation timeout is used as the hand-off deadline by ToolRuntime.
    deadline: datetime | None = None

    async def wait(self) -> dict[str, Any]:
        return await self.awaitable


class InvocationResult(BaseModel):
    """Structured result of one tool invocation."""

    ok: bool = False
    invocation_id: str = ""
    tool_name: str = ""
    data: dict[str, Any] = {}
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    request_sent: bool | None = None
    duration_ms: float = 0.0
    replayed: bool = False           # True when returned from ledger cache
    status: str = "COMPLETED"
    pending: bool = False
    async_task_id: str = ""
    evidence: ExecutionEvidence | None = None

    @classmethod
    def from_tool_result(
        cls,
        invocation_id: str,
        tool_name: str,
        raw: dict[str, Any],
        duration_ms: float,
        evidence: ExecutionEvidence | None = None,
    ) -> InvocationResult:
        resolved_evidence = ExecutionEvidence.from_payload(raw, base=evidence)
        raw_request_sent = raw.get("request_sent", resolved_evidence.request_sent)
        return cls(
            ok=bool(raw.get("ok", False)),
            invocation_id=invocation_id,
            tool_name=tool_name,
            data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
            error_code=str(raw.get("code") or ""),
            error_message=str(raw.get("user_message") or raw.get("message", "")),
            retryable=bool(raw.get("retryable", False)),
            request_sent=(
                raw_request_sent
                if isinstance(raw_request_sent, bool) or raw_request_sent is None
                else resolved_evidence.request_sent
            ),
            duration_ms=duration_ms,
            status="COMPLETED" if bool(raw.get("ok", False)) else "FAILED",
            evidence=resolved_evidence,
        )


class ToolRuntime:
    """Unified invocation boundary for MCP tools.

    Usage::

        runtime = ToolRuntime(tool_handler, ledger=ToolExecutionLedger())
        ctx = ToolInvocationContext.build(tool_name="...", ...)
        result = await runtime.invoke(ctx)
    """

    def __init__(
        self,
        tool_handler: ToolHandler,
        ledger: ToolExecutionLedger | None = None,
        trace: Any = None,  # AgentTrace | None
        on_async_complete: Callable[
            [ToolInvocationContext, InvocationResult], Awaitable[None] | None
        ] | None = None,
    ) -> None:
        self._handler = tool_handler
        self._ledger = ledger or ToolExecutionLedger()
        self._trace = trace
        self._on_async_complete = on_async_complete
        self._pending_results: dict[str, InvocationResult] = {}
        self._async_results: dict[str, InvocationResult] = {}

    # ── main entry ───────────────────────────────────────────────

    async def invoke(self, ctx: ToolInvocationContext) -> InvocationResult:
        """Execute *ctx* through the tool handler with full lifecycle."""

        base_evidence = ExecutionEvidence.from_context(ctx)

        # 1. Idempotency check — replay completed calls
        if ctx.idempotency_key:
            pending = self._pending_results.get(ctx.idempotency_key)
            if pending is not None:
                replay = pending.model_copy(deep=True)
                replay.replayed = True
                return replay
            async_result = self._async_results.get(ctx.idempotency_key)
            if async_result is not None:
                replay = async_result.model_copy(deep=True)
                replay.replayed = True
                return replay
            cached = self._ledger.try_replay(ctx.idempotency_key)
            if cached is not None:
                logger.debug("Replaying cached invocation %s", ctx.invocation_id)
                result = InvocationResult.from_tool_result(
                    ctx.invocation_id, ctx.tool_name, cached.result, 0.0,
                    evidence=cached.evidence or base_evidence,
                )
                result.replayed = True
                return result

        # 2. Record start
        try:
            self._ledger.record_start(ctx, evidence=base_evidence)
        except ValueError:
            # Key conflict (race) — try replay one more time
            cached = self._ledger.try_replay(ctx.idempotency_key)
            if cached is not None:
                result = InvocationResult.from_tool_result(
                    ctx.invocation_id, ctx.tool_name, cached.result, 0.0,
                    evidence=cached.evidence or base_evidence,
                )
                result.replayed = True
                return result
            raise

        # 3. Emit trace: TOOL_INVOKED
        if self._trace is not None:
            self._trace._emit_raw(ctx.tool_name, "TOOL_INVOKED",
                                  step_id=ctx.step_id, capability=ctx.capability)

        # 4. Execute with timeout
        start = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self._handler(ctx.tool_name, ctx.tool_args),
                timeout=ctx.timeout_seconds,
            )
            elapsed = (time.monotonic() - start) * 1000.0
        except TimeoutError:
            elapsed = (time.monotonic() - start) * 1000.0
            evidence = ExecutionEvidence.from_payload(
                {},
                base=base_evidence,
                request_sent=None,
                side_effect_state="UNKNOWN",
                error_code="TIMEOUT",
                raw_error_type="TimeoutError",
                phase="TOOL_RUNTIME_TIMEOUT",
            )
            self._ledger.record_timeout(
                ctx.invocation_id,
                elapsed,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(ctx.tool_name, "TOOL_FAILED",
                                      step_id=ctx.step_id, capability=ctx.capability,
                                      payload={"error": "TIMEOUT"})
            return InvocationResult(
                invocation_id=ctx.invocation_id,
                tool_name=ctx.tool_name,
                error_code="TIMEOUT",
                error_message=f"Tool '{ctx.tool_name}' timed out after {ctx.timeout_seconds}s",
                retryable=True,
                duration_ms=elapsed,
                status="TIMEOUT",
                evidence=evidence,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000.0
            evidence = ExecutionEvidence.from_payload(
                {},
                base=base_evidence,
                request_sent=None,
                side_effect_state="UNKNOWN",
                error_code="TOOL_EXECUTION_FAILED",
                raw_error_type=type(exc).__name__,
                phase="TOOL_HANDLER_EXCEPTION",
            )
            self._ledger.record_failure(
                ctx.invocation_id,
                "TOOL_EXECUTION_FAILED",
                str(exc),
                elapsed,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(ctx.tool_name, "TOOL_FAILED",
                                      step_id=ctx.step_id, capability=ctx.capability,
                                      payload={"error": str(exc)})
            return InvocationResult(
                invocation_id=ctx.invocation_id,
                tool_name=ctx.tool_name,
                error_code="TOOL_EXECUTION_FAILED",
                error_message=str(exc),
                retryable=False,
                duration_ms=elapsed,
                status="FAILED",
                evidence=evidence,
            )

        # 5. A long tool may acknowledge work before its final result exists.
        # Do not apply the normal timeout to the task's completion awaitable;
        # the returned handle is tracked by the Runtime instead.
        if isinstance(raw, AsyncTaskHandle):
            pending_evidence = ExecutionEvidence.from_payload(
                raw.metadata,
                base=base_evidence,
                request_sent=True,
                external_operation_id=raw.task_id,
                phase="ASYNC_ACCEPTED",
            )
            self._ledger.record_evidence(
                ctx.invocation_id,
                pending_evidence,
            )
            pending = InvocationResult(
                ok=False,
                invocation_id=ctx.invocation_id,
                tool_name=ctx.tool_name,
                data={
                    "task_id": raw.task_id,
                    "status": raw.status,
                    **raw.metadata,
                },
                duration_ms=elapsed,
                status=raw.status,
                pending=True,
                async_task_id=raw.task_id,
                evidence=pending_evidence,
            )
            if ctx.idempotency_key:
                self._pending_results[ctx.idempotency_key] = pending
            if self._trace is not None:
                self._trace._emit_raw(
                    ctx.tool_name,
                    "TOOL_ASYNC_STARTED",
                    step_id=ctx.step_id,
                    capability=ctx.capability,
                    payload={"task_id": raw.task_id},
                )
            asyncio.create_task(
                self._complete_async_handle(ctx, raw),
                name=f"tool-task:{raw.task_id}",
            )
            return pending

        # 6. Record result
        ok = bool(raw.get("ok", False))
        code = str(raw.get("code") or "")
        evidence = ExecutionEvidence.from_payload(
            raw,
            base=base_evidence,
            error_code=code or None,
        )
        if ok:
            self._ledger.record_complete(
                ctx.invocation_id,
                raw,
                elapsed,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(ctx.tool_name, "TOOL_COMPLETED",
                                      step_id=ctx.step_id, capability=ctx.capability,
                                      payload={"ok": True})
        else:
            code = code or "TOOL_EXECUTION_FAILED"
            msg = str(raw.get("user_message") or raw.get("message", ""))
            evidence = ExecutionEvidence.from_payload(
                raw,
                base=base_evidence,
                error_code=code,
            )
            self._ledger.record_failure(
                ctx.invocation_id,
                code,
                msg,
                elapsed,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(ctx.tool_name, "TOOL_FAILED",
                                      step_id=ctx.step_id, capability=ctx.capability,
                                      payload={"error": code})

        return InvocationResult.from_tool_result(
            ctx.invocation_id,
            ctx.tool_name,
            raw,
            elapsed,
            evidence=evidence,
        )

    async def _complete_async_handle(
        self,
        ctx: ToolInvocationContext,
        handle: AsyncTaskHandle,
    ) -> None:
        ledger_entry = self._ledger.find_by_id(ctx.invocation_id)
        base_evidence = (
            ledger_entry.evidence
            if ledger_entry is not None and ledger_entry.evidence is not None
            else ExecutionEvidence.from_context(ctx)
        )
        start = time.monotonic()
        try:
            deadline = handle.deadline or (
                datetime.now(UTC) + timedelta(seconds=ctx.timeout_seconds)
            )
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TimeoutError
            raw_value = await asyncio.wait_for(handle.wait(), timeout=remaining)
            if hasattr(raw_value, "model_dump"):
                raw = raw_value.model_dump(mode="json")
            elif isinstance(raw_value, dict):
                raw = raw_value
            else:
                raw = {
                    "ok": False,
                    "code": "TOOL_EXECUTION_FAILED",
                    "message": "Async tool returned an invalid result",
                }
            elapsed = (time.monotonic() - start) * 1000.0
            code = str(raw.get("code") or "")
            evidence = ExecutionEvidence.from_payload(
                raw,
                base=base_evidence,
                error_code=code or None,
            )
            if raw.get("ok", False):
                self._ledger.record_complete(
                    ctx.invocation_id,
                    raw,
                    elapsed,
                    evidence=evidence,
                )
            else:
                self._ledger.record_failure(
                    ctx.invocation_id,
                    code or "TOOL_EXECUTION_FAILED",
                    str(raw.get("user_message") or raw.get("message", "")),
                    elapsed,
                    raw,
                    evidence=evidence,
                )
            result = InvocationResult.from_tool_result(
                ctx.invocation_id,
                ctx.tool_name,
                raw,
                elapsed,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(
                    ctx.tool_name,
                    "TOOL_ASYNC_COMPLETED" if result.ok else "TOOL_FAILED",
                    step_id=ctx.step_id,
                    capability=ctx.capability,
                    payload={"task_id": handle.task_id, "ok": result.ok},
                )
        except TimeoutError:
            elapsed = (time.monotonic() - start) * 1000.0
            message = f"Async tool '{ctx.tool_name}' exceeded its deadline"
            raw = {
                "ok": False,
                "code": "TIMEOUT",
                "user_message": message,
                "retryable": True,
            }
            evidence = ExecutionEvidence.from_payload(
                raw,
                base=base_evidence,
                request_sent=None,
                side_effect_state="UNKNOWN",
                raw_error_type="TimeoutError",
                phase="ASYNC_TOOL_TIMEOUT",
            )
            self._ledger.record_timeout(
                ctx.invocation_id,
                elapsed,
                raw,
                evidence=evidence,
            )
            result = InvocationResult(
                invocation_id=ctx.invocation_id,
                tool_name=ctx.tool_name,
                error_code="TIMEOUT",
                error_message=message,
                retryable=True,
                duration_ms=elapsed,
                status="TIMEOUT",
                async_task_id=handle.task_id,
                evidence=evidence,
            )
            if self._trace is not None:
                self._trace._emit_raw(
                    ctx.tool_name,
                    "TOOL_FAILED",
                    step_id=ctx.step_id,
                    capability=ctx.capability,
                    payload={"task_id": handle.task_id, "error": "TIMEOUT"},
                )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000.0
            evidence = ExecutionEvidence.from_payload(
                {},
                base=base_evidence,
                request_sent=None,
                side_effect_state="UNKNOWN",
                error_code="TOOL_EXECUTION_FAILED",
                raw_error_type=type(exc).__name__,
                phase="ASYNC_HANDLER_EXCEPTION",
            )
            self._ledger.record_failure(
                ctx.invocation_id,
                "TOOL_EXECUTION_FAILED",
                str(exc),
                elapsed,
                evidence=evidence,
            )
            result = InvocationResult(
                invocation_id=ctx.invocation_id,
                tool_name=ctx.tool_name,
                error_code="TOOL_EXECUTION_FAILED",
                error_message=str(exc),
                duration_ms=elapsed,
                status="FAILED",
                async_task_id=handle.task_id,
                evidence=evidence,
            )

        # Store both success and failure before resuming the Worker.  The
        # resumed step therefore replays this result and never calls the MCP
        # tool a second time.
        if ctx.idempotency_key:
            self._async_results[ctx.idempotency_key] = result.model_copy(deep=True)
            self._pending_results.pop(ctx.idempotency_key, None)

        if self._on_async_complete is not None:
            try:
                callback_result = self._on_async_complete(ctx, result)
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception:
                # The Runtime callback owns the state transition to FAILED;
                # re-raise so a broken callback cannot look like success.
                logger.exception(
                    "Async completion callback failed task_id=%s",
                    handle.task_id,
                )
                raise

    # ── queries ──

    @property
    def ledger(self) -> ToolExecutionLedger:
        return self._ledger
