"""CapabilityRegistry — the canonical catalog of what the Assistant can do.

Each entry maps a semantic capability to zero or more MCP tools.
"""

from __future__ import annotations

from .models import (
    Capability,
    CapabilityCategory,
    CapabilityInput,
    CapabilityMatch,
    RiskLevel,
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
        risk_level=RiskLevel.READ,
        side_effect=False,
        parallelizable=True,
    ),
    Capability(
        name="GET_POST_DETAIL",
        description="Retrieve full details of a single post by ID",
        category=CapabilityCategory.SEARCH,
        tools=["community.get_post"],
        inputs=CapabilityInput(required=["post_id"]),
        output_artifact_type="POST_DETAIL",
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),
    Capability(
        name="LIST_OWN_POSTS",
        description="List the current user's own published posts",
        category=CapabilityCategory.SEARCH,
        tools=["community.list_own_posts"],
        inputs=CapabilityInput(optional=["page", "size"]),
        output_artifact_type="OWNED_POST_SET",
        risk_level=RiskLevel.READ,
        side_effect=False,
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
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),
    Capability(
        name="ANALYZE_PERFORMANCE",
        description="Get engagement metrics for a post or account",
        category=CapabilityCategory.ANALYZE,
        tools=["analytics.get_post_performance", "analytics.get_account_summary"],
        inputs=CapabilityInput(required=["post_id"]),
        output_artifact_type="PERFORMANCE_DATA",
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),

    # ── CREATE ───────────────────────────────────────────────────
    Capability(
        name="GENERATE_CONTENT",
        description="Create a new draft post via Creator Agent",
        category=CapabilityCategory.CREATE,
        tools=["content.create_draft"],
        inputs=CapabilityInput(required=["title", "content"],
                               optional=["references", "summary"]),
        output_artifact_type="DRAFT",
        risk_level=RiskLevel.IDEMPOTENT_WRITE,
        side_effect=True,
    ),
    Capability(
        name="IMPROVE_CONTENT",
        description="Revise an existing draft via Creator Agent",
        category=CapabilityCategory.CREATE,
        tools=["content.revise_draft"],
        inputs=CapabilityInput(required=["draft_id", "revision_instruction"],
                               optional=["title", "expected_version"]),
        output_artifact_type="DRAFT",
        risk_level=RiskLevel.IDEMPOTENT_WRITE,
        side_effect=True,
    ),
    Capability(
        name="GET_DRAFT",
        description="Retrieve a draft by ID or resolve from session",
        category=CapabilityCategory.CREATE,
        tools=["content.get_draft"],
        inputs=CapabilityInput(optional=["draft_id"]),
        output_artifact_type="",
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),
    Capability(
        name="LIST_DRAFTS",
        description="List the current user's drafts",
        category=CapabilityCategory.CREATE,
        tools=["content.list_drafts"],
        inputs=CapabilityInput(),
        output_artifact_type="",
        risk_level=RiskLevel.READ,
        side_effect=False,
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
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),

    # ── PUBLISH ──────────────────────────────────────────────────
    Capability(
        name="SCHEDULE_PUBLISH",
        description="Schedule a draft for future publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.schedule"],
        inputs=CapabilityInput(required=["run_at"], optional=["draft_id", "timezone"]),
        output_artifact_type="SCHEDULE",
        risk_level=RiskLevel.IDEMPOTENT_WRITE,
        side_effect=True,
    ),
    Capability(
        name="PUBLISH_NOW",
        description="Immediately publish a draft (requires user approval)",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.publish_now"],
        inputs=CapabilityInput(optional=["draft_id"]),
        output_artifact_type="PUBLICATION",
        risk_level=RiskLevel.DESTRUCTIVE_WRITE,
        requires_approval=True,
        side_effect=True,
    ),
    Capability(
        name="MANAGE_SCHEDULE",
        description="Update or cancel a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.update_schedule", "publication.cancel_schedule"],
        inputs=CapabilityInput(required=["schedule_id", "run_at"]),
        output_artifact_type="SCHEDULE",
        risk_level=RiskLevel.IDEMPOTENT_WRITE,
        side_effect=True,
    ),
    Capability(
        name="CANCEL_SCHEDULE",
        description="Cancel a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.cancel_schedule"],
        inputs=CapabilityInput(required=["schedule_id"]),
        output_artifact_type="",
        risk_level=RiskLevel.IDEMPOTENT_WRITE,
        side_effect=True,
    ),
    Capability(
        name="GET_SCHEDULE_STATUS",
        description="Check the status of a scheduled publication",
        category=CapabilityCategory.PUBLISH,
        tools=["publication.get_status"],
        inputs=CapabilityInput(optional=["schedule_id"]),
        output_artifact_type="",
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),

    # ── INTERACT ─────────────────────────────────────────────────
    Capability(
        name="LIST_COMMENTS",
        description="List comments on a post",
        category=CapabilityCategory.INTERACT,
        tools=["interaction.list_comments"],
        inputs=CapabilityInput(required=["post_id"], optional=["cursor", "size"]),
        output_artifact_type="",
        risk_level=RiskLevel.READ,
        side_effect=False,
    ),
    Capability(
        name="REPLY_USER",
        description="Reply to a comment on a post (requires user approval)",
        category=CapabilityCategory.INTERACT,
        tools=["interaction.send_reply"],
        inputs=CapabilityInput(required=["post_id", "parent_comment_id", "content"]),
        output_artifact_type="COMMENT",
        risk_level=RiskLevel.DESTRUCTIVE_WRITE,
        requires_approval=True,
        side_effect=True,
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
