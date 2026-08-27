"""CapabilityExecutor — map a PlanStep's capability to a tool call and execute it.

Phase 4.0: one-shot execution via raw tool_handler.
Phase 5.1: supports ToolRuntime via invoke_fn (ToolInvocationContext → dict).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.argument_binder import ArgumentBinder
from greenbook_agent_core.observability.context import TraceContext
from greenbook_agent_core.planning.contracts import PlanStep

from .failure_decision import normalize_failure_payload
from .input import ExecutionInput
from .invocation import ExecutionResult
from .models import ArtifactHandle
from .runtime.invocation_context import ToolInvocationContext

logger = logging.getLogger(__name__)

# Legacy: (tool_name, tool_args) → dict
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# New: (ToolInvocationContext) → dict  (wraps ToolRuntime.invoke)
InvokeFn = Callable[[ToolInvocationContext], Awaitable[dict[str, Any]]]


class ResourceBindingError(ValueError):
    """A step cannot bind a valid Objective-owned resource (controlled reject)."""


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
        execution_input: ExecutionInput | None = None,
        user_message: str = "",
        timezone: str = "Asia/Shanghai",
        active_draft_id: str | None = None,
        active_schedule_id: str | None = None,
        trace_context: TraceContext | None = None,
        tool_registry: Any | None = None,
        objective_id: str | None = None,
        objective_draft_ids: Sequence[str] | None = None,
        objective_schedule_ids: Sequence[str] | None = None,
        objective_dependency_draft_ids: Sequence[str] | None = None,
    ) -> None:
        self._registry = registry
        self._tool_handler = tool_handler
        self._invoke_fn = invoke_fn
        self._task_id = task_id
        self._execution_id = execution_id
        self._argument_binder = argument_binder
        self._execution_input = execution_input
        self._user_message = user_message
        self._timezone = timezone
        self._active_draft_id = active_draft_id
        self._active_schedule_id = active_schedule_id
        self._trace_context = trace_context or TraceContext()
        self._objective_id = objective_id
        self._objective_draft_ids = tuple(
            str(rid) for rid in (objective_draft_ids or ()) if rid
        )
        self._objective_schedule_ids = tuple(
            str(rid) for rid in (objective_schedule_ids or ()) if rid
        )
        self._objective_dependency_draft_ids = tuple(
            str(rid) for rid in (objective_dependency_draft_ids or ()) if rid
        )
        self._tool_registry = tool_registry

    def bind_trace_context(self, context: TraceContext) -> None:
        """Update correlation metadata after the Execution id is allocated."""

        self._trace_context = context

    def bind_execution_id(self, execution_id: str) -> None:
        """Bind the durable Execution id before the first tool invocation."""

        self._execution_id = execution_id

    # ── main entry ───────────────────────────────────────────────

    async def execute_step(self, step: PlanStep) -> ExecutionResult:
        """Execute *step* and return a structured ExecutionResult."""

        # 1. Look up capability
        cap = self._registry.get(step.capability)
        if cap is None:
            return self._failure_result(
                capability=step.capability,
                tool_name="",
                error_code="UNKNOWN_CAPABILITY",
                error_message=f"Capability '{step.capability}' is not registered",
                retryable=False,
                request_sent=False,
            )

        # 2. Reasoning-backed steps belong to AgentLoop.PRODUCE_RESULT.  A
        # Worker reaching this branch is an execution-semantics violation;
        # returning success here would manufacture a false business result.
        if cap.is_llm_step:
            return self._failure_result(
                capability=cap.name,
                tool_name="(llm)",
                error_code="WRONG_EXECUTION_SEMANTICS",
                error_message=(
                    "Reasoning-backed capability reached Worker; use "
                    "AgentLoop.PRODUCE_RESULT."
                ),
                retryable=False,
                request_sent=False,
            )

        # 3. Use a runtime-selected tool when one is present. Legacy plans
        # with exactly one declared tool remain compatible; multi-tool
        # capabilities must be selected by AgentLoop/ToolSelector instead of
        # silently taking a positional entry.
        tool_name = str(getattr(step, "tool_name", "") or "")
        if not tool_name and len(cap.tools) == 1:
            tool_name = next(iter(cap.tools))
        if not tool_name:
            return self._failure_result(
                capability=cap.name,
                tool_name="",
                error_code="TOOL_SELECTION_REQUIRED",
                error_message=(
                    f"Capability '{cap.name}' requires a runtime-selected tool"
                ),
                retryable=False,
                request_sent=False,
            )
        if tool_name not in cap.tools:
            return self._failure_result(
                capability=cap.name,
                tool_name=tool_name,
                error_code="TOOL_NOT_DECLARED",
                error_message=(
                    f"Tool '{tool_name}' is not declared for capability '{cap.name}'"
                ),
                retryable=False,
                request_sent=False,
            )

        # 4. Build tool args
        try:
            tool_args = self._bound_tool_args(step)
        except ResourceBindingError as exc:
            return self._failure_result(
                capability=cap.name,
                tool_name=tool_name,
                error_code="INVALID_RESOURCE_BINDING",
                error_message=str(exc),
                retryable=False,
                request_sent=False,
            )

        # 5. Call through invoke_fn (ToolRuntime) or raw tool_handler
        try:
            if self._invoke_fn is not None:
                ctx = ToolInvocationContext.build(
                    task_id=self._task_id,
                    execution_id=self._execution_id,
                    step_id=step.step_id,
                    goal_id=str(getattr(step, "goal_id", "") or "") or None,
                    capability=cap.name,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    timeout_seconds=self._timeout_for(tool_name),
                    trace_context=self._trace_context.for_step(step.step_id),
                    objective_id=self._objective_id,
                )
                result = await self._invoke_fn(ctx)
            elif self._tool_handler is not None:
                result = await self._tool_handler(tool_name, tool_args)
            else:
                return self._failure_result(
                    capability=cap.name,
                    tool_name=tool_name,
                    error_code="NO_HANDLER",
                    error_message="No tool_handler or invoke_fn configured",
                    retryable=False,
                    request_sent=False,
                )
        except Exception:
            logger.exception("Tool handler raised for capability=%s tool=%s",
                             cap.name, tool_name)
            return self._failure_result(
                capability=cap.name,
                tool_name=tool_name,
                error_code="INTERNAL_ERROR",
                error_message="Tool handler raised an exception",
                retryable=False,
                # The exception is raised by the local Runtime boundary.  A
                # side-effect adapter that loses a downstream acknowledgement
                # must return RESULT_UNKNOWN with explicit evidence instead;
                # this generic path must not manufacture a reconcile case.
                request_sent=False,
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

        return self._failure_result(
            capability=cap.name,
            tool_name=tool_name,
            payload=result,
            error_code=code or "INTERNAL_ERROR",
            error_message=str(result.get("user_message") or result.get("message", "")),
            retryable=bool(result.get("retryable", False)),
            request_sent=result.get("request_sent", False),
        )

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _failure_result(
        *,
        capability: str,
        tool_name: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        request_sent: bool | None,
        payload: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        tool_result, failure = normalize_failure_payload(
            payload,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            request_sent=request_sent,
        )
        return ExecutionResult.from_tool_error(
            capability=capability,
            tool_name=tool_name,
            error_code=error_code,
            error_message=error_message,
            retryable=failure.retryable,
            request_sent=failure.request_sent,
            tool_result=tool_result,
            external_failure=failure,
        )

    @staticmethod
    def _build_tool_args(step: PlanStep) -> dict[str, Any]:
        args: dict[str, Any] = dict(step.constraints)
        return args

    def _bound_tool_args(self, step: PlanStep) -> dict[str, Any]:
        """Bind the step at the last safe boundary before MCP invocation."""

        if self._argument_binder is None:
            bound = self._build_tool_args(step)
        else:
            bound = self._argument_binder.bind(
                step,
                execution_input=self._execution_input,
            )
        return self._bind_scoped_resource_arguments(step, bound)

    def _bind_scoped_resource_arguments(
        self,
        step: PlanStep,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill a missing canonical resource field from this Task's scope.

        The adapter supplies a per-Task ``active_draft_id`` /
        ``active_schedule_id`` snapshot to the Worker.  They are not
        conversation-global fallbacks: a value is present only when that Task
        has exactly one durable resource of the matching type.  Apply it only
        for a field declared by the Capability contract and never override an
        explicit plan argument.  This is a schema/capability binding rule, not
        a tool-name-specific workflow shortcut.
        """

        capability = self._registry.get(str(getattr(step, "capability", "") or ""))
        if capability is None:
            return arguments
        declared_fields = set(
            getattr(getattr(capability, "inputs", None), "required", ()) or ()
        ) | set(
            getattr(getattr(capability, "inputs", None), "optional", ()) or ()
        )

        # Objective-owned resource authority: a CREATE_SCHEDULE/PUBLISH_NOW
        # under a Business Objective normally consumes that Objective's OWN
        # Draft.  The only explicit dependency exception is one verified Draft
        # produced by a declared predecessor Objective.  It remains owned by
        # the predecessor; this boundary merely passes the typed artifact into
        # the dependent action.  Task-global latest/first resources and model
        # guesses remain invalid.
        if (
            self._objective_id
            and capability.name in {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}
            and "draft_id" in declared_fields
        ):
            owned = list(self._objective_draft_ids)
            if len(owned) == 1:
                arguments["draft_id"] = owned[0]
                return arguments
            supplied = str(arguments.get("draft_id") or "")
            if supplied and supplied in owned:
                return arguments
            dependency_owned = list(self._objective_dependency_draft_ids)
            if not owned and len(dependency_owned) == 1:
                arguments["draft_id"] = dependency_owned[0]
                return arguments
            if not owned and supplied and supplied in dependency_owned and len(dependency_owned) == 1:
                arguments["draft_id"] = supplied
                return arguments
            raise ResourceBindingError(
                "INVALID_RESOURCE_BINDING: Objective "
                f"{self._objective_id} owns {len(owned)} draft(s) and its "
                f"explicit dependencies provide {len(dependency_owned)}; "
                f"{capability.name} requires exactly one verified Draft."
            )

        # Immediate publication has the same Draft ownership invariant as
        # scheduling. Keep it at the existing capability/resource binding
        # boundary so PUBLISH_NOW cannot fall back to a task-global or
        # model-guessed Draft.
        if (
            self._objective_id
            and capability.name == "PUBLISH_NOW"
            and "draft_id" in declared_fields
        ):
            owned = list(self._objective_draft_ids)
            if len(owned) == 1:
                arguments["draft_id"] = owned[0]
                return arguments
            supplied = str(arguments.get("draft_id") or "")
            if supplied and supplied in owned:
                return arguments
            raise ResourceBindingError(
                "INVALID_RESOURCE_BINDING: Objective "
                f"{self._objective_id} owns {len(owned)} draft(s); "
                "PUBLISH_NOW requires exactly one Objective-owned Draft."
            )

        # Objective-scoped mutation target: MANAGE_DRAFT / MANAGE_SCHEDULE must
        # mutate the Objective's OWN resource, never a cross-Objective id the
        # model supplied and never a task-global latest/first fallback.  A wrong
        # model id is normalized to the Objective's owned resource; a missing or
        # ambiguous owned resource is a controlled reject (no cross-objective
        # mutation, no side effect).
        if self._objective_id:
            if capability.name == "MANAGE_DRAFT" and "draft_id" in declared_fields:
                owned = list(self._objective_draft_ids)
                if len(owned) == 1:
                    arguments["draft_id"] = owned[0]
                    return arguments
                supplied = str(arguments.get("draft_id") or "")
                if supplied and supplied in owned:
                    return arguments
                raise ResourceBindingError(
                    "INVALID_RESOURCE_BINDING: Objective "
                    f"{self._objective_id} owns {len(owned)} draft(s); "
                    "MANAGE_DRAFT requires exactly one Objective-owned Draft."
                )
            if capability.name == "MANAGE_SCHEDULE" and "schedule_id" in declared_fields:
                owned = list(self._objective_schedule_ids)
                if len(owned) == 1:
                    arguments["schedule_id"] = owned[0]
                    return arguments
                supplied = str(arguments.get("schedule_id") or "")
                if supplied and supplied in owned:
                    return arguments
                raise ResourceBindingError(
                    "INVALID_RESOURCE_BINDING: Objective "
                    f"{self._objective_id} owns {len(owned)} schedule(s); "
                    "MANAGE_SCHEDULE requires exactly one Objective-owned Schedule."
                )
            if capability.name == "CANCEL_SCHEDULE" and "schedule_id" in declared_fields:
                owned = list(self._objective_schedule_ids)
                if len(owned) == 1:
                    arguments["schedule_id"] = owned[0]
                    return arguments
                supplied = str(arguments.get("schedule_id") or "")
                if supplied and supplied in owned:
                    return arguments
                raise ResourceBindingError(
                    "INVALID_RESOURCE_BINDING: Objective "
                    f"{self._objective_id} owns {len(owned)} schedule(s); "
                    "CANCEL_SCHEDULE requires exactly one Objective-owned Schedule."
                )

        scoped_values = {
            "draft_id": self._active_draft_id,
            "schedule_id": self._active_schedule_id,
        }
        for field, resource_id in scoped_values.items():
            if (
                field in declared_fields
                and field not in arguments
                and resource_id not in (None, "")
            ):
                arguments[field] = str(resource_id)
        return arguments

    def _timeout_for(self, tool_name: str) -> float:
        """Resolve the canonical timeout used by ToolRuntime."""

        registry = self._tool_registry
        metadata = None
        if registry is not None:
            getter = getattr(registry, "get_tool_metadata", None)
            if callable(getter):
                try:
                    metadata = getter(tool_name)
                except (KeyError, ValueError):
                    metadata = None
            if metadata is None:
                getter = getattr(registry, "get", None)
                if callable(getter):
                    try:
                        metadata = getter(tool_name)
                    except (KeyError, ValueError):
                        metadata = None
        policy = getattr(metadata, "policy", None)
        timeout = getattr(policy, "timeout_seconds", None)
        try:
            resolved = float(timeout)
        except (TypeError, ValueError):
            resolved = 120.0
        return resolved if resolved > 0 else 120.0

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
        resource_refs = _extract_resource_refs(result, data, artifact_type)
        resource_id: str | None = None
        resource_key = _resource_key_for_artifact_type(artifact_type)
        if resource_key and data.get(resource_key):
            resource_id = str(data[resource_key])
        if not resource_id:
            expected_kind = str(artifact_type).strip().upper()
            for ref in resource_refs:
                if str(ref.get("kind") or "").upper() == expected_kind:
                    resource_id = str(ref.get("resource_id") or "") or None
                    if resource_id:
                        break
        return ArtifactHandle(
            artifact_type=artifact_type,
            resource_id=resource_id,
            summary=summary,
            resource_refs=resource_refs,
        )


def _resource_key_for_artifact_type(artifact_type: str) -> str | None:
    normalized = str(artifact_type).strip().upper()
    if normalized in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
        return "draft_id"
    if normalized in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
        return "schedule_id"
    if normalized in {"POST", "PUBLISHED_POST", "PUBLICATION"}:
        return "post_id"
    return None


def _extract_resource_refs(
    result: dict[str, Any],
    data: dict[str, Any],
    artifact_type: str,
) -> list[dict[str, Any]]:
    """Keep stable resource identifiers for downstream argument binding."""

    raw_refs = result.get("resource_refs") or data.get("resource_refs") or []
    candidates: list[Any] = list(raw_refs) if isinstance(raw_refs, list) else []
    if str(artifact_type).upper() == "SEARCH_RESULT":
        candidates.extend(data.get("items") or data.get("posts") or [])

    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        resource_id = raw.get("resource_id")
        kind = raw.get("kind") or raw.get("resource_type") or raw.get("type")
        if not resource_id:
            resource_id = raw.get("post_id") or raw.get("postId") or raw.get("id")
            if resource_id and not kind and str(artifact_type).upper() == "SEARCH_RESULT":
                kind = "POST"
        if not resource_id or not kind:
            continue
        normalized = (str(kind).strip().upper(), str(resource_id))
        if normalized in seen:
            continue
        seen.add(normalized)
        refs.append({"kind": normalized[0], "resource_id": normalized[1]})
        if len(refs) >= 32:
            break
    return refs
