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
from greenbook_agent_core.llm_compat import structured_call

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
                    "dependencies, per-goal target, temporal constraint, "
                    "publication intent, and required capabilities."
                ),
            }
        return await structured_call(
            client,
            model,
            _GOAL_SYSTEM_PROMPT,
            "greenbook_goal_tree",
            GoalTree.model_json_schema(),
            request,
        )

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
Goal. For every user-visible Goal, preserve its own semantic_operation,
target, temporal_constraint, and publication_intent. Independent child Goals
may be parallel; dependent Goals must declare dependencies. A GoalTree may
include TaskNodes when a single Goal needs multiple planner requirements.

Publication semantics are safety-critical and are never inherited from a
request-wide scalar. Use SCHEDULE_PUBLISH only when that Goal has its own
future temporal constraint, use PUBLISH_NOW only when the Command explicitly
requested immediate publication, and use publication_intent DRAFT_ONLY when
the Goal must remain a draft. A missing schedule time must remain unresolved
and fail closed; it must never be normalized to immediate publication. The
absence of a schedule is not evidence of PUBLISH_NOW.

Do not add target-specific capabilities merely because they are available in
the catalog, and never invent a resource identifier: any concrete-read step
must consume a real reference emitted by an earlier SEARCH dependency. For a
general trend or interest request, SEARCH_COMMUNITY plus an appropriate
analysis step is sufficient. But when the requested outcome is to summarize,
synthesize, compare, or extract the community's common methods, viewpoints,
writing style, or lessons, the analysis must be grounded in real post bodies:
add a concrete-read Goal (GET_POST_DETAIL, depending on the SEARCH Goal) that
reads the representative posts returned by the search — at least two posts for
a reliable synthesis — so the summary is not inferred from titles alone. An
empty search result must not be followed by a fabricated post_id. Likewise,
do not add own-post performance analysis unless the user asked for their own
posts or the context supplies that target.

Keep content generation focused: GENERATE_CONTENT produces the actual draft
from the accumulated evidence. Do not turn it into a fixed workflow; preserve
the dependencies expressed by the GoalTree.

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
