"""MCP-compatible in-process tool registry.

Handlers are registered as adapters over the canonical contracts in
``greenbook_contracts``.  This service owns no second policy catalog.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, Signature, signature
from typing import Any

from greenbook_contracts.tool_contract import (
    TOOL_POLICY_CATALOG,
    ToolContract,
    ToolMetadata,
    ToolRegistry,
    semantic_action_for_tool,
)
from greenbook_contracts.tool_result import ToolResult
from pydantic import BaseModel

from .tool_schemas import (
    CancelScheduleArguments,
    CreateDraftArguments,
    DeleteDraftArguments,
    DeletePostArguments,
    GetDraftArguments,
    GetPostArguments,
    GetScheduleStatusArguments,
    ListDraftsArguments,
    ListOwnPostsArguments,
    PublishNowArguments,
    ScheduleArguments,
    SearchPublicPostsArguments,
    UpdateScheduleArguments,
    UpdateDraftArguments,
)
from .tools import community, content, publication

_TOOLS: dict[str, ToolContract] = {}
_METADATA: ToolRegistry | None = None

def _register(
    name: str,
    handler: Callable[..., Any],
    *,
    capability: str,
    operations: tuple[str, ...],
    description: str,
    category: str,
    input_schema: type[BaseModel],
    serves: tuple[str, ...] = (),
) -> None:
    try:
        policy = TOOL_POLICY_CATALOG[name]
    except KeyError as exc:
        raise RuntimeError(f"Missing canonical ToolMetadata policy for {name}") from exc
    _TOOLS[name] = ToolContract(
        name=name,
        description=description,
        handler=handler,
        category=category,
        capability=capability,
        input_schema=input_schema,
        output_schema=ToolResult,
        operations=operations,
        policy=policy,
        semantic_action=semantic_action_for_tool(name),
        serves=serves,
    )


# Community -----------------------------------------------------------------
_register(
    "community.search_public_posts",
    community.search_public_posts,
    capability="SEARCH_COMMUNITY",
    operations=("SEARCH_CONTENT",),
    description="Search public posts in the GreenBook community",
    category="community",
    input_schema=SearchPublicPostsArguments,
)
_register(
    "community.get_post",
    community.get_post,
    capability="GET_POST_DETAIL",
    operations=("QUERY_CONTENT",),
    description="Get a single public post by ID",
    category="community",
    input_schema=GetPostArguments,
    # A search-and-summarize step stays on SEARCH_COMMUNITY while reading post
    # details; get_post is read-only and legitimately completes that capability.
    serves=("SEARCH_COMMUNITY",),
)
_register(
    "community.list_own_posts",
    community.list_own_posts,
    capability="LIST_OWN_POSTS",
    operations=("QUERY_CONTENT",),
    description="List the current user's own posts",
    category="community",
    input_schema=ListOwnPostsArguments,
)

# Content -------------------------------------------------------------------
_register(
    "content.create_draft",
    content.create_draft,
    capability="GENERATE_CONTENT",
    operations=("CREATE_CONTENT",),
    description="Create a new draft via the Java Agent Facade",
    category="content",
    input_schema=CreateDraftArguments,
)
_register(
    "content.update_draft",
    content.update_draft,
    capability="MANAGE_DRAFT",
    operations=("UPDATE_CONTENT",),
    description="Partially update an existing draft through the Java Agent Facade",
    category="content",
    input_schema=UpdateDraftArguments,
)
_register(
    "content.delete_draft",
    content.delete_draft,
    capability="DELETE_DRAFT",
    operations=("DELETE_CONTENT",),
    description="Soft-delete a draft through the Java Agent Facade (requires approval)",
    category="content",
    input_schema=DeleteDraftArguments,
)
_register(
    "community.delete_post",
    community.delete_post,
    capability="DELETE_POST",
    operations=("DELETE_CONTENT",),
    description="Delete an owned published post through the Java Agent Facade (requires approval)",
    category="community",
    input_schema=DeletePostArguments,
)
_register(
    "content.get_draft",
    content.get_draft,
    capability="GET_DRAFT",
    operations=("QUERY_CONTENT",),
    description="Get a draft by ID or resolve it from the session",
    category="content",
    input_schema=GetDraftArguments,
)
_register(
    "content.list_drafts",
    content.list_drafts,
    capability="LIST_DRAFTS",
    operations=("QUERY_CONTENT",),
    description="List the current user's drafts",
    category="content",
    input_schema=ListDraftsArguments,
)

# Publication ---------------------------------------------------------------
_register(
    "publication.schedule",
    publication.schedule,
    capability="SCHEDULE_PUBLISH",
    operations=("SCHEDULE_PUBLISH",),
    description="Schedule a draft for publication",
    category="publication",
    input_schema=ScheduleArguments,
)
_register(
    "publication.get_status",
    publication.get_status,
    capability="GET_SCHEDULE_STATUS",
    operations=("QUERY_SCHEDULE",),
    description="Get the current status of a scheduled publication",
    category="publication",
    input_schema=GetScheduleStatusArguments,
)
_register(
    "publication.update_schedule",
    publication.update_schedule,
    capability="MANAGE_SCHEDULE",
    operations=("UPDATE_PUBLISH",),
    description="Update a scheduled publication's run_at time",
    category="publication",
    input_schema=UpdateScheduleArguments,
)
_register(
    "publication.cancel_schedule",
    publication.cancel_schedule,
    capability="CANCEL_SCHEDULE",
    operations=("CANCEL_PUBLISH",),
    description="Cancel a scheduled publication",
    category="publication",
    input_schema=CancelScheduleArguments,
)
_register(
    "publication.publish_now",
    publication.publish_now,
    capability="PUBLISH_NOW",
    operations=("PUBLISH_CONTENT",),
    description="Immediately publish a draft (requires approval)",
    category="publication",
    input_schema=PublishNowArguments,
)

# Interaction and analytics handlers/schemas remain in the repository as
# historical compatibility assets.  They are deliberately not registered in
# the active Agent catalog because the current product scope excludes these
# capabilities.


def get_tool(name: str) -> ToolContract:
    if name not in _TOOLS:
        raise ValueError(f"Unknown tool: {name}")
    return _TOOLS[name]


def list_tools() -> list[ToolContract]:
    return list(_TOOLS.values())


def metadata_registry() -> ToolRegistry:
    """Return the descriptive ToolRegistry without changing execution."""

    global _METADATA
    if _METADATA is None:
        _METADATA = ToolRegistry(tool.metadata for tool in _TOOLS.values())
    return _METADATA


def get_tool_metadata(name: str) -> ToolMetadata:
    return metadata_registry().get_required(name)


def list_tool_metadata() -> list[ToolMetadata]:
    return metadata_registry().list()


def tool_catalog_prompt() -> str:
    """Build a compact tool catalog for LLM function-calling context."""
    return "\n".join(
        f"- {tool.name}: {tool.description} (risk: {tool.policy.risk_level})"
        for tool in _TOOLS.values()
    )


def validate_registered_tool_contracts(*, capability_registry: Any | None = None) -> None:
    """Fail fast when a schema, handler, or policy is incomplete or drifts."""

    for definition in _TOOLS.values():
        if not definition.operations:
            raise RuntimeError(f"Tool contract {definition.name} has no operation mapping")
        if definition.semantic_action is None:
            raise RuntimeError(f"Tool contract {definition.name} has no semantic action mapping")
        model = definition.input_schema
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise RuntimeError(f"Tool contract for {definition.name} has no input schema")
        if not isinstance(definition.output_schema, type) or not issubclass(
            definition.output_schema, BaseModel
        ):
            raise RuntimeError(f"Tool contract for {definition.name} has no output schema")

        handler_signature: Signature = signature(definition.handler)
        handler_parameters = {
            name: parameter
            for name, parameter in handler_signature.parameters.items()
            if name != "ctx"
            and parameter.kind
            not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        }
        handler_fields = set(handler_parameters)
        model_fields = set(model.model_fields)
        if handler_fields != model_fields:
            raise RuntimeError(
                f"Tool contract drift for {definition.name}: "
                f"model={sorted(model_fields)} handler={sorted(handler_fields)}"
            )

        policy = definition.policy
        if policy.requires_approval and not policy.side_effect.has_side_effect:
            raise RuntimeError(
                f"Tool contract {definition.name} requires approval without a side effect"
            )
        if policy.side_effect.destructive and not policy.side_effect.has_side_effect:
            raise RuntimeError(
                f"Tool contract {definition.name} is destructive without a side effect"
            )
        if policy.side_effect.destructive and policy.side_effect.idempotent:
            raise RuntimeError(
                f"Tool contract {definition.name} cannot be both destructive and idempotent"
            )

    # Keep the dependency optional at import time for MCP-only tooling, but
    # validate the semantic Capability catalog whenever the Agent Runtime
    # package is present (which is the production composition).
    try:
        from greenbook_agent_core.capability.registry import get_capability_registry
    except ModuleNotFoundError:
        return
    (capability_registry or get_capability_registry()).validate_tool_contracts(_TOOLS)


__all__ = [
    "get_tool",
    "get_tool_metadata",
    "list_tools",
    "list_tool_metadata",
    "metadata_registry",
    "tool_catalog_prompt",
    "validate_registered_tool_contracts",
]
