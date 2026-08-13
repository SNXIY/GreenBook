"""CapabilityRegistry — the canonical catalog of what the Agent can do.

Each entry maps a semantic capability to zero or more MCP tools.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import (
    Capability,
    CapabilityCategory,
    CapabilityInput,
    CapabilityMatch,
)

# ── canonical catalog ────────────────────────────────────────────────

_CATALOG: list[Capability] = [
    # ── SEARCH ───────────────────────────────────────────────────
    Capability(
        name="SEARCH_COMMUNITY",
        description="Search public posts in the GreenBook community by keywords",
        category=CapabilityCategory.SEARCH,
        tools=["community.search_public_posts"],
        inputs=CapabilityInput(required=["query"], optional=["sort", "page", "size"]),
        output_artifact_type="SEARCH_RESULT",
        parallelizable=True,
    ),
    Capability(
        name="GET_POST_DETAIL",
        description="Retrieve full details of a single post by ID",
        category=CapabilityCategory.SEARCH,
        tools=["community.get_post"],
        inputs=CapabilityInput(required=["post_id"]),
        output_artifact_type="POST_DETAIL",
    ),
    Capability(
        name="LIST_OWN_POSTS",
        description="List the current user's own published posts",
        category=CapabilityCategory.SEARCH,
        tools=["community.list_own_posts"],
        inputs=CapabilityInput(optional=["page", "size"]),
        output_artifact_type="OWNED_POST_SET",
    ),

    # ── ANALYZE ──────────────────────────────────────────────────
    Capability(
        name="ANALYZE_CONTENT_PATTERNS",
        description="Analyze content patterns, writing styles, and trends from search results",
        category=CapabilityCategory.ANALYZE,
        tools=[],                       # pure-LLM step
        is_llm_step=True,
        inputs=CapabilityInput(required=["source_artifact"]),
        output_artifact_type="ANALYSIS_REPORT",
    ),
    Capability(
        name="ANALYZE_PERFORMANCE",
        description="Get engagement metrics for a post or account",
        category=CapabilityCategory.ANALYZE,
        tools=["analytics.get_post_performance", "analytics.get_account_summary"],
        # The capability projects two canonical read tools: post analytics
        # require ``post_id`` while account summary is scoped by the
        # authenticated user and does not.  Keep the semantic capability
        # input optional; the selected ToolMetadata schema remains the final
        # validator for post-specific calls.
        inputs=CapabilityInput(optional=["post_id"]),
        output_artifact_type="PERFORMANCE_DATA",
    ),

    # ── CREATE ───────────────────────────────────────────────────
    Capability(
        name="GENERATE_CONTENT",
        description="Create a new draft post via Creator Service",
        category=CapabilityCategory.CREATE,
        tools=["content.create_draft"],
        # ``content.create_draft`` consumes an instruction/brief.  The
        # generated document is produced by Creator and is not an input
        # supplied to the handler.
        inputs=CapabilityInput(
            required=["title", "instruction"],
            optional=[
                "references",
                "summary",
                "strategy_task_id",
                "strategy_artifact_id",
            ],
        ),
        output_artifact_type="DRAFT",
    ),
    Capability(
        name="DESIGN_CONTENT_STRATEGY",
        description=(
            "Build an evidence-aware content strategy or series plan via the "
            "Creator Service without creating a Java draft"
        ),
        category=CapabilityCategory.CREATE,
        tools=["content.build_strategy"],
        inputs=CapabilityInput(
            required=["instruction"],
            optional=["references", "constraints"],
        ),
        output_artifact_type="CONTENT_STRATEGY",
    ),
    Capability(
        name="IMPROVE_CONTENT",
        description="Revise an existing draft via Creator Service",
        category=CapabilityCategory.CREATE,
        tools=["content.revise_draft"],
        inputs=CapabilityInput(required=["draft_id", "revision_instruction"],
                               optional=[
                                   "title",
                                   "revision_scope",
                                   "expected_version",
                               ]),
        output_artifact_type="DRAFT",
    ),
    Capability(
        name="GET_DRAFT",
        description="Retrieve a draft by ID or resolve from session",
        category=CapabilityCategory.CREATE,
        tools=["content.get_draft"],
        inputs=CapabilityInput(optional=["draft_id"]),
        output_artifact_type="",
    ),
    Capability(
        name="LIST_DRAFTS",
        description="List the current user's drafts",
        category=CapabilityCategory.CREATE,
        tools=["content.list_drafts"],
        inputs=CapabilityInput(),
        output_artifact_type="",
    ),

    # ── VALIDATE ─────────────────────────────────────────────────
    Capability(
        name="VALIDATE_QUALITY",
        description="Validate content quality (title novelty, code examples, constraints)",
        category=CapabilityCategory.VALIDATE,
        tools=[],                       # pure-LLM step
        is_llm_step=True,
        inputs=CapabilityInput(required=["draft_artifact"]),
        output_artifact_type="VALIDATION_REPORT",
    ),

    # ── PUBLISH ──────────────────────────────────────────────────
    Capability(
        name="SCHEDULE_PUBLISH",
        description="Schedule a draft for future publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.schedule"],
        inputs=CapabilityInput(
            required=["run_at"],
            optional=["draft_id", "timezone", "requires_approval"],
        ),
        output_artifact_type="SCHEDULE",
    ),
    Capability(
        name="PUBLISH_NOW",
        description="Immediately publish a draft (requires user approval)",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.publish_now"],
        inputs=CapabilityInput(optional=["draft_id"]),
        output_artifact_type="PUBLICATION",
    ),
    Capability(
        name="MANAGE_SCHEDULE",
        description="Update or cancel a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.update_schedule", "publication.cancel_schedule"],
        inputs=CapabilityInput(required=["schedule_id", "run_at"]),
        output_artifact_type="SCHEDULE",
    ),
    Capability(
        name="CANCEL_SCHEDULE",
        description="Cancel a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.cancel_schedule"],
        # The active schedule can be resolved from the session when the
        # caller omits an explicit target.
        inputs=CapabilityInput(optional=["schedule_id"]),
        output_artifact_type="",
    ),
    Capability(
        name="GET_SCHEDULE_STATUS",
        description="Check the status of a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.get_status"],
        inputs=CapabilityInput(optional=["schedule_id"]),
        output_artifact_type="",
    ),

    # ── INTERACT ─────────────────────────────────────────────────
    Capability(
        name="LIST_COMMENTS",
        description="List comments on a post",
        category=CapabilityCategory.INTERACT,
        tools=["interaction.list_comments"],
        inputs=CapabilityInput(required=["post_id"], optional=["cursor", "size"]),
        output_artifact_type="",
    ),
    Capability(
        name="REPLY_USER",
        description="Reply to a comment on a post (requires user approval)",
        category=CapabilityCategory.INTERACT,
        tools=["interaction.send_reply"],
        inputs=CapabilityInput(required=["post_id", "parent_comment_id", "content"]),
        output_artifact_type="COMMENT",
    ),
]


class CapabilityRegistry:
    """Look up capabilities by name, category, or tool."""

    def __init__(self) -> None:
        self._by_name: dict[str, Capability] = {c.name: c for c in _CATALOG}
        self._by_tool: dict[str, Capability] = {}
        for c in _CATALOG:
            for t in c.tools:
                self._by_tool[t] = c

    # ── lookups ──

    def get(self, name: str) -> Capability | None:
        """Return the capability with *name*, or None."""
        return self._by_name.get(name)

    def get_required(self, name: str) -> Capability:
        """Return the capability, raising ValueError when unknown."""
        cap = self.get(name)
        if cap is None:
            raise ValueError(f"Unknown capability: {name}")
        return cap

    def list_all(self) -> list[Capability]:
        return list(_CATALOG)

    def list_by_category(self, category: CapabilityCategory) -> list[Capability]:
        return [c for c in _CATALOG if c.category == category]

    def find_by_tool(self, tool_name: str) -> Capability | None:
        """Reverse-lookup: which capability does *tool_name* belong to?"""
        return self._by_tool.get(tool_name)

    def validate_tool_contracts(self, contracts: Iterable[Any] | Mapping[str, Any]) -> None:
        """Validate the capability side of the shared ToolContract boundary.

        MCP owns handlers, while this package owns semantic capabilities. A
        contract is valid only when its declared capability contains the tool
        and a single-tool capability exposes the same input fields. Composite
        capabilities are validated against the union they intentionally
        expose (for example update/cancel schedule operations).
        """

        values = contracts.values() if isinstance(contracts, Mapping) else contracts
        for contract in values:
            tool_name = str(getattr(contract, "name", ""))
            capability_name = str(getattr(contract, "capability", ""))
            capability = self.get(capability_name)
            if capability is None:
                raise RuntimeError(
                    f"Tool contract {tool_name} references unknown capability "
                    f"{capability_name}"
                )
            if tool_name not in capability.tools:
                raise RuntimeError(
                    f"Tool contract {tool_name} is not registered by capability "
                    f"{capability_name}"
                )

            input_schema = getattr(contract, "input_schema", None)
            model_fields = set(getattr(input_schema, "model_fields", {}))
            semantic_fields = set(
                capability.inputs.required + capability.inputs.optional
            )
            if len(capability.tools) == 1 and model_fields != semantic_fields:
                raise RuntimeError(
                    f"Capability/tool input drift for {tool_name}: "
                    f"capability={sorted(semantic_fields)} "
                    f"schema={sorted(model_fields)}"
                )
            schema_required = {
                name
                for name, field in getattr(input_schema, "model_fields", {}).items()
                if field.is_required()
            }
            if len(capability.tools) == 1 and schema_required != set(
                capability.inputs.required
            ):
                raise RuntimeError(
                    f"Capability/tool requiredness drift for {tool_name}: "
                    f"capability={sorted(capability.inputs.required)} "
                    f"schema={sorted(schema_required)}"
                )
            # A composite capability may intentionally route to a tool with
            # fewer fields, but a direct single-tool contract must not omit
            # semantic required fields.
            if (
                not set(capability.inputs.required).issubset(model_fields)
                and len(capability.tools) == 1
            ):
                raise RuntimeError(
                    f"Capability {capability_name} requires fields absent from "
                    f"{tool_name}: {sorted(set(capability.inputs.required) - model_fields)}"
                )

    # ── resolution ──

    def resolve_requirement(
        self,
        requirement: dict[str, str],
    ) -> CapabilityMatch:
        """Map a requirement dict {type: "SEARCH", ...} to a Capability.

        Phase 3.0 strategy: simple type → name mapping.
        """
        req_type = (requirement.get("type") or "").strip().upper()
        mapping: dict[str, str] = {
            "SEARCH":     "SEARCH_COMMUNITY",
            "ANALYZE":    "ANALYZE_CONTENT_PATTERNS",
            "CREATE":     "GENERATE_CONTENT",
            "IMPROVE":    "IMPROVE_CONTENT",
            "VALIDATE":   "VALIDATE_QUALITY",
            "PUBLISH":    "SCHEDULE_PUBLISH",
            "REPLY":      "REPLY_USER",
            "QUERY":      "GET_DRAFT",
            "CANCEL":     "CANCEL_SCHEDULE",
            "UPDATE":     "MANAGE_SCHEDULE",
        }
        cap_name = mapping.get(req_type)
        if cap_name is None:
            return CapabilityMatch(
                requirement=requirement,
                confidence=0.0,
                error=f"No capability for requirement type: {req_type}",
            )
        cap = self.get(cap_name)
        return CapabilityMatch(
            requirement=requirement,
            capability=cap,
            confidence=0.90 if cap is not None else 0.0,
            error="" if cap is not None else f"Capability '{cap_name}' not found",
        )

    # ── catalog info ──

    @property
    def tool_count(self) -> int:
        return len(self._by_tool)

    @property
    def capability_count(self) -> int:
        return len(_CATALOG)


# ── singleton ────────────────────────────────────────────────────────

_default_registry: CapabilityRegistry | None = None


def get_capability_registry() -> CapabilityRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = CapabilityRegistry()
    return _default_registry
