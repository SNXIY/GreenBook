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
        description="Search public posts in the GreenBook community by keywords and read the selected post details",
        category=CapabilityCategory.SEARCH,
        # Single canonical tool so single-tool auto-selection stays valid for
        # orchestrated/worker plans.  get_post remains reachable on this
        # capability via its ``serves=("SEARCH_COMMUNITY",)`` declaration, not
        # by being a second positional tool here.
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
        result_requirement="GROUNDED_SYNTHESIS",
    ),
    # ── CREATE ───────────────────────────────────────────────────
    Capability(
        name="GENERATE_CONTENT",
        description="Create a new draft post via the assistant-first generator",
        category=CapabilityCategory.CREATE,
        tools=["content.create_draft"],
        # ``content.create_draft`` consumes an instruction/brief; the host
        # LLM writes the body in one round trip, then Java persists the draft.
        inputs=CapabilityInput(
            required=["title", "instruction"],
            optional=[
                "references",
                "summary",
            ],
        ),
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
    Capability(
        name="MANAGE_DRAFT",
        description="Partially update a specific existing draft without replacing omitted fields",
        category=CapabilityCategory.CREATE,
        tools=["content.update_draft"],
        inputs=CapabilityInput(required=[], optional=["draft_id", "title", "content"]),
        output_artifact_type="DRAFT",
    ),
    Capability(
        name="DELETE_DRAFT",
        description="Soft-delete a draft after explicit user approval",
        category=CapabilityCategory.CREATE,
        tools=["content.delete_draft"],
        inputs=CapabilityInput(optional=["draft_id"]),
        output_artifact_type="",
    ),
    Capability(
        name="DELETE_POST",
        description="Delete one owned published post after explicit user approval",
        category=CapabilityCategory.PUBLISH,
        tools=["community.delete_post"],
        inputs=CapabilityInput(required=["post_id"]),
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
        result_requirement="GROUNDED_SYNTHESIS",
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
        """Map a structured requirement to one Capability without guessing.

        Legacy callers supplied only a broad ``type`` such as ``UPDATE``.
        That is no longer enough to choose a write capability: updating a
        Draft and updating a Schedule have different Java postconditions.
        Prefer the canonical semantic action; a bare ambiguous ``UPDATE``
        fails closed rather than silently selecting schedule management.
        """
        req_type = (requirement.get("type") or "").strip().upper()
        semantic_action = (
            requirement.get("semantic_action")
            or requirement.get("semantic_operation")
            or ""
        ).strip().upper()
        semantic_mapping: dict[str, str] = {
            "SEARCH_POSTS": "SEARCH_COMMUNITY",
            "GET_POST": "GET_POST_DETAIL",
            "LIST_OWN_POSTS": "LIST_OWN_POSTS",
            "CREATE_DRAFT": "GENERATE_CONTENT",
            "GET_DRAFT": "GET_DRAFT",
            "LIST_DRAFTS": "LIST_DRAFTS",
            "UPDATE_DRAFT": "MANAGE_DRAFT",
            "DELETE_DRAFT": "DELETE_DRAFT",
            "DELETE_POST": "DELETE_POST",
            "CREATE_SCHEDULE": "SCHEDULE_PUBLISH",
            "GET_SCHEDULE": "GET_SCHEDULE_STATUS",
            "UPDATE_SCHEDULE": "MANAGE_SCHEDULE",
            "CANCEL_SCHEDULE": "CANCEL_SCHEDULE",
            "PUBLISH_NOW": "PUBLISH_NOW",
        }
        if semantic_action:
            cap_name = semantic_mapping.get(semantic_action)
            if cap_name is None:
                return CapabilityMatch(
                    requirement=requirement,
                    confidence=0.0,
                    error=f"No capability for semantic action: {semantic_action}",
                )
            cap = self.get(cap_name)
            return CapabilityMatch(
                requirement=requirement,
                capability=cap,
                confidence=0.98 if cap is not None else 0.0,
                error="" if cap is not None else f"Capability '{cap_name}' not found",
            )

        resource_kind = (
            requirement.get("resource_kind")
            or requirement.get("target_kind")
            or requirement.get("kind")
            or ""
        ).strip().upper()
        mapping: dict[str, str] = {
            "SEARCH":     "SEARCH_COMMUNITY",
            "ANALYZE":    "ANALYZE_CONTENT_PATTERNS",
            "CREATE":     "GENERATE_CONTENT",
            "VALIDATE":   "VALIDATE_QUALITY",
            "PUBLISH":    "SCHEDULE_PUBLISH",
            "QUERY":      "GET_DRAFT",
            "CANCEL":     "CANCEL_SCHEDULE",
        }
        if req_type == "UPDATE":
            cap_name = {
                "DRAFT": "MANAGE_DRAFT",
                "SCHEDULE": "MANAGE_SCHEDULE",
            }.get(resource_kind)
        else:
            cap_name = mapping.get(req_type)
        if cap_name is None:
            detail = (
                "Use semantic_action or a typed resource_kind for UPDATE."
                if req_type == "UPDATE"
                else f"No capability for requirement type: {req_type}"
            )
            return CapabilityMatch(
                requirement=requirement,
                confidence=0.0,
                error=detail,
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
