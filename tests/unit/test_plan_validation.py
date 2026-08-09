"""Phase 3.2 tests for PlanValidator."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


@pytest.fixture
def orchestrator(registry: CapabilityRegistry) -> TaskOrchestrator:
    return TaskOrchestrator(registry)


@pytest.fixture
def validator(registry: CapabilityRegistry) -> PlanValidator:
    return PlanValidator(registry)


# ── helpers ──────────────────────────────────────────────────────

def _make_plan(
    orchestrator: TaskOrchestrator,
    task_id: str,
    goal_category: str,
    requirements: list[dict[str, str]],
) -> TaskPlan:
    return orchestrator.generate_plan(
        task_id=task_id,
        goal_category=goal_category,
        requirements=requirements,
    )


# ── Scenario 1: valid CREATE_WITH_RESEARCH passes ─────────────────

def test_valid_create_with_research_passes_all_checks(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
) -> None:
    task_plan = _make_plan(
        orchestrator,
        task_id="t1",
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "CREATE"},
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is True
    assert result.error_count == 0
    assert result.capabilities_validated is True
    assert result.tools_mapped is True
    assert result.dependencies_checked is True
    assert result.artifacts_checked is True
    assert result.cycles_checked is True
    assert result.has_side_effects is True  # GENERATE_CONTENT has side effect
    assert result.requires_approval is False
    assert result.template_name == "CREATE_WITH_RESEARCH"


def test_valid_full_pipeline_passes(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
) -> None:
    task_plan = _make_plan(
        orchestrator, task_id="t2", goal_category="COMPOSITE",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is True
    assert result.error_count == 0


# ── Scenario 2: artifact flow broken → validation fails ──────────

def test_missing_input_artifact_fails(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
) -> None:
    """ANALYZE step needs SEARCH_RESULT but upstream produces nothing."""
    task_plan = _make_plan(
        orchestrator, task_id="t3", goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "CREATE"},
        ],
    )
    # Manually break artifact flow: make ANALYZE need a type that SEARCH
    # doesn't produce (but is produced elsewhere in the plan, so it's not
    # treated as external).
    analyze_step = task_plan.steps[1]
    assert analyze_step.capability == "ANALYZE_CONTENT_PATTERNS"
    analyze_step.input_artifact_types.append("DRAFT")  # SEARCH doesn't produce DRAFT

    result = validator.validate(task_plan)
    assert result.is_valid is False
    assert result.error_count >= 1
    codes = {e.error_code for e in result.errors}
    assert "ARTIFACT_MISSING" in codes


def test_missing_dependency_fails(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
) -> None:
    """A step references a dependency that doesn't exist."""
    task_plan = _make_plan(
        orchestrator, task_id="t4", goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
    )
    # Add a fake dependency
    task_plan.steps[1].depends_on.append("nonexistent-step-id")

    result = validator.validate(task_plan)
    assert result.is_valid is False
    codes = {e.error_code for e in result.errors}
    assert "MISSING_DEPENDENCY" in codes


# ── Scenario 3: PUBLISH_NOW triggers approval ────────────────────

def test_publish_now_requires_approval_flag(validator: PlanValidator) -> None:
    """A plan with PUBLISH_NOW sets requires_approval=True."""
    task_plan = TaskPlan(
        task_id="t5",
        steps=[
            PlanStep(
                capability="GENERATE_CONTENT",
                ordinal=1,
                output_artifact_type="DRAFT",
            ),
            PlanStep(
                capability="PUBLISH_NOW",
                ordinal=2,
                depends_on=["_dep_0"],
                input_artifact_types=["DRAFT"],
            ),
        ],
    )
    # Fix dependencies
    from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
    task_plan = TaskOrchestrator._fix_dependencies(task_plan)

    result = validator.validate(task_plan)
    assert result.requires_approval is True
    # The step description should be annotated
    publish_step = next(s for s in result.steps if s.capability == "PUBLISH_NOW")
    assert "APPROVAL REQUIRED" in publish_step.description


# ── Scenario 4: unknown capability rejected ──────────────────────

def test_unknown_capability_fails(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t6",
        steps=[
            PlanStep(
                capability="NONEXISTENT_CAPABILITY",
                ordinal=1,
                description="This does not exist",
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is False
    assert result.error_count >= 1
    assert result.errors[0].error_code == "UNKNOWN_CAPABILITY"


def test_unknown_capability_also_blocks_tool_check(
    validator: PlanValidator,
) -> None:
    """Unknown cap is only reported once (UNKNOWN_CAPABILITY), not also MISSING_TOOL."""
    task_plan = TaskPlan(
        task_id="t7",
        steps=[
            PlanStep(
                capability="FAKE_CAPABILITY",
                ordinal=1,
            ),
        ],
    )
    result = validator.validate(task_plan)
    codes = {e.error_code for e in result.errors}
    assert "UNKNOWN_CAPABILITY" in codes
    assert "MISSING_TOOL" not in codes  # skipped because cap doesn't exist


# ── Scenario 5: cycle detection ──────────────────────────────────

def test_self_loop_detected(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t8",
        steps=[
            PlanStep(
                step_id="s1",
                capability="GENERATE_CONTENT",
                ordinal=1,
                depends_on=["s1"],  # self-loop
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is False
    codes = {e.error_code for e in result.errors}
    assert "CYCLIC_DEPENDENCY" in codes


def test_two_step_cycle_detected(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t9",
        steps=[
            PlanStep(
                step_id="s1",
                capability="GENERATE_CONTENT",
                ordinal=1,
                depends_on=["s2"],
                output_artifact_type="DRAFT",
            ),
            PlanStep(
                step_id="s2",
                capability="VALIDATE_QUALITY",
                ordinal=2,
                depends_on=["s1"],
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is False
    codes = {e.error_code for e in result.errors}
    assert "CYCLIC_DEPENDENCY" in codes


def test_three_step_cycle_detected(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t10",
        steps=[
            PlanStep(
                step_id="s1", capability="SEARCH_COMMUNITY",
                ordinal=1, depends_on=["s3"], output_artifact_type="SR",
            ),
            PlanStep(
                step_id="s2", capability="ANALYZE_CONTENT_PATTERNS",
                ordinal=2, depends_on=["s1"],
            ),
            PlanStep(
                step_id="s3", capability="GENERATE_CONTENT",
                ordinal=3, depends_on=["s2"],
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is False
    codes = {e.error_code for e in result.errors}
    assert "CYCLIC_DEPENDENCY" in codes


# ── edge cases ────────────────────────────────────────────────────

def test_single_step_plan_passes(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t11",
        steps=[
            PlanStep(
                capability="SEARCH_COMMUNITY",
                ordinal=1,
                output_artifact_type="SEARCH_RESULT",
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is True
    assert result.error_count == 0


def test_llm_step_without_tools_passes(validator: PlanValidator) -> None:
    """ANALYZE_CONTENT_PATTERNS is a pure-LLM step — no tools needed."""
    task_plan = TaskPlan(
        task_id="t12",
        steps=[
            PlanStep(
                capability="ANALYZE_CONTENT_PATTERNS",
                ordinal=1,
                output_artifact_type="ANALYSIS_REPORT",
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.is_valid is True


def test_valid_plan_preserves_all_step_metadata(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
) -> None:
    task_plan = _make_plan(
        orchestrator, task_id="t13", goal_category="COMPOSITE",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    result = validator.validate(task_plan)
    assert len(result.steps) == len(task_plan.steps)
    for orig, validated in zip(task_plan.steps, result.steps):
        assert validated.capability == orig.capability
        assert validated.ordinal == orig.ordinal
        assert validated.depends_on == orig.depends_on
        assert validated.output_artifact_type == orig.output_artifact_type


def test_side_effect_flag(validator: PlanValidator) -> None:
    task_plan = TaskPlan(
        task_id="t14",
        steps=[
            PlanStep(
                capability="SEARCH_COMMUNITY",
                ordinal=1,
                output_artifact_type="SEARCH_RESULT",
            ),
        ],
    )
    result = validator.validate(task_plan)
    assert result.has_side_effects is False  # SEARCH is read-only
