"""Assistant orchestration boundary for the canonical Execution Runtime."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..models.runtime_context import RuntimeContext
from ..models.runtime_result import RuntimeResult
from .execution_projection_adapter import ExecutionProjectionAdapter
from .runtime_router import ExecutionPath, RuntimeRouter

logger = logging.getLogger(__name__)


class AssistantService:
    """Route Assistant turns to Runtime without Legacy fallback."""

    def __init__(self, mode: str = "on") -> None:
        self._runtime: Any | None = None
        self._router = RuntimeRouter(mode)
        self._projection_adapter = ExecutionProjectionAdapter()
        self._background_results: dict[str, RuntimeResult] = {}

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        """Execute one Assistant turn through the canonical Runtime."""
        path = self._router.route(ctx)
        if path == ExecutionPath.RUNTIME and self._runtime is not None:
            return self._present(await self._execute_runtime(ctx))

        return self._present(RuntimeResult(
            success=False,
            status="FAILED",
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            error_code="LEGACY_EXECUTION_DISABLED",
            error_message="Legacy execution is retired; use the Runtime path.",
            fallback_allowed=False,
            execution_path="legacy",
        ))

    async def execute_background(
        self,
        ctx: RuntimeContext,
        *,
        completion_callback: Callable[[RuntimeResult], Awaitable[None] | None] | None = None,
    ) -> RuntimeResult:
        """Start Runtime execution and return once its PlanExecution exists.

        This is intentionally an additive API.  ``execute`` remains the
        synchronous contract used by short jobs and unit tests, while the
        HTTP message route can opt into a detached Worker pass for long tool
        calls such as Creator draft generation.
        """
        path = self._router.route(ctx)
        if path != ExecutionPath.RUNTIME or self._runtime is None:
            return await self.execute(ctx)

        async def on_complete(result: RuntimeResult) -> None:
            presented = self._present(result)
            self._background_results[presented.run_id] = presented
            if completion_callback is not None:
                callback_result = completion_callback(presented)
                if inspect.isawaitable(callback_result):
                    await callback_result

        execute = self._runtime.execute
        try:
            supports_detach = "detach" in inspect.signature(execute).parameters
        except (TypeError, ValueError):
            supports_detach = False
        if not supports_detach:
            return self._present(await self._execute_runtime(ctx))
        result = await execute(
            ctx,
            detach=True,
            completion_callback=on_complete,
        )
        return self._present(result)

    def background_result(self, run_id: str) -> RuntimeResult | None:
        """Read the latest detached result without consulting Legacy state."""
        result = self._background_results.get(run_id)
        if result is not None:
            return result
        runtime_result = getattr(self._runtime, "background_result", None)
        return runtime_result(run_id) if runtime_result is not None else None

    async def _execute_runtime(self, ctx: RuntimeContext) -> RuntimeResult:
        """Delegate to RuntimeAgentService and preserve Runtime failures."""
        if self._runtime is None:
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=ctx.run_id,
                trace_id=ctx.trace_id,
                error_code="RUNTIME_UNAVAILABLE",
                error_message="Runtime execution is not available.",
                execution_path="runtime",
            )
        try:
            return await self._runtime.execute(ctx)
        except Exception as exc:
            logger.exception("Runtime execution failed runtime_failure_reason=%s", type(exc).__name__)
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=ctx.run_id,
                trace_id=ctx.trace_id,
                error_code="RUNTIME_ERROR",
                error_message="Runtime execution failed",
                execution_path="runtime",
            )

    async def resume_runtime_approval(
        self, approval_id: str, decision: str,
    ) -> RuntimeResult | None:
        """Resume a Runtime approval without exposing Runtime internals."""
        if self._runtime is None:
            return None
        result = await self._runtime.resume_human_interaction(
            approval_id, "", decision=decision,
        )
        return self._present(result) if result is not None else None

    def register_runtime(self, runtime: Any) -> None:
        """Register RuntimeAgentService for the active application."""
        self._runtime = runtime

    def _present(self, result: RuntimeResult) -> RuntimeResult:
        """Attach user-facing presentation without changing Runtime state."""
        try:
            response = self._projection_adapter.project(result)
            result.presentation = response.model_dump(mode="json")
            result.content = response.message
        except Exception:
            # Formatting must never turn a completed execution into a failed
            # one.  Preserve the Runtime result and its original content if a
            # malformed optional artifact cannot be rendered.
            logger.exception("Execution result presentation failed")
        return result
