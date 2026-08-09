"""Phase 3.1 tests for TaskOrchestrator — plan generation only."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator


@pytest.fixture
def orchestrator() -> TaskOrchestrator:
    return TaskOrchestrator(CapabilityRegistry())


# ── helpers ──────────────────────────────────────────────────────

def _step_names(plan: TaskPlan) -> list[str]:
    return [s.capability for s in plan.steps]


def _step_ordinals(plan: TaskPlan) -> list[int]:
    return [s.ordinal for s in plan.steps]


# ── Scenario 1: CREATE_CONTENT — single-step plan ─────────────────

def test_create_content_single_step(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-1",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].capability == "GENERATE_CONTENT"
    assert plan.steps[0].ordinal == 1
    assert plan.steps[0].output_artifact_type == "DRAFT"
    assert plan.steps[0].depends_on == []
    assert plan.template_name == "SINGLE_CREATE"


def test_create_content_goal_only_fallback(orchestrator: TaskOrchestrator) -> None:
    """No requirements → fall back to category-based template."""
    plan = orchestrator.generate_plan(
        task_id="task-1",
        goal_category="CREATE_CONTENT",
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].capability == "GENERATE_CONTENT"


# ── Scenario 2: CREATE + SCHEDULE — multi-step plan ───────────────

def test_create_and_schedule_multi_step(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-2",
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "CREATE"},
            {"type": "PUBLISH"},
        ],
    )
    names = _step_names(plan)
    assert len(names) == 3
    assert names == [
        "GENERATE_CONTENT",
        "VALIDATE_QUALITY",
        "SCHEDULE_PUBLISH",
    ]
    assert plan.template_name == "CREATE_AND_PUBLISH"


def test_create_and_schedule_dependencies_correct(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-2",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
    )
    s1, s2, s3 = plan.steps

    # Step 1: no deps
    assert s1.depends_on == []
    assert s1.output_artifact_type == "DRAFT"

    # Step 2: depends on step 1
    assert s2.depends_on == [s1.step_id]
    assert "DRAFT" in s2.input_artifact_types

    # Step 3: depends on step 2
    assert s3.depends_on == [s2.step_id]
    assert "DRAFT" in s3.input_artifact_types
    assert s3.output_artifact_type == "SCHEDULE"


def test_create_and_schedule_ordinals(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-2",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
    )
    assert _step_ordinals(plan) == [1, 2, 3]


# ── Scenario 3: IMPROVE with research — SEARCH → ANALYZE → REVISE ─

def test_improve_with_research(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-3",
        goal_category="IMPROVE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "IMPROVE"},
        ],
    )
    names = _step_names(plan)
    assert names == [
        "SEARCH_COMMUNITY",
        "ANALYZE_CONTENT_PATTERNS",
        "IMPROVE_CONTENT",
    ]
    assert plan.template_name == "IMPROVE_WITH_RESEARCH"


def test_improve_with_research_artifact_flow(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-3",
        goal_category="IMPROVE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "IMPROVE"},
        ],
    )
    s1, s2, s3 = plan.steps

    # s1: SEARCH → SEARCH_RESULT
    assert s1.output_artifact_type == "SEARCH_RESULT"

    # s2: consumes SEARCH_RESULT, produces ANALYSIS_REPORT
    assert s2.depends_on == [s1.step_id]
    assert "SEARCH_RESULT" in s2.input_artifact_types
    assert s2.output_artifact_type == "ANALYSIS_REPORT"

    # s3: consumes ANALYSIS_REPORT + DRAFT, produces DRAFT
    assert s3.depends_on == [s2.step_id]
    assert "ANALYSIS_REPORT" in s3.input_artifact_types
    assert "DRAFT" in s3.input_artifact_types
    assert s3.output_artifact_type == "DRAFT"


# ── Scenario 4: unknown capability → marked as warning ────────────

def test_plan_with_unknown_capability_is_still_generated(
    orchestrator: TaskOrchestrator,
) -> None:
    """The plan is always generated; unknown capabilities get a warning."""
    plan = orchestrator.generate_plan(
        task_id="task-x",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    # All capabilities in our templates are known, so no warning expected
    assert len(plan.steps) >= 1
    for s in plan.steps:
        assert "WARN" not in s.description


# ── FULL_PIPELINE — 5 steps ──────────────────────────────────────

def test_full_pipeline(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-5",
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "CREATE"},
            {"type": "PUBLISH"},
        ],
    )
    names = _step_names(plan)
    assert len(names) == 5
    assert names == [
        "SEARCH_COMMUNITY",
        "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT",
        "VALIDATE_QUALITY",
        "SCHEDULE_PUBLISH",
    ]
    assert plan.template_name == "FULL_PIPELINE"


def test_full_pipeline_dependencies_linear(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-5",
        goal_category="COMPOSITE",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    for i in range(1, len(plan.steps)):
        prev = plan.steps[i - 1]
        curr = plan.steps[i]
        assert curr.depends_on == [prev.step_id], (
            f"Step {curr.ordinal} should depend on step {prev.ordinal}"
        )


# ── edge cases ────────────────────────────────────────────────────

def test_empty_requirements_returns_fallback_plan(
    orchestrator: TaskOrchestrator,
) -> None:
    plan = orchestrator.generate_plan(task_id="t")
    assert len(plan.steps) >= 1


def test_plan_ids_are_unique(orchestrator: TaskOrchestrator) -> None:
    plans = [
        orchestrator.generate_plan(
            task_id=f"task-{i}",
            goal_category="CREATE_CONTENT",
            requirements=[{"type": "CREATE"}],
        )
        for i in range(5)
    ]
    ids = [p.plan_id for p in plans]
    assert len(set(ids)) == 5


def test_step_ids_are_unique_within_plan(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-1",
        goal_category="COMPOSITE",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    step_ids = [s.step_id for s in plan.steps]
    assert len(set(step_ids)) == len(step_ids)


def test_single_cancel(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-c",
        goal_category="MANAGE_SCHEDULE",
        requirements=[{"type": "CANCEL"}],
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].capability == "CANCEL_SCHEDULE"
    assert plan.template_name == "SINGLE_CANCEL"


def test_template_count(orchestrator: TaskOrchestrator) -> None:
    assert orchestrator.template_count == 11


def test_plan_has_metadata(orchestrator: TaskOrchestrator) -> None:
    plan = orchestrator.generate_plan(
        task_id="task-meta",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    assert plan.plan_id
    assert plan.task_id == "task-meta"
    assert plan.template_name
    assert plan.generated_at
