"""Phase 3.3 tests for ExecutionStateManager."""

from __future__ import annotations

import pytest
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.models import (
    ArtifactHandle,
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
from greenbook_assistant_core.planning.validation import PlanValidator


@pytest.fixture(autouse=True)
def _clear_store() -> None:
    ExecutionRepository.clear()


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


@pytest.fixture
def orchestrator(registry: CapabilityRegistry) -> TaskOrchestrator:
    return TaskOrchestrator(registry)


@pytest.fixture
def validator(registry: CapabilityRegistry) -> PlanValidator:
    return PlanValidator(registry)


@pytest.fixture
def repo() -> ExecutionRepository:
    return ExecutionRepository()


@pytest.fixture
def mgr(repo: ExecutionRepository) -> ExecutionStateManager:
    return ExecutionStateManager(repo)


# ── helpers ──────────────────────────────────────────────────────

def _init_full_pipeline(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
    task_id: str = "t1",
) -> PlanExecution:
    plan = orchestrator.generate_plan(
        task_id=task_id,
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    executable = validator.validate(plan)
    assert executable.is_valid
    return mgr.init_execution(plan, executable)


# ── Scenario 1: complete plan initialises execution ──────────────

def test_full_pipeline_init(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    assert ex.status == ExecutionStatus.PENDING
    assert ex.total_step_count == 5
    assert ex.completed_step_count == 0
    assert ex.failed_step_count == 0
    for s in ex.steps:
        assert s.status == StepStatus.PENDING
    assert ex.requires_approval is False
    assert ex.has_side_effects is True


def test_step_ordinals_match_plan(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    assert [s.ordinal for s in ex.steps] == [1, 2, 3, 4, 5]


# ── Scenario 2: step 1 success, step 2 fails ────────────────────

def test_step1_success_step2_fails(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    # Step 1: succeed
    s1 = ex.steps[0]
    mgr.start_step(ex.execution_id, s1.step_execution_id)
    mgr.complete_step(
        ex.execution_id, s1.step_execution_id,
        output_artifact=ArtifactHandle(
            artifact_type="SEARCH_RESULT",
            summary="Found 20 Java posts",
        ),
    )

    # Step 2: fail (retryable)
    s2 = ex.steps[1]
    mgr.start_step(ex.execution_id, s2.step_execution_id)
    result = mgr.fail_step(
        ex.execution_id, s2.step_execution_id,
        error_code="LLM_ERROR",
        error_message="Analysis failed",
    )

    # Verify
    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.steps[0].status == StepStatus.COMPLETED
    assert ex2.steps[0].output_artifact is not None
    assert ex2.steps[0].output_artifact.artifact_type == "SEARCH_RESULT"

    assert result.status == StepStatus.FAILED_RETRYABLE
    assert result.retry_count == 1
    assert result.error_code == "LLM_ERROR"

    assert ex2.status == ExecutionStatus.RUNNING  # not FAILED yet (retryable)


def test_step_exhausts_retries_becomes_failed(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)
    s1 = ex.steps[0]
    mgr.start_step(ex.execution_id, s1.step_execution_id)

    # Fail 3 times (max_retries=3 → 3rd fail is permanent)
    for _ in range(3):
        result = mgr.fail_step(
            ex.execution_id, s1.step_execution_id,
            error_code="TOOL_ERROR",
        )
    assert result.status == StepStatus.FAILED
    assert result.retry_count == 3

    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.status == ExecutionStatus.FAILED


# ── Scenario 3: resume skips completed step ─────────────────────

def test_resume_skips_completed_steps(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    # Complete step 1
    s1 = ex.steps[0]
    mgr.start_step(ex.execution_id, s1.step_execution_id)
    mgr.complete_step(ex.execution_id, s1.step_execution_id)

    # Fail step 2 (retryable)
    s2 = ex.steps[1]
    mgr.start_step(ex.execution_id, s2.step_execution_id)
    mgr.fail_step(ex.execution_id, s2.step_execution_id,
                  error_code="CRASH")

    # Resume
    resumed = mgr.resume_execution(ex.execution_id)
    assert resumed.steps[0].status == StepStatus.COMPLETED  # skipped
    assert resumed.steps[1].status == StepStatus.PENDING     # reset for retry
    assert resumed.steps[2].status == StepStatus.PENDING
    assert resumed.status == ExecutionStatus.RUNNING


def test_resume_after_crash_resets_running_steps(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    # Start step 1 but don't complete (simulate crash)
    s1 = ex.steps[0]
    mgr.start_step(ex.execution_id, s1.step_execution_id)
    # CRASH — no complete_step call

    resumed = mgr.resume_execution(ex.execution_id)
    assert resumed.steps[0].status == StepStatus.PENDING  # reset


# ── Scenario 4: approval pause and resume ───────────────────────

def test_approval_pause(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    s3 = ex.steps[2]  # GENERATE_CONTENT
    mgr.start_step(ex.execution_id, s3.step_execution_id)
    mgr.pause_for_approval(ex.execution_id, s3.step_execution_id)

    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.steps[2].status == StepStatus.WAITING_APPROVAL
    assert ex2.status == ExecutionStatus.WAITING_APPROVAL


def test_approval_resume(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    s3 = ex.steps[2]
    mgr.start_step(ex.execution_id, s3.step_execution_id)
    mgr.pause_for_approval(ex.execution_id, s3.step_execution_id)
    mgr.approve_and_resume(ex.execution_id, s3.step_execution_id)

    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.steps[2].status == StepStatus.RUNNING
    assert ex2.status == ExecutionStatus.RUNNING


# ── edge cases ────────────────────────────────────────────────────

def test_cannot_start_non_pending_step(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)
    s1 = ex.steps[0]
    mgr.start_step(ex.execution_id, s1.step_execution_id)
    mgr.complete_step(ex.execution_id, s1.step_execution_id)

    with pytest.raises(ValueError, match="Invalid transition"):
        mgr.start_step(ex.execution_id, s1.step_execution_id)


def test_cannot_complete_non_running_step(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    s1 = ex.steps[0]
    with pytest.raises(ValueError, match="Invalid transition"):
        mgr.complete_step(ex.execution_id, s1.step_execution_id)


def test_inject_artifact_to_step(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    s2 = ex.steps[1]

    artifact = ArtifactHandle(
        artifact_type="SEARCH_RESULT",
        summary="Injected from another task",
    )
    result = mgr.inject_artifact(
        ex.execution_id, s2.step_execution_id, artifact,
    )
    assert len(result.input_artifacts) == 1
    assert result.input_artifacts[0].artifact_type == "SEARCH_RESULT"

    # Duplicate injection is idempotent
    mgr.inject_artifact(ex.execution_id, s2.step_execution_id, artifact)
    ex2 = mgr._require_execution(ex.execution_id)
    assert len(ex2.steps[1].input_artifacts) == 1


def test_cancel_execution(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)
    mgr.cancel_execution(ex.execution_id)

    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.status == ExecutionStatus.CANCELLED


def test_complete_all_steps_sets_completed(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    mgr: ExecutionStateManager,
) -> None:
    ex = _init_full_pipeline(orchestrator, validator, mgr)
    mgr.start_execution(ex.execution_id)

    for step in ex.steps:
        mgr.start_step(ex.execution_id, step.step_execution_id)
        mgr.complete_step(ex.execution_id, step.step_execution_id)

    ex2 = mgr._require_execution(ex.execution_id)
    assert ex2.status == ExecutionStatus.COMPLETED
    assert ex2.completed_at != ""
    assert ex2.completed_step_count == 5
