"""Phase 5.0 tests for RuntimeRouter."""

from __future__ import annotations

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_router import (
    ExecutionPath,
    RuntimeRouter,
)


def _ctx(goal_category: str = "", relation: str = "") -> RuntimeContext:
    """Build a RuntimeContext with a minimal task_intent mock."""
    intent = None
    if goal_category:
        intent = type("_Intent", (), {
            "goal_category": goal_category,
            "relation": relation,
        })()
    return RuntimeContext(task_intent=intent)


# ── Scenario 1: off mode → always legacy ─────────────────────────

def test_off_mode_always_legacy() -> None:
    router = RuntimeRouter(mode="off")
    assert router.route(_ctx("CREATE_CONTENT", "NEW_TASK")) == ExecutionPath.LEGACY
    assert router.route(_ctx("IMPROVE_CONTENT", "MODIFY_TASK")) == ExecutionPath.LEGACY
    assert router.route(_ctx()) == ExecutionPath.LEGACY


# ── Scenario 2: on mode → always runtime ─────────────────────────

def test_on_mode_always_runtime() -> None:
    router = RuntimeRouter(mode="on")
    assert router.route(_ctx("CREATE_CONTENT", "NEW_TASK")) == ExecutionPath.RUNTIME
    assert router.route(_ctx("IMPROVE_CONTENT", "MODIFY_TASK")) == ExecutionPath.RUNTIME
    assert router.route(_ctx()) == ExecutionPath.RUNTIME


def test_default_mode_is_runtime() -> None:
    router = RuntimeRouter()
    assert router.mode == "on"
    assert router.route(_ctx("UNSUPPORTED", "NEW_TASK")) == ExecutionPath.RUNTIME
    assert router.route(None) == ExecutionPath.RUNTIME


# ── Scenario 3: dual is a Runtime-only compatibility alias ──────

def test_dual_create_content_goes_runtime() -> None:
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx("CREATE_CONTENT", "NEW_TASK")) == ExecutionPath.RUNTIME


def test_dual_improve_content_goes_runtime() -> None:
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx("IMPROVE_CONTENT", "MODIFY_TASK")) == ExecutionPath.RUNTIME


def test_dual_analyze_community_goes_runtime() -> None:
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx("ANALYZE_COMMUNITY", "NEW_TASK")) == ExecutionPath.RUNTIME


# ── Scenario 4: dual never implicitly selects Legacy ─────────────

def test_dual_unknown_goal_category_stays_runtime() -> None:
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx("QUERY_INFO", "DIRECT")) == ExecutionPath.RUNTIME
    assert router.route(_ctx("INTERACT", "NEW_TASK")) == ExecutionPath.RUNTIME
    assert router.route(_ctx("MANAGE_SCHEDULE", "CANCEL_TASK")) == ExecutionPath.RUNTIME


def test_dual_no_task_intent_stays_runtime() -> None:
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx()) == ExecutionPath.RUNTIME
    assert router.route(None) == ExecutionPath.RUNTIME


def test_dual_wrong_relation_stays_runtime() -> None:
    """Unsupported routing hints do not select Legacy implicitly."""
    router = RuntimeRouter(mode="dual")
    assert router.route(_ctx("CREATE_CONTENT", "CONTINUE_TASK")) == ExecutionPath.RUNTIME


# ── edge cases ────────────────────────────────────────────────────

def test_supported_scenarios_is_immutable() -> None:
    scenarios = RuntimeRouter.supported_scenarios()
    assert ("CREATE_CONTENT", "NEW_TASK") in scenarios
    assert len(scenarios) == 3


def test_mode_property() -> None:
    assert RuntimeRouter(mode="off").mode == "off"
    assert RuntimeRouter(mode="dual").mode == "dual"
    assert RuntimeRouter(mode="on").mode == "on"
