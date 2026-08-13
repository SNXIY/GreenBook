"""LLM-backed ToolMetadata selection for AgentLoop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata, ToolRegistry
from pydantic import ValidationError

from greenbook_agent_core.goal.models import Goal
from greenbook_agent_core.llm_compat import (
    add_json_schema_instruction,
    has_structured_payload,
    retry_json_object,
    structured_provider_options,
)

from .actions import SelectedTool
from .state import Observation


class ToolSelectionError(ValueError):
    """Raised when a tool cannot be selected from metadata."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        }
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SELECTOR_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request, ensure_ascii=False, default=str),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "greenbook_selected_tool",
                    "strict": True,
                    "schema": SelectedTool.model_json_schema(),
                },
            },
            "temperature": 0.0,
            **structured_provider_options(client, model),
        }
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "response_format" not in str(exc).lower() and "json_schema" not in str(exc).lower():
                raise
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["messages"] = add_json_schema_instruction(
                kwargs["messages"],
                SelectedTool.model_json_schema(),
            )
            response = await client.chat.completions.create(**kwargs)
        if not has_structured_payload(response):
            response = await retry_json_object(
                client,
                kwargs,
                SelectedTool.model_json_schema(),
            )
        return response


def _normalize_catalog(value: Sequence[ToolMetadata] | ToolRegistry | Any) -> list[ToolMetadata]:
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
bypassed by the model.
"""


__all__ = ["ToolSelectionError", "ToolSelector"]
