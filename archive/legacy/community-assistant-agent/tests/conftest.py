"""Shared pytest hooks for community-assistant-agent."""

from __future__ import annotations

import pytest

# Canonical regression set. Counts from this marker are the only ones that
# should be compared across Phase 5 reports.
REGRESSION_MODULES: frozenset[str] = frozenset(
    {
        "test_control_plane_router",
        "test_query_agent",
        "test_task_manager",
        "test_target_resolution",
        "test_temporal_resolver",
        "test_plan_compiler",
        "test_temporal_schedule_integration",
        "test_tool_runtime_step1",
        "test_tool_runtime_step2",
        "test_tool_runtime_step3",
        "test_tool_runtime_step4",
        "test_tool_runtime_step4_1",
        "test_tool_runtime_step5",
        "test_tool_runtime_step6",
        "test_moderation_removed",
        "test_orchestration",
        "test_adaptive_execution",
        "test_harness_controls",
        "test_execution_reliability",
        "test_runtime_contracts",
        "test_runtime_smoke_phase38",
        "test_multiturn_lifecycle_matrix",
        "test_operation_contracts",
        "test_intent_delta",
        "test_intent_delta_plan_compiler",
        "test_conversation_workspace",
        "test_artifact_lifecycle",
        "test_search_retrieval",
        "test_turn_plan_matrix",
    }
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        module_name = item.module.__name__.rsplit(".", 1)[-1]
        if module_name in REGRESSION_MODULES:
            item.add_marker(pytest.mark.regression)
