"""Schema-driven binding from resolved PlanStep data to tool arguments."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
from greenbook_agent_core.time_parser import TemporalBase

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
        # A top-level argument belongs only to the legacy single-step input
        # shape.  In a multi-step execution, each PlanStep owns its arguments;
        # treating the request-level scalar as a base would leak a schedule
        # time or target from one logical Goal into another.
        arguments = (
            dict(getattr(execution_input, "arguments", {}) or {})
            if len(getattr(execution_input, "steps", ()) or ()) <= 1
            else {}
        )
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
            semantic_arguments=arguments,
        )
        return bound

    def _normalize_schedule_time(
        self,
        step: PlanStep,
        bound: ToolArguments,
        *,
        execution_input: ExecutionInput,
        properties: Mapping[str, Mapping[str, Any] | None],
        semantic_arguments: Mapping[str, Any],
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

        temporal_base = self._temporal_base(semantic_arguments)
        reference_time = self._now
        if temporal_base == TemporalBase.EXISTING_SCHEDULE_TIME:
            reference_time = self._existing_schedule_time(semantic_arguments)
            if reference_time is None:
                # Production update_schedule can read the authoritative
                # Schedule immediately before its PUT.  Preserve the declared
                # base and raw expression for that handler when its schema
                # explicitly supports it; legacy schemas still fail closed.
                if "temporal_base" in properties:
                    bound["temporal_base"] = temporal_base.value
                    if "timezone" in properties and "timezone" not in bound:
                        bound["timezone"] = self._timezone
                    return
                raise ValueError(
                    "EXISTING_SCHEDULE_TIME requires authoritative existing_schedule_run_at"
                )
        elif reference_time is None:
            created_at = str(getattr(execution_input, "created_at", "") or "")
            if created_at:
                try:
                    reference_time = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    reference_time = None

        # A queued request may contain several schedule steps.  Their
        # temporal expressions belong to the individual PlanStep; parsing the
        # request-wide execution_input.goal would make every schedule inherit
        # the first goal's time.  Keep the single-step fallback only for the
        # legacy contract where the step itself did not carry run_at.
        raw_run_at = bound.get("run_at")
        explicit_run_at = raw_run_at not in (None, "")
        parse_source = str(raw_run_at or "")
        if not parse_source and len(getattr(execution_input, "steps", ()) or ()) == 1:
            parse_source = str(getattr(execution_input, "goal", "") or "")
        # A canonical ISO instant carries its own offset; never re-parse it as
        # natural language. A "Z" value is already UTC and passes through;
        # an explicit offset (e.g. +08:00) is normalized to the Java contract's
        # UTC "Z" form. Re-parsing "…T12:00:00Z" as natural language would
        # interpret the wall clock in the session timezone and shift the
        # instant by the offset (observed: stored as 04:00 instead of 12:00).
        iso_instant = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
            r"(?:Z|[+-]\d{2}:\d{2})$"
        )
        if iso_instant.match(parse_source):
            if not parse_source.endswith("Z"):
                try:
                    parsed_iso = datetime.fromisoformat(parse_source)
                    if parsed_iso.tzinfo is not None:
                        bound["run_at"] = parsed_iso.astimezone(UTC).isoformat().replace("+00:00", "Z")
                except ValueError:
                    pass
            if "timezone" in properties and "timezone" not in bound:
                bound["timezone"] = self._timezone

        elif parse_source:
            parsed = TemporalResolver(now=reference_time).resolve(
                parse_source,
                timezone=self._timezone,
            )
            if parsed:
                bound["run_at"] = parsed
                if "timezone" in properties:
                    bound["timezone"] = self._timezone
            elif explicit_run_at:
                # A schedule tool must never receive a natural-language
                # temporal value that the canonical resolver could not turn
                # into an instant.  Semantic clarification normally prevents
                # this path; the binder remains a final fail-closed boundary
                # for direct/replayed Runtime inputs.
                raise ValueError(
                    "Unresolved future temporal expression cannot be scheduled"
                )
        elif raw_run_at not in (None, "") and "timezone" in properties and "timezone" not in bound:
            # Canonical ISO values already carry their own instant.  Only add
            # the display/contract timezone when the caller did not provide
            # one; never apply a second clock conversion here.
            bound["timezone"] = self._timezone

    @staticmethod
    def _temporal_base(values: Mapping[str, Any]) -> TemporalBase:
        raw = values.get("temporal_base") or values.get("relative_to")
        nested = values.get("temporal_constraint")
        if not raw and isinstance(nested, Mapping):
            raw = nested.get("temporal_base") or nested.get("relative_to")
        if raw in (None, ""):
            return TemporalBase.CURRENT_TIME
        normalized = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return TemporalBase(normalized)
        except ValueError as exc:
            raise ValueError(f"Unknown temporal_base: {raw}") from exc

    @staticmethod
    def _existing_schedule_time(values: Mapping[str, Any]) -> datetime | None:
        raw = values.get("existing_schedule_run_at")
        nested = values.get("temporal_constraint")
        if raw in (None, "") and isinstance(nested, Mapping):
            raw = nested.get("existing_schedule_run_at")
        if raw in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

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
