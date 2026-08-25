"""CapabilityMapper — index semantic capability requirements.

Phase 3.0: mapping only — no execution.
"""

from __future__ import annotations

from .models import Capability, CapabilityMatch
from .registry import CapabilityRegistry, get_capability_registry


class CapabilityMapper:
    """Map resolved capability requirements to registry entries.

    This is a *static* mapping layer.  Phase 3.0 does NOT execute the
    capabilities — it only produces the ordered list.
    """

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        # Production Runtime callers inject the Container-owned registry.
        # Keep the legacy direct-construction path on the module singleton so
        # it cannot silently allocate another catalog.
        self._registry = registry or get_capability_registry()

    # ── main entry ───────────────────────────────────────────────

    def map_requirements(
        self,
        requirements: list[dict[str, str]],
    ) -> list[CapabilityMatch]:
        """Convert *requirements* to CapabilityMatch entries."""
        results: list[CapabilityMatch] = []
        for req in requirements:
            match = self._registry.resolve_requirement(req)
            results.append(match)
        return results

    def map_single(self, requirement_type: str) -> CapabilityMatch:
        """Map a single requirement type string to a Capability."""
        return self._registry.resolve_requirement({"type": requirement_type})

    # ── higher-level helpers ─────────────────────────────────────

    def capabilities_for_goal(
        self,
        goal_category: str,
    ) -> list[Capability]:
        """Return the default capability chain for a goal_category.

        These are the *minimum* capabilities needed to satisfy each
        goal category.  Phase 4+ Planner will expand/specialise these.
        """
        chains: dict[str, list[str]] = {
            "CREATE_CONTENT":     ["GENERATE_CONTENT"],
            "ANALYZE_COMMUNITY":  ["SEARCH_COMMUNITY"],
            "PUBLISH_CONTENT":    ["SCHEDULE_PUBLISH"],
            "MANAGE_SCHEDULE":    ["MANAGE_SCHEDULE"],
            "INTERACT":           ["REPLY_USER"],
            "QUERY_INFO":         ["GET_DRAFT"],
            "COMPOSITE":          [
                "SEARCH_COMMUNITY",
                "ANALYZE_CONTENT_PATTERNS",
                "GENERATE_CONTENT",
                "VALIDATE_QUALITY",
                "SCHEDULE_PUBLISH",
            ],
        }
        names = chains.get(goal_category, [])
        return [self._registry.get_required(n) for n in names]

    def tools_for_capability(self, capability_name: str) -> list[str]:
        """Return the MCP tool names bound to *capability_name*."""
        cap = self._registry.get(capability_name)
        if cap is None:
            return []
        return cap.tools
