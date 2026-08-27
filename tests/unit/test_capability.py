"""Phase 3.0 tests for CapabilityRegistry and CapabilityMapper."""

from __future__ import annotations

import pytest
from greenbook_agent_core.capability.mapper import CapabilityMapper
from greenbook_agent_core.capability.models import (
    CapabilityCategory,
)
from greenbook_agent_core.capability.registry import (
    CapabilityRegistry,
    get_capability_registry,
)
from greenbook_contracts.tool_contract import TOOL_POLICY_CATALOG

# ── helpers ──────────────────────────────────────────────────────

@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


@pytest.fixture
def mapper(registry: CapabilityRegistry) -> CapabilityMapper:
    return CapabilityMapper(registry)


# ── Registry: lookups ────────────────────────────────────────────

def test_registry_has_all_capabilities(registry: CapabilityRegistry) -> None:
    assert registry.capability_count >= 16
    assert registry.tool_count >= 14


def test_get_known_capability(registry: CapabilityRegistry) -> None:
    cap = registry.get("SEARCH_COMMUNITY")
    assert cap is not None
    assert cap.name == "SEARCH_COMMUNITY"
    assert cap.category == CapabilityCategory.SEARCH
    # Search-and-summarize completes by reading post details; get_post is the
    # read-only retrieval tool this capability must legally reach.
    assert cap.tools == ["community.search_public_posts"]
    # get_post stays reachable on SEARCH_COMMUNITY via its serves declaration,
    # not as a positional second tool (keeps single-tool auto-selection valid).
    assert "community.search_public_posts" in cap.tools


def test_get_unknown_returns_none(registry: CapabilityRegistry) -> None:
    assert registry.get("NONEXISTENT") is None


def test_get_required_raises_on_unknown(registry: CapabilityRegistry) -> None:
    with pytest.raises(ValueError, match="Unknown capability"):
        registry.get_required("NONEXISTENT")


def test_find_by_tool(registry: CapabilityRegistry) -> None:
    cap = registry.find_by_tool("content.create_draft")
    assert cap is not None
    assert cap.name == "GENERATE_CONTENT"


def test_find_by_tool_unknown(registry: CapabilityRegistry) -> None:
    assert registry.find_by_tool("no.such_tool") is None


def test_list_by_category(registry: CapabilityRegistry) -> None:
    search = registry.list_by_category(CapabilityCategory.SEARCH)
    assert len(search) >= 3
    names = {c.name for c in search}
    assert "SEARCH_COMMUNITY" in names

    create = registry.list_by_category(CapabilityCategory.CREATE)
    assert len(create) >= 3


def test_list_by_category_publish(registry: CapabilityRegistry) -> None:
    pub = registry.list_by_category(CapabilityCategory.PUBLISH)
    names = {c.name for c in pub}
    assert "SCHEDULE_PUBLISH" in names
    assert "PUBLISH_NOW" in names


# ── Scenario 1: CREATE_CONTENT → content.create_draft ─────────────

def test_create_content_maps_to_generate_content(mapper: CapabilityMapper) -> None:
    match = mapper.map_single("CREATE")
    assert match.capability is not None
    assert match.capability.name == "GENERATE_CONTENT"
    assert "content.create_draft" in match.capability.tools
    assert match.confidence >= 0.80


def test_generate_content_has_correct_metadata(registry: CapabilityRegistry) -> None:
    cap = registry.get_required("GENERATE_CONTENT")
    assert cap.category == CapabilityCategory.CREATE
    assert "risk_level" not in type(cap).model_fields
    assert "requires_approval" not in type(cap).model_fields
    assert "side_effect" not in type(cap).model_fields
    assert cap.output_artifact_type == "DRAFT"
    assert "title" in cap.inputs.required
    assert "instruction" in cap.inputs.required
    assert "content" not in cap.inputs.required


# ── Scenario 2: IMPROVE_CONTENT → content.revise_draft ────────────

def test_manage_schedule_maps_correctly(mapper: CapabilityMapper) -> None:
    match = mapper.map_single("PUBLISH")
    assert match.capability is not None
    assert match.capability.name == "SCHEDULE_PUBLISH"
    assert "publication.schedule" in match.capability.tools


def test_cancel_schedule_maps_correctly(mapper: CapabilityMapper) -> None:
    match = mapper.map_single("CANCEL")
    assert match.capability is not None
    assert match.capability.name == "CANCEL_SCHEDULE"
    assert "publication.cancel_schedule" in match.capability.tools


def test_semantic_update_draft_maps_to_draft_management(
    registry: CapabilityRegistry,
) -> None:
    match = registry.resolve_requirement(
        {"type": "UPDATE", "semantic_action": "UPDATE_DRAFT"}
    )

    assert match.capability is not None
    assert match.capability.name == "MANAGE_DRAFT"
    assert match.capability.tools == ["content.update_draft"]


def test_bare_update_fails_closed_instead_of_selecting_schedule(
    registry: CapabilityRegistry,
) -> None:
    match = registry.resolve_requirement({"type": "UPDATE"})

    assert match.capability is None
    assert "semantic_action" in match.error


def test_manage_schedule_requires_run_at(registry: CapabilityRegistry) -> None:
    cap = registry.get_required("SCHEDULE_PUBLISH")
    assert "run_at" in cap.inputs.required


# ── Scenario 4: unknown capability → clear error ──────────────────

def test_unknown_requirement_returns_error(mapper: CapabilityMapper) -> None:
    match = mapper.map_single("UNKNOWN_TYPE")
    assert match.capability is None
    assert match.error != ""
    assert "UNKNOWN_TYPE" in match.error
    assert match.confidence == 0.0


def test_map_requirements_handles_mixed(mapper: CapabilityMapper) -> None:
    results = mapper.map_requirements([
        {"type": "CREATE"},
        {"type": "UNKNOWN"},
        {"type": "SEARCH"},
    ])
    assert len(results) == 3
    assert results[0].capability is not None
    assert results[1].capability is None
    assert results[2].capability is not None


# ── Mapper: capabilities_for_goal ────────────────────────────────

def test_capabilities_for_create_content(mapper: CapabilityMapper) -> None:
    caps = mapper.capabilities_for_goal("CREATE_CONTENT")
    assert len(caps) == 1
    assert caps[0].name == "GENERATE_CONTENT"


def test_capabilities_for_composite(mapper: CapabilityMapper) -> None:
    caps = mapper.capabilities_for_goal("COMPOSITE")
    names = [c.name for c in caps]
    assert "SEARCH_COMMUNITY" in names
    assert "ANALYZE_CONTENT_PATTERNS" in names
    assert "GENERATE_CONTENT" in names
    assert "VALIDATE_QUALITY" in names
    assert "SCHEDULE_PUBLISH" in names
    # SEARCH must be before ANALYZE
    si = names.index("SEARCH_COMMUNITY")
    ai = names.index("ANALYZE_CONTENT_PATTERNS")
    ci = names.index("GENERATE_CONTENT")
    vi = names.index("VALIDATE_QUALITY")
    pi = names.index("SCHEDULE_PUBLISH")
    assert si < ai < ci < vi < pi


# ── Mapper: tools_for_capability ─────────────────────────────────

def test_tools_for_create_content(mapper: CapabilityMapper) -> None:
    tools = mapper.tools_for_capability("GENERATE_CONTENT")
    assert "content.create_draft" in tools


def test_tools_for_llm_step(mapper: CapabilityMapper) -> None:
    tools = mapper.tools_for_capability("ANALYZE_CONTENT_PATTERNS")
    assert tools == []  # pure-LLM step


def test_tools_for_unknown_capability(mapper: CapabilityMapper) -> None:
    assert mapper.tools_for_capability("NONEXISTENT") == []


# ── LLM-step capabilities ────────────────────────────────────────

def test_analyze_patterns_is_llm_step(registry: CapabilityRegistry) -> None:
    cap = registry.get_required("ANALYZE_CONTENT_PATTERNS")
    assert cap.is_llm_step is True
    assert cap.tools == []
    assert "side_effect" not in type(cap).model_fields
    assert cap.output_artifact_type == "ANALYSIS_REPORT"


def test_validate_quality_is_llm_step(registry: CapabilityRegistry) -> None:
    cap = registry.get_required("VALIDATE_QUALITY")
    assert cap.is_llm_step is True
    assert cap.tools == []
    assert cap.output_artifact_type == "VALIDATION_REPORT"


# ── Risk levels ──────────────────────────────────────────────────

def test_publish_now_requires_approval(registry: CapabilityRegistry) -> None:
    assert TOOL_POLICY_CATALOG["publication.publish_now"].requires_approval is True
    assert TOOL_POLICY_CATALOG["publication.publish_now"].risk_level == "DESTRUCTIVE_WRITE"


def test_reply_user_requires_approval(registry: CapabilityRegistry) -> None:
    assert TOOL_POLICY_CATALOG["interaction.send_reply"].requires_approval is True
    assert TOOL_POLICY_CATALOG["interaction.send_reply"].risk_level == "DESTRUCTIVE_WRITE"


def test_read_capability_no_side_effect(registry: CapabilityRegistry) -> None:
    cap = registry.get_required("SEARCH_COMMUNITY")
    assert "side_effect" not in type(cap).model_fields
    assert TOOL_POLICY_CATALOG["community.search_public_posts"].risk_level == "READ"


# ── Singleton ────────────────────────────────────────────────────

def test_singleton_returns_same_registry() -> None:
    r1 = get_capability_registry()
    r2 = get_capability_registry()
    assert r1 is r2


# ── mapper + registry reference equality ─────────────────────────

def test_capability_references_are_stable(registry: CapabilityRegistry) -> None:
    cap1 = registry.get("GENERATE_CONTENT")
    cap2 = registry.get("GENERATE_CONTENT")
    assert cap1 is cap2  # same object reference


# ── per-business-item temporal binding ──────────────────────────────────


def test_objectives_from_items_three_targets_three_times() -> None:
    import datetime
    from greenbook_agent_core.command.models import CommandItem
    from greenbook_agent_core.task.objective_compat import objectives_from_items

    now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
    items = [
        CommandItem(title="Java集合", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="明天上午八点"),
        CommandItem(title="JVM", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="明天下午两点"),
        CommandItem(title="Spring Boot", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="明天下午五点"),
    ]
    objs = objectives_from_items(items, "t1", timezone="Asia/Shanghai", now=now)
    assert len(objs) == 3, "exactly one Objective per business item (not per capability)"
    assert [o.constraints.get("run_at") for o in objs] == [
        "2026-08-17T00:00:00Z", "2026-08-17T06:00:00Z", "2026-08-17T09:00:00Z",
    ]
    assert all(o.constraints.get("timezone") == "Asia/Shanghai" for o in objs)


def test_objectives_from_items_partial_temporal_no_inherit() -> None:
    import datetime
    from greenbook_agent_core.command.models import CommandItem
    from greenbook_agent_core.task.objective_compat import objectives_from_items

    now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=datetime.timezone.utc)
    items = [
        CommandItem(title="Java", capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"], temporal_text="明天上午十点"),
        CommandItem(title="Agent草稿", capabilities=["GENERATE_CONTENT"]),
    ]
    objs = objectives_from_items(items, "t1", timezone="Asia/Shanghai", now=now)
    assert objs[0].constraints.get("run_at") == "2026-08-17T02:00:00Z"
    assert "run_at" not in objs[1].constraints, "item without temporal must not inherit"


def test_objectives_from_items_resolve_explicit_item_dependencies() -> None:
    from greenbook_agent_core.command.models import CommandItem
    from greenbook_agent_core.task.objective_compat import objectives_from_items

    items = [
        CommandItem(
            item_key="A",
            title="检索结果",
            capabilities=["SEARCH_COMMUNITY"],
        ),
        CommandItem(
            item_key="B",
            title="依赖检索的草稿",
            capabilities=["GENERATE_CONTENT"],
            dependencies=["A"],
        ),
    ]

    objectives = objectives_from_items(items, "task-dependency")

    assert len(objectives) == 2
    assert objectives[1].dependencies == [objectives[0].objective_id]
    assert objectives[1].constraints["dependency_resolution"]["status"] == "RESOLVED"


def test_objectives_from_items_fail_closed_for_unresolved_dependency_reference() -> None:
    from greenbook_agent_core.command.models import CommandItem
    from greenbook_agent_core.task.objective_compat import objectives_from_items

    objectives = objectives_from_items([
        CommandItem(item_key="A", title="检索", capabilities=["SEARCH_COMMUNITY"]),
        CommandItem(
            item_key="B",
            title="草稿",
            capabilities=["GENERATE_CONTENT"],
            dependencies=["missing-item"],
        ),
    ], "task-unresolved-dependency")

    assert objectives[1].dependencies == []
    assert objectives[1].constraints["dependency_resolution"] == {
        "status": "UNRESOLVED",
        "references": ["missing-item"],
    }
