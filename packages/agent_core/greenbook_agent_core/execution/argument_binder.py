"""Schema-driven binding from resolved PlanStep data to tool arguments."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_agent_core.time_parser import parse_natural_schedule_time

if TYPE_CHECKING:
    from greenbook_agent_core.runtime.container import RuntimeContainer

    from .input import ExecutionInput

ToolArguments = dict[str, Any]
ToolSchemaSource = (
    Mapping[str, Any]
    | Sequence[Mapping[str, Any]]
    | Callable[[str], Mapping[str, Any] | None]
)


class ArgumentBinder:
    """Bind a ``PlanStep`` using the selected tool's exported schema.

    The binder deliberately knows capabilities and field semantics, but not
    individual MCP tool names.  Tool names are resolved through the capability
    registry and field availability comes from the tool schema.  This keeps
    the binding layer usable with both the in-process MCP server and test
    doubles that expose OpenAI-style tool definitions.
    """

    def __init__(
        self,
        tool_schemas: ToolSchemaSource | None = None,
        *,
        registry: CapabilityRegistry | None = None,
        container: RuntimeContainer | None = None,
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
    ) -> None:
        if container is None:
            from greenbook_agent_core.runtime.container import RuntimeContainer

            container = RuntimeContainer.for_testing()
        self._tool_schemas = tool_schemas
        self._container = container
        self._registry = registry or self._container.capability_registry
        self._timezone = timezone
        self._now = now

    def bind(
        self,
        step: PlanStep,
        *,
        execution_input: ExecutionInput | None = None,
    ) -> ToolArguments:
        """Return arguments accepted by the tool selected for ``step``."""

        tool_name = self._tool_name(step)
        schema = self._schema_for(tool_name)
        if execution_input is None:
            raise ValueError("ArgumentBinder requires a resolved ExecutionInput.")
        return self._bind_resolved_step(step, schema, execution_input)

    def bind_plan(
        self,
        plan: Any,
        *,
        execution_input: ExecutionInput | None = None,
    ) -> Any:
        """Bind known arguments into every step before execution starts."""

        for step in plan.steps:
            bound = self.bind(
                step,
                execution_input=execution_input,
            )
            # Replace planner metadata with the concrete tool arguments.  In
            # particular, ``time``/``approval`` are request-level fields; the
            # schedule step must carry canonical ``run_at`` instead.
            step.constraints = bound
        return plan

    def _bind_resolved_step(
        self,
        step: PlanStep,
        schema: Mapping[str, Any] | None,
        execution_input: ExecutionInput,
    ) -> ToolArguments:
        """Filter already-resolved arguments; never infer from text."""

        properties, _required = self._schema_fields(step, schema)
        arguments = dict(getattr(execution_input, "arguments", {}) or {})
        arguments.update(dict(step.constraints or {}))
        bound = {
            str(key): value
            for key, value in arguments.items()
            if str(key) in properties and value is not None and value != ""
        }
        self._normalize_schedule_time(
            step,
            bound,
            execution_input=execution_input,
            properties=properties,
        )
        return bound

    def _normalize_schedule_time(
        self,
        step: PlanStep,
        bound: ToolArguments,
        *,
        execution_input: ExecutionInput,
        properties: Mapping[str, Mapping[str, Any] | None],
    ) -> None:
        """Resolve user-relative schedule text at the execution boundary.

        The planner may return a natural-language value for ``run_at`` even
        though Java accepts only an ISO instant.  The queue payload already
        contains the root goal and its creation time, so this deterministic
        adapter can normalize the value without teaching the Worker how to
        understand user requests or adding a second routing path.
        """

        if str(getattr(step, "tool_name", "")) not in {
            "publication.schedule",
            "publication.update_schedule",
        }:
            return
        if "run_at" not in properties:
            return

        reference_time = self._now
        if reference_time is None:
            created_at = str(getattr(execution_input, "created_at", "") or "")
            if created_at:
                try:
                    reference_time = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    reference_time = None

        parsed = parse_natural_schedule_time(
            str(getattr(execution_input, "goal", "") or ""),
            self._timezone,
            now=reference_time,
        )
        if parsed:
            bound["run_at"] = parsed
            if "timezone" in properties:
                bound["timezone"] = self._timezone

    def _tool_name(self, step: PlanStep) -> str:
        selected = str(getattr(step, "tool_name", "") or "")
        if selected:
            return selected

        capability = self._registry.get(step.capability)
        # A single declared tool is an unambiguous legacy plan.  When a
        # capability has multiple tools, the AgentLoop/ToolSelector must
        # provide the explicit PlanStep.tool_name instead of silently
        # choosing by registry order.
        if capability and len(capability.tools) == 1:
            return next(iter(capability.tools))
        return step.capability

    def _schema_for(self, tool_name: str) -> Mapping[str, Any] | None:
        source = self._tool_schemas
        if source is None:
            return None
        raw: Any = None
        if callable(source) and not isinstance(source, type):
            raw = source(tool_name)
        elif isinstance(source, Mapping):
            raw = source.get(tool_name) or source.get(tool_name.replace(".", "_"))
        else:
            for item in source:
                if not isinstance(item, Mapping):
                    continue
                candidate = item.get("function", item)
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("name")
                    in {tool_name, tool_name.replace(".", "_")}
                ):
                    raw = candidate
                    break
        if not isinstance(raw, Mapping):
            return None
        function = raw.get("function")
        if isinstance(function, Mapping):
            raw = function
        parameters = raw.get("parameters")
        if isinstance(parameters, Mapping):
            return parameters
        return raw

    def _schema_fields(
        self,
        step: PlanStep,
        schema: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Mapping[str, Any] | None], list[str]]:
        properties = schema.get("properties") if schema else None
        if isinstance(properties, Mapping) and properties:
            property_map = {
                str(name): value if isinstance(value, Mapping) else {}
                for name, value in properties.items()
            }
            required = [
                str(name)
                for name in schema.get("required", [])
                if str(name) in property_map
            ]
            return property_map, required

        capability = self._registry.get(step.capability)
        if capability is None:
            return {str(name): None for name in step.constraints}, []
        names = list(dict.fromkeys(capability.inputs.required + capability.inputs.optional))
        return {name: None for name in names}, list(capability.inputs.required)

__all__ = ["ArgumentBinder", "ToolArguments"]
