"""LLM-backed semantic Goal decomposition.

The decomposer consumes an already interpreted Command.  It never classifies
raw user text with keywords and it never selects an MCP tool.  The only model
output accepted at this boundary is the Pydantic GoalTree schema.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from greenbook_agent_core.capability.models import Capability
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command.models import Command, CommandContext
from greenbook_agent_core.llm_compat import (
    add_json_schema_instruction,
    has_structured_payload,
    retry_json_object,
    structured_provider_options,
)

from .models import GoalTree


class GoalDecompositionError(ValueError):
    """Raised when Goal Runtime structured output cannot be accepted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GoalDecomposer:
    """Convert one canonical Command into a validated GoalTree."""

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._capability_registry = capability_registry

    async def decompose(
        self,
        command: Command,
        context_snapshot: Any | None = None,
        available_capabilities: Sequence[Capability | Mapping[str, Any] | str] | None = None,
        *,
        llm: Any | None = None,
        model: str | None = None,
    ) -> GoalTree:
        """Ask the model for a GoalTree and validate capability references."""

        if not isinstance(command, Command):
            raise GoalDecompositionError(
                "GOAL_COMMAND_INVALID",
                "GoalDecomposer requires a canonical Command object.",
            )
        client = llm or self._llm
        if client is None:
            raise GoalDecompositionError(
                "GOAL_LLM_UNAVAILABLE",
                "Goal Runtime requires an LLM structured-output client.",
            )

        context = CommandContext.from_any(context_snapshot)
        descriptors = self._capability_descriptors(available_capabilities)
        allowed = {item["name"] for item in descriptors}
        selected_model = model if model is not None else self._model

        async def create_and_validate(
            repair_missing: Sequence[str] = (),
            repair_issue: str = "",
        ) -> GoalTree:
            response = await self._create_response(
                client,
                command,
                context,
                descriptors,
                selected_model,
                repair_missing=repair_missing,
                repair_issue=repair_issue,
            )
            try:
                payload = _response_payload(response)
                candidate = GoalTree.model_validate(payload)
                candidate.validate_tree()
            except (ValidationError, ValueError) as exc:
                raise GoalDecompositionError(
                    "GOAL_SCHEMA_INVALID",
                    "LLM output does not match the GoalTree schema: "
                    + str(exc)[:1000],
                ) from exc
            if allowed:
                unknown = {
                    capability
                    for goal in candidate.all_goals()
                    for capability in goal.required_capabilities
                    if capability not in allowed
                }
                unknown.update({
                    task.capability
                    for task in candidate.task_nodes
                    if task.capability not in allowed
                })
                if unknown:
                    raise GoalDecompositionError(
                        "GOAL_CAPABILITY_UNAVAILABLE",
                        "GoalTree references capabilities outside the supplied catalog: "
                        + ", ".join(sorted(unknown)),
                    )
            return candidate

        try:
            tree = await create_and_validate()
        except GoalDecompositionError as exc:
            if exc.code != "GOAL_SCHEMA_INVALID":
                raise
            # Ask the same semantic boundary to repair its serialized
            # contract once. The repair carries validation evidence only; it
            # does not add capabilities or synthesize a workflow locally.
            tree = await create_and_validate(repair_issue=str(exc))
        requested = set(command.required_capabilities)

        def missing_capabilities(candidate: GoalTree) -> set[str]:
            produced = {
                capability
                for goal in candidate.all_goals()
                for capability in goal.required_capabilities
            }
            produced.update(task.capability for task in candidate.task_nodes)
            return requested - produced

        missing = missing_capabilities(tree)
        if missing:
            # Preserve the semantic command contract by asking the model to
            # repair only the omitted capabilities.  We do not attach them to
            # an arbitrary Goal or synthesize a fixed workflow in Python.
            tree = await create_and_validate(sorted(missing))
            missing = missing_capabilities(tree)
        if missing:
            raise GoalDecompositionError(
                "GOAL_REQUIRED_CAPABILITY_MISSING",
                "GoalTree omitted capabilities required by Command understanding: "
                + ", ".join(sorted(missing)),
            )
        return tree

    async def _create_response(
        self,
        client: Any,
        command: Command,
        context: CommandContext,
        capabilities: list[dict[str, Any]],
        model: str,
        *,
        repair_missing: Sequence[str] = (),
        repair_issue: str = "",
    ) -> Any:
        request = {
            "command": command.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "available_capabilities": capabilities,
        }
        if repair_missing:
            request["contract_repair"] = {
                "omitted_required_capabilities": list(repair_missing),
                "instruction": (
                    "The previous GoalTree omitted these capabilities. Return a "
                    "complete semantic GoalTree that preserves every one, "
                    "placing each on the appropriate Goal or TaskNode. Do not "
                    "invent target identifiers or executable tool names."
                ),
            }
        if repair_issue:
            request["contract_repair"] = {
                **dict(request.get("contract_repair") or {}),
                "validation_error": repair_issue[:1000],
                "instruction": (
                    "Return the same semantic GoalTree with the exact supplied "
                    "schema. Repair only serialization or field-shape errors; "
                    "preserve the user objective, goal decomposition, "
                    "dependencies, constraints, and required capabilities."
                ),
            }
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": _GOAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request, ensure_ascii=False, default=str),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "greenbook_goal_tree",
                    "strict": True,
                    "schema": GoalTree.model_json_schema(),
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
                GoalTree.model_json_schema(),
            )
            response = await client.chat.completions.create(**kwargs)
        if not has_structured_payload(response):
            response = await retry_json_object(
                client,
                kwargs,
                GoalTree.model_json_schema(),
            )
        return response

    def _capability_descriptors(
        self,
        available_capabilities: Sequence[Capability | Mapping[str, Any] | str] | None,
    ) -> list[dict[str, Any]]:
        values: Sequence[Capability | Mapping[str, Any] | str]
        if available_capabilities is not None:
            list_all = getattr(available_capabilities, "list_all", None)
            values = list_all() if callable(list_all) else available_capabilities
        elif self._capability_registry is not None:
            values = self._capability_registry.list_all()
        else:
            values = []
        return [_capability_descriptor(value) for value in values]


LLMGoalDecomposer = GoalDecomposer


def _capability_descriptor(value: Capability | Mapping[str, Any] | str) -> dict[str, Any]:
    """Expose semantic capability metadata without exposing tool selection."""

    if isinstance(value, Capability):
        return {
            "name": value.name,
            "description": value.description,
            "category": str(value.category),
            "tags": list(value.tags),
            "candidate_tools": list(value.tools),
            "required_inputs": list(value.inputs.required),
            "optional_inputs": list(value.inputs.optional),
            "output_artifact_type": value.output_artifact_type,
            "parallelizable": value.parallelizable,
            "llm_step": value.is_llm_step,
        }
    if isinstance(value, Mapping):
        name = str(value.get("name", "")).strip()
        raw_inputs = value.get("inputs", {})
        if isinstance(raw_inputs, Mapping):
            required_inputs = raw_inputs.get("required", [])
            optional_inputs = raw_inputs.get("optional", [])
        else:
            required_inputs = []
            optional_inputs = []
        return {
            "name": name,
            "description": str(value.get("description", "")),
            "category": str(value.get("category", "")),
            "tags": list(value.get("tags", []) or []),
            "candidate_tools": list(
                value.get("candidate_tools", value.get("tools", [])) or []
            ),
            "required_inputs": list(value.get("required_inputs", required_inputs) or []),
            "optional_inputs": list(value.get("optional_inputs", optional_inputs) or []),
            "output_artifact_type": str(value.get("output_artifact_type", "")),
            "parallelizable": bool(value.get("parallelizable", False)),
            "llm_step": bool(value.get("llm_step", value.get("is_llm_step", False))),
        }
    return {
        "name": str(value),
        "description": "",
        "category": "",
        "tags": [],
        "candidate_tools": [],
        "required_inputs": [],
        "optional_inputs": [],
        "output_artifact_type": "",
        "parallelizable": False,
        "llm_step": False,
    }


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise GoalDecompositionError(
            "GOAL_RESPONSE_EMPTY",
            "LLM returned no structured GoalTree content.",
        )
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise GoalDecompositionError(
            "GOAL_RESPONSE_INVALID_JSON",
            "LLM returned invalid JSON for the Goal Runtime.",
        ) from exc


_GOAL_SYSTEM_PROMPT = """You are the GreenBook Goal Runtime.

Convert the canonical Command into a GoalTree. Return exactly one JSON object
matching the supplied greenbook_goal_tree schema. Use one root Goal. Split a
complex objective into explicit child Goals only when the requested outcome
requires multiple capability requirements. Put dependencies between goal_ids
explicitly; do not rely on sentence order. Set required_capabilities to
semantic capability names from the supplied catalog and preserve every
required_capability requested by Command understanding. Keep entities,
constraints, references, time bounds, and expected outputs on the relevant
Goal. Independent child Goals may be parallel; dependent Goals must declare
dependencies. A GoalTree may include TaskNodes when a single Goal needs
multiple planner requirements.

Do not add target-specific capabilities merely because they are available in
the catalog. Require GET_POST_DETAIL or another concrete-resource capability
only when the Command/Context contains a target identifier or a dependency is
explicitly guaranteed to emit the required resource reference. For a general
trend or interest request, SEARCH_COMMUNITY plus an appropriate analysis step
is sufficient; an empty search result must not be followed by a fabricated
post_id. Likewise, do not add own-post performance analysis unless the user
asked for their own posts or the context supplies that target.

Keep strategy design separate from draft generation: when the requested result
contains a distinct editorial strategy or series plan, use the semantic
DESIGN_CONTENT_STRATEGY capability for that result and reserve
GENERATE_CONTENT for the actual draft. Do not turn either capability into a
fixed workflow; preserve the dependencies expressed by the GoalTree.

Do not emit MCP tool names, Agent names, execution steps, queue operations, or
prose. Candidate tool names are catalog context only and are not executable
instructions. Do not infer a plan by matching words or fixed templates. The
Planner will compile this tree after this boundary.
"""


__all__ = [
    "GoalDecomposer",
    "GoalDecompositionError",
    "LLMGoalDecomposer",
]
