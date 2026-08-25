"""LLM-backed ToolMetadata selection for AgentLoop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata, ToolRegistry
from pydantic import ValidationError

from greenbook_agent_core.execution.observation import observation_evidence
from greenbook_agent_core.goal.models import Goal
from greenbook_agent_core.llm_compat import structured_call

from .actions import SelectedTool
from .state import Observation


class ToolSelectionError(ValueError):
    """Raised when a tool cannot be selected from metadata."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_arguments_against_schema(
    tool_name: str,
    arguments: Mapping[str, Any],
    input_schema: Any,
) -> None:
    """Validate tool arguments against the tool's declared JSON Schema.

    Lightweight structural check over the JSON-Schema subset emitted by
    ``ToolMetadata``: required properties, and property type compatibility.
    This is the last validation before an in-loop (SYNC) tool call — the
    durable Worker path additionally binds against the same schema.  A
    mismatch raises ``ToolSelectionError`` so the AgentLoop re-reasons with a
    controlled failure instead of sending malformed arguments downstream.
    """
    schema = input_schema
    if schema is None:
        return
    if isinstance(schema, type) and hasattr(schema, "model_validate"):
        # Some callers carry a pydantic model class as the schema.
        try:
            schema.model_validate(dict(arguments))
            return
        except Exception as exc:
            raise ToolSelectionError(
                "TOOL_ARGUMENT_SCHEMA_INVALID",
                f"Tool '{tool_name}' arguments failed schema validation: {exc}",
            ) from exc
    if not isinstance(schema, Mapping):
        return
    if schema.get("type") not in (None, "object"):
        # Non-object schemas are not expected for tool arguments.
        return
    properties = schema.get("properties") or {}
    if not isinstance(properties, Mapping):
        return
    for required in schema.get("required") or ():
        if required not in arguments:
            raise ToolSelectionError(
                "TOOL_ARGUMENT_MISSING",
                f"Tool '{tool_name}' is missing required argument '{required}'.",
            )
    for name, value in arguments.items():
        if name not in properties:
            continue
        prop = properties[name]
        if not isinstance(prop, Mapping):
            continue
        expected = prop.get("type")
        if not expected or value is None:
            continue
        if expected == "string" and not isinstance(value, str):
            raise ToolSelectionError(
                "TOOL_ARGUMENT_TYPE_INVALID",
                f"Tool '{tool_name}' argument '{name}' must be a string.",
            )
        if expected in {"integer", "number"} and not isinstance(value, (int, float)):
            raise ToolSelectionError(
                "TOOL_ARGUMENT_TYPE_INVALID",
                f"Tool '{tool_name}' argument '{name}' must be numeric.",
            )
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolSelectionError(
                "TOOL_ARGUMENT_TYPE_INVALID",
                f"Tool '{tool_name}' argument '{name}' must be a boolean.",
            )
        if expected == "array" and not isinstance(value, (list, tuple)):
            raise ToolSelectionError(
                "TOOL_ARGUMENT_TYPE_INVALID",
                f"Tool '{tool_name}' argument '{name}' must be an array.",
            )


class ToolSelector:
    """Select one concrete tool using only the descriptive metadata catalog."""

    def __init__(self, *, llm: Any | None = None, model: str = "") -> None:
        self._llm = llm
        self._model = model

    async def select(
        self,
        goal: Goal | None,
        observation: Observation,
        tool_catalog: Sequence[ToolMetadata] | ToolRegistry | Any,
        *,
        requested_tool: str = "",
        requested_arguments: Mapping[str, Any] | None = None,
        llm: Any | None = None,
        model: str | None = None,
    ) -> SelectedTool:
        """Return a catalog-validated tool selection.

        ``requested_tool`` is only a structured AgentAction hint. It is
        validated against the catalog; Python never maps a capability to a
        positional tool or applies a business-specific name mapping.
        """

        catalog = _normalize_catalog(tool_catalog)
        if not catalog:
            raise ToolSelectionError(
                "TOOL_CATALOG_EMPTY",
                "ToolSelector requires a non-empty ToolMetadata catalog.",
            )
        by_name = {item.name: item for item in catalog}
        candidate_catalog = _candidate_catalog(goal, observation, catalog)
        if requested_tool:
            metadata = by_name.get(requested_tool)
            if metadata is None:
                raise ToolSelectionError(
                    "TOOL_NOT_IN_CATALOG",
                    f"Requested tool '{requested_tool}' is not in ToolMetadata catalog.",
                )
            # Capability consistency: the requested tool must serve the Goal's
            # CURRENT semantic step.  A model may drift across steps (observed:
            # a GENERATE_CONTENT step calling community.get_post, which
            # silently produced no draft and the Goal failed with
            # EVIDENCE_INSUFFICIENT).  Python refuses the mismatch instead of
            # executing a semantically wrong tool.  Tools without capability
            # annotations cannot be checked and stay allowed.
            declared = {str(value).upper() for value in (metadata.capabilities or ())}
            current_task = observation.current_task
            current_capability = (
                str(current_task.get("capability") or "")
                if isinstance(current_task, Mapping)
                else ""
            ).upper()
            if declared and current_capability:
                step_tools = {
                    str(item.name)
                    for item in catalog
                    if current_capability
                    in {str(value).upper() for value in (item.capabilities or ())}
                }
                if step_tools and metadata.name not in step_tools:
                    raise ToolSelectionError(
                        "TOOL_CAPABILITY_MISMATCH",
                        f"Requested tool '{requested_tool}' does not serve the "
                        f"current capability '{current_capability}' "
                        f"(allowed: {sorted(step_tools)}).",
                    )
            validate_arguments_against_schema(
                metadata.name,
                dict(requested_arguments or {}),
                metadata.input_schema,
            )
            return SelectedTool(
                tool_name=metadata.name,
                arguments=dict(requested_arguments or {}),
                reason="Validated structured tool request against metadata.",
                confidence=1.0,
                metadata=metadata,
            )

        client = llm or self._llm
        if client is None:
            raise ToolSelectionError(
                "TOOL_SELECTOR_LLM_UNAVAILABLE",
                "ToolSelector requires an LLM when no structured tool name is supplied.",
            )
        response = await self._create_response(
            client,
            goal,
            observation,
            candidate_catalog,
            model if model is not None else self._model,
        )
        payload = _response_payload(response)
        try:
            selected = SelectedTool.model_validate(payload)
        except ValidationError as exc:
            raise ToolSelectionError(
                "TOOL_SELECTION_SCHEMA_INVALID",
                "LLM output does not match SelectedTool schema.",
            ) from exc
        metadata = by_name.get(selected.tool_name)
        if metadata is None:
            raise ToolSelectionError(
                "TOOL_NOT_IN_CATALOG",
                f"LLM selected unavailable tool '{selected.tool_name}'.",
            )
        validate_arguments_against_schema(
            metadata.name,
            dict(selected.arguments or {}),
            metadata.input_schema,
        )
        selected.metadata = metadata
        return selected

    async def _create_response(
        self,
        client: Any,
        goal: Goal | None,
        observation: Observation,
        catalog: list[ToolMetadata],
        model: str,
    ) -> Any:
        request = {
            "goal": goal.model_dump(mode="json") if goal is not None else {},
            "observation": observation.model_dump(mode="json"),
            "tool_metadata": [_metadata_payload(item) for item in catalog],
            "candidate_tool_names": [item.name for item in catalog],
            "read_evidence_constraints": _read_evidence_constraints(observation, catalog),
        }
        response = await structured_call(
            client,
            model,
            _SELECTOR_PROMPT,
            "greenbook_selected_tool",
            SelectedTool.model_json_schema(),
            request,
        )
        return response


def _normalize_catalog(value: Sequence[ToolMetadata] | ToolRegistry | Any) -> list[ToolMetadata]:
    # Fast path: an already-materialized ToolMetadata list is the common
    # in-process case; avoid rebuilding it on every select call.
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(item, ToolMetadata) for item in value
    ):
        return list(value)
    if isinstance(value, ToolRegistry):
        return value.list()
    list_metadata = getattr(value, "list_tool_metadata", None)
    if callable(list_metadata):
        return list(list_metadata())
    list_method = getattr(value, "list", None)
    if callable(list_method) and not isinstance(value, (list, tuple)):
        values = list_method()
    else:
        values = value or []
    return [item if isinstance(item, ToolMetadata) else ToolMetadata.model_validate(item) for item in values]


def _metadata_payload(metadata: ToolMetadata) -> dict[str, Any]:
    payload = metadata.model_dump(mode="json")
    # ToolMetadata is intentionally the only discovery surface. Do not add
    # handlers, capabilities, or positional tool assumptions here.
    return payload


def _candidate_catalog(
    goal: Goal | None,
    observation: Observation,
    catalog: list[ToolMetadata],
) -> list[ToolMetadata]:
    """Narrow the model prompt with semantic metadata when it is reliable.

    This is a catalog projection, not a capability-to-tool routing table. If
    a host has incomplete capability annotations, the full catalog remains
    available so existing integrations do not lose a valid tool.
    """

    required: set[str] = set()
    if goal is not None:
        required.update(str(value) for value in goal.required_capabilities if value)
    current_task = observation.current_task
    if isinstance(current_task, Mapping) and current_task.get("capability"):
        required.add(str(current_task["capability"]))
    if not required:
        return catalog
    matching = [
        item
        for item in catalog
        if required.intersection(str(value) for value in item.capabilities)
    ]
    return matching or catalog


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise ToolSelectionError("TOOL_SELECTION_EMPTY", "LLM returned no tool selection.")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ToolSelectionError(
            "TOOL_SELECTION_INVALID_JSON",
            "LLM returned invalid JSON for ToolSelector.",
        ) from exc


_SELECTOR_PROMPT = """You are GreenBook's Tool Selector.

Choose exactly one tool from the supplied ToolMetadata catalog for the current
Goal and Observation. Return only the SelectedTool JSON schema. Use the
metadata name exactly. Fill arguments according to input_schema. Never emit a
tool that is not in the catalog. Do not choose by list position. Do not emit
capability names, Agent names, or execution plans. The candidate_tool_names
field is a semantic metadata projection; if it is non-empty, select from that
set. Policy is enforced by ToolPolicyGate after selection and must never be
bypassed by the model. Treat read_evidence_constraints as a hard runtime
boundary: do not select a read-only tool again for the same already-consumed
scope. An EMPTY read may be retried only with materially changed,
evidence-bounded arguments. Existing SUCCESS evidence should be consumed by
the AgentLoop rather than refreshed speculatively.
"""


def _read_evidence_constraints(
    observation: Observation,
    catalog: Sequence[ToolMetadata],
) -> dict[str, Any]:
    """Project consumed read evidence without choosing a replacement tool."""

    metadata_by_name = {str(item.name): item for item in catalog}
    consumed: list[dict[str, Any]] = []
    for result in observation.tool_results:
        tool_name = str(result.get("tool_name") or "")
        metadata = metadata_by_name.get(tool_name)
        if metadata is None:
            continue
        policy = metadata.policy
        side_effect = policy.side_effect
        if (
            policy.requires_approval
            or side_effect.has_side_effect
            or side_effect.destructive
            or str(side_effect.access_mode).upper() != "READ"
        ):
            continue
        evidence = observation_evidence(result)
        if evidence["result_status"] not in {"SUCCESS", "EMPTY"}:
            continue
        consumed.append(
            {
                "tool_name": tool_name,
                "capabilities": list(metadata.capabilities),
                "arguments": dict(result.get("tool_arguments") or {}),
                "result_status": evidence["result_status"],
                "resource_count": evidence["resource_count"],
            }
        )
    return {
        "consumed_read_evidence": consumed,
        "same_scope_read_redispatch": "FORBIDDEN",
        "empty_result_requires_material_scope_change": True,
    }


__all__ = ["ToolSelectionError", "ToolSelector"]
