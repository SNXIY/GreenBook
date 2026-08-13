"""LLM-backed Command Runtime interpreter.

This module owns the only user-message-to-command conversion used by the new
boundary.  Python validates model output; it does not classify messages with
language keywords or route tools from text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from greenbook_agent_core.llm_compat import (
    add_json_schema_instruction,
    has_structured_payload,
    retry_json_object,
    structured_provider_options,
)

from .models import (
    Command,
    CommandContext,
    StructuredCommandOutput,
)
from .target import TargetResolutionStatus, TargetResolver


class CommandInterpretationError(ValueError):
    """Raised when the model does not return a valid Command shape."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CommandInterpreter:
    """Convert one user message into one validated Command object."""

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        target_resolver: TargetResolver | None = None,
        capability_registry: Any | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._target_resolver = target_resolver or TargetResolver()
        self._capability_registry = capability_registry

    async def interpret(
        self,
        user_input: str,
        context: CommandContext | Any | None = None,
        *,
        llm: Any | None = None,
        model: str | None = None,
    ) -> Command:
        """Ask the model for structured output and validate it."""

        text = user_input.strip()
        if not text:
            raise CommandInterpretationError("COMMAND_INPUT_EMPTY", "Command input is empty.")

        client = llm or self._llm
        if client is None:
            raise CommandInterpretationError(
                "COMMAND_LLM_UNAVAILABLE",
                "Command Runtime requires an LLM structured-output client.",
            )
        command_context = CommandContext.from_any(context)
        capability_catalog = self._capability_catalog()
        response = await self._create_response(
            client,
            text,
            command_context,
            capability_catalog,
            model if model is not None else self._model,
        )
        payload = _response_payload(response)
        try:
            structured = StructuredCommandOutput.model_validate(payload)
        except ValidationError as exc:
            raise CommandInterpretationError(
                "COMMAND_SCHEMA_INVALID",
                "LLM output does not match the Command Runtime schema.",
            ) from exc

        command = Command(
            type=structured.command,
            goal=structured.goal or structured.objective or text,
            objective=structured.goal or structured.objective or text,
            target=structured.target,
            parameters=structured.parameters,
            entities=structured.entities,
            constraints=structured.constraints,
            semantic_operation=structured.semantic_operation,
            scope=structured.scope,
            risk=structured.risk,
            references=structured.references,
            ambiguity=structured.ambiguity,
            needs_clarification=structured.needs_clarification,
            required_capabilities=list(dict.fromkeys(structured.required_capabilities)),
            confidence=structured.confidence,
            raw_input=text,
        )
        if command.is_broad_destructive:
            command.target_resolution = "NOT_APPLICABLE"
        elif command.requires_target:
            self._resolve_target(command, command_context)
        self._validate_capabilities(command, capability_catalog)
        return command

    async def _create_response(
        self,
        client: Any,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
        model: str,
    ) -> Any:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": _COMMAND_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_input": user_input,
                            "context": context.model_dump(mode="json"),
                            "available_capabilities": capability_catalog,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "greenbook_command",
                    "strict": True,
                    "schema": StructuredCommandOutput.model_json_schema(),
                },
            },
            "temperature": 0.0,
            **structured_provider_options(client, model),
        }
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Some OpenAI-compatible gateways expose structured JSON but not
            # the json_schema response-format variant.  Keep the schema in
            # the prompt and use the provider's JSON mode as a narrow adapter.
            if "response_format" not in str(exc).lower() and "json_schema" not in str(exc).lower():
                raise
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["messages"] = add_json_schema_instruction(
                kwargs["messages"],
                StructuredCommandOutput.model_json_schema(),
            )
            response = await client.chat.completions.create(**kwargs)
        if not has_structured_payload(response):
            response = await retry_json_object(
                client,
                kwargs,
                StructuredCommandOutput.model_json_schema(),
            )
        return response

    def _capability_catalog(self) -> list[dict[str, str]]:
        registry = self._capability_registry
        if registry is None:
            return []
        list_all = getattr(registry, "list_all", None)
        values = list_all() if callable(list_all) else registry
        if values is None:
            return []
        catalog: list[dict[str, str]] = []
        for value in values:
            if isinstance(value, Mapping):
                name = str(value.get("name", "")).strip()
                description = str(value.get("description", "")).strip()
            else:
                name = str(getattr(value, "name", "")).strip()
                description = str(getattr(value, "description", "")).strip()
            if name:
                catalog.append({"name": name, "description": description})
        return catalog

    @staticmethod
    def _validate_capabilities(
        command: Command,
        capability_catalog: Sequence[Mapping[str, str]],
    ) -> None:
        if not capability_catalog or command.is_broad_destructive:
            return
        allowed = {item["name"] for item in capability_catalog}
        unknown = set(command.required_capabilities) - allowed
        if unknown:
            raise CommandInterpretationError(
                "COMMAND_CAPABILITY_UNAVAILABLE",
                "LLM output referenced capabilities outside the canonical catalog: "
                + ", ".join(sorted(unknown)),
            )
    def _resolve_target(self, command: Command, context: CommandContext) -> None:
        if command.target is None:
            command.target_resolution = TargetResolutionStatus.NOT_FOUND.value
            return
        resolution = self._target_resolver.resolve(command, context)
        command.target_resolution = resolution.status.value
        if resolution.is_resolved and resolution.target is not None:
            candidate = resolution.target
            command.resolved_target = candidate.model_dump(mode="json")
            command.target = command.target.model_copy(
                update={
                    "id": candidate.identity,
                    "task_id": candidate.task_id,
                    "resource_id": candidate.resource_id,
                }
            )


LLMCommandInterpreter = CommandInterpreter


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise CommandInterpretationError(
            "COMMAND_RESPONSE_EMPTY",
            "LLM returned no structured command content.",
        )
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise CommandInterpretationError(
            "COMMAND_RESPONSE_INVALID_JSON",
            "LLM returned invalid JSON for the Command Runtime.",
        ) from exc


_COMMAND_SYSTEM_PROMPT = """You are the GreenBook Command Runtime.

Return exactly one JSON object matching the supplied greenbook_command schema.
The command field is only the coarse operation boundary CREATE, MODIFY, CANCEL,
QUERY, or CONTROL; it is not a traditional intent taxonomy and must not be
chosen by keyword rules.  Extract the user's semantic outcome into goal,
entities, constraints, references, ambiguity, and required_capabilities.

Use the conversation history, summary, active tasks, unfinished goals, and
structured target candidates to understand follow-up turns.  When the user
refers to an existing task or artifact, emit a structured target/reference and
prefer reference_type ACTIVE only when the conversation context establishes an
active object.  A follow-up such as changing an existing task should modify the
existing target rather than silently creating a new task.  Set
needs_clarification when the request is genuinely underspecified or multiple
targets cannot be distinguished; do not guess an identity.

Use reference_type IDENTIFIER for a specific ID, ORDINAL with ordinal for an
ordered target, PROPERTY with property and value for an attribute match, and
TEMPORAL with ISO after/before bounds for a time-window match.  Do not emit MCP
tool names, execution plans, queue operations, or prose outside the JSON.  When
available_capabilities are supplied in the user payload, required_capabilities
must contain only those exact canonical names; never invent synonyms or
lowercase aliases. CONTROL is reserved for explicit runtime controls such as
approve, reject, pause, resume, retry, or cancel an existing execution. A
business request such as "立即发布这篇文章" is a CREATE or MODIFY operation
with the PUBLISH_NOW capability, not a CONTROL command.

Required capabilities must be semantic and sufficient for the requested
outcome, but must not include capabilities whose required target or evidence
is absent. In particular, do not request GET_POST_DETAIL or
ANALYZE_PERFORMANCE for a general community trend, interest, column-planning,
or promotion request unless the user explicitly asks for a concrete post,
engagement metrics, account performance, or supplies an eligible target. Do
not treat understanding community interests as a request for the user's own
performance metrics, and do not add capabilities merely because they are
present in the catalog.

When the user explicitly asks for an editorial strategy, series plan, or
content-growth direction as a distinct deliverable, include
DESIGN_CONTENT_STRATEGY; use GENERATE_CONTENT only for an actual draft. When
the user asks to write, generate, create, or save an article as a draft, use
GENERATE_CONTENT; SAVE_DRAFT is not a canonical capability. Use
SCHEDULE_PUBLISH for a future publication and PUBLISH_NOW only for an explicit
immediate publication. When the user explicitly says publication must wait for
their confirmation, record the structured constraint {"requires_approval":
true}; do not infer that constraint for an ordinary future schedule.
"""

_COMMAND_SYSTEM_PROMPT += """

For a broad destructive request such as deleting all owned posts or articles,
represent the meaning without inventing a target ID: set semantic_operation to
DELETE, scope to ALL_OWNED_POSTS (or an equivalent explicit unbounded scope),
and risk to BROAD_DESTRUCTIVE. Do not turn it into a normal target-not-found
error and do not select a delete-all capability that is not in the catalog.
"""


__all__ = [
    "CommandInterpretationError",
    "CommandInterpreter",
    "LLMCommandInterpreter",
]
