"""CapabilityExecutor — map a PlanStep's capability to a tool call and execute it.

Phase 4.0: one-shot execution via raw tool_handler.
Phase 5.1: supports ToolRuntime via invoke_fn (ToolInvocationContext → dict).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.argument_binder import ArgumentBinder
from greenbook_assistant_core.orchestration.context import PlanningContext
from greenbook_assistant_core.orchestration.models import PlanStep
from greenbook_assistant_core.task.intent_models import IntentSpec

from .invocation import ExecutionResult
from .models import ArtifactHandle
from .runtime.invocation_context import ToolInvocationContext

logger = logging.getLogger(__name__)

# Legacy: (tool_name, tool_args) → dict
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# New: (ToolInvocationContext) → dict  (wraps ToolRuntime.invoke)
InvokeFn = Callable[[ToolInvocationContext], Awaitable[dict[str, Any]]]


class CapabilityExecutor:
    """Execute a single PlanStep by resolving its capability to an MCP tool.

    Accepts either a raw *tool_handler* (legacy) or an *invoke_fn* that
    wraps ToolRuntime (Phase 5.1+).  When *invoke_fn* is provided, every
    tool call gets a full ToolInvocationContext with idempotency key and
    timeout metadata.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        tool_handler: ToolHandler | None = None,
        *,
        invoke_fn: InvokeFn | None = None,
        task_id: str = "",
        execution_id: str = "",
        argument_binder: ArgumentBinder | None = None,
        planning_context: PlanningContext | None = None,
        intent_spec: IntentSpec | None = None,
        user_message: str = "",
        timezone: str = "Asia/Shanghai",
        active_draft_id: str | None = None,
        active_schedule_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._tool_handler = tool_handler
        self._invoke_fn = invoke_fn
        self._task_id = task_id
        self._execution_id = execution_id
        self._argument_binder = argument_binder
        self._planning_context = planning_context
        self._intent_spec = intent_spec
        self._user_message = user_message
        self._timezone = timezone
        self._active_draft_id = active_draft_id
        self._active_schedule_id = active_schedule_id

    # ── main entry ───────────────────────────────────────────────

    async def execute_step(self, step: PlanStep) -> ExecutionResult:
        """Execute *step* and return a structured ExecutionResult."""

        # 1. Look up capability
        cap = self._registry.get(step.capability)
        if cap is None:
            return ExecutionResult.unknown_capability(step.capability)

        # 2. LLM-only step — no tool call needed
        if cap.is_llm_step:
            return ExecutionResult.success(
                capability=cap.name,
                tool_name="(llm)",
                tool_result={"llm_step": True, "description": step.description},
                artifact=ArtifactHandle(
                    artifact_type=cap.output_artifact_type,
                    summary=step.description,
                ) if cap.output_artifact_type else None,
            )

        # 3. Pick tool
        if not cap.tools:
            return ExecutionResult.missing_tool(cap.name)

        tool_name = cap.tools[0]

        # 4. Build tool args
        tool_args = self._bound_tool_args(step)

        # 5. Call through invoke_fn (ToolRuntime) or raw tool_handler
        try:
            if self._invoke_fn is not None:
                ctx = ToolInvocationContext.build(
                    task_id=self._task_id,
                    execution_id=self._execution_id,
                    step_id=step.step_id,
                    capability=cap.name,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    timeout_seconds=120.0,
                )
                result = await self._invoke_fn(ctx)
            elif self._tool_handler is not None:
                result = await self._tool_handler(tool_name, tool_args)
            else:
                return ExecutionResult.from_tool_error(
                    capability=cap.name,
                    tool_name=tool_name,
                    error_code="NO_HANDLER",
                    error_message="No tool_handler or invoke_fn configured",
                    retryable=False,
                )
        except Exception:
            logger.exception("Tool handler raised for capability=%s tool=%s",
                             cap.name, tool_name)
            return ExecutionResult.from_tool_error(
                capability=cap.name,
                tool_name=tool_name,
                error_code="TOOL_EXECUTION_FAILED",
                error_message="Tool handler raised an exception",
                retryable=False,
            )

        # 6. Interpret result
        ok = bool(result.get("ok"))
        code = str(result.get("code") or "")

        if bool(result.get("pending")):
            task_id = str(
                result.get("async_task_id")
                or (result.get("data") or {}).get("task_id", "")
            )
            return ExecutionResult.pending_result(
                capability=cap.name,
                tool_name=tool_name,
                tool_result=result,
                task_id=task_id,
            )

        if code == "APPROVAL_REQUIRED":
            return ExecutionResult.approval_required_result(cap.name, tool_name)

        if ok:
            artifact = self._extract_artifact(cap.name, cap.output_artifact_type, result)
            return ExecutionResult.success(
                capability=cap.name,
                tool_name=tool_name,
                tool_result=result,
                artifact=artifact,
            )

        return ExecutionResult.from_tool_error(
            capability=cap.name,
            tool_name=tool_name,
            error_code=code or "TOOL_EXECUTION_FAILED",
            error_message=str(result.get("user_message") or result.get("message", "")),
            retryable=bool(result.get("retryable", False)),
            request_sent=bool(result.get("request_sent", False)),
        )

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _build_tool_args(step: PlanStep) -> dict[str, Any]:
        args: dict[str, Any] = dict(step.constraints)
        return args

    def _bound_tool_args(self, step: PlanStep) -> dict[str, Any]:
        """Bind the step at the last safe boundary before MCP invocation."""

        if self._argument_binder is None:
            return self._build_tool_args(step)
        return self._argument_binder.bind(
            step,
            self._planning_context,
            self._intent_spec,
            user_message=self._user_message,
            timezone=self._timezone,
            active_draft_id=self._active_draft_id,
            active_schedule_id=self._active_schedule_id,
        )

    @staticmethod
    def _extract_artifact(
        capability_name: str,
        artifact_type: str,
        result: dict[str, Any],
    ) -> ArtifactHandle | None:
        if not artifact_type:
            return None
        data = result.get("data")
        if not isinstance(data, dict):
            return None
        summary = str(data.get("title") or data.get("summary") or "")
        if not summary and data.get("items"):
            items = data["items"]
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    summary = str(first.get("title") or "")
                summary = f"{summary} (+{len(items) - 1} more)" if len(items) > 1 else summary
        resource_id: str | None = None
        for key in ("draft_id", "schedule_id", "post_id"):
            val = data.get(key)
            if val:
                resource_id = str(val)
                break
        return ArtifactHandle(
            artifact_type=artifact_type,
            resource_id=resource_id,
            summary=summary,
        )
