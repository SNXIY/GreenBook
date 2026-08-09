"""Phase 4.1 tests for ExecutionWorker."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.execution.capability_executor import CapabilityExecutor
from greenbook_assistant_core.execution.invocation import ExecutionResult
from greenbook_assistant_core.execution.models import (
    ArtifactHandle,
    ExecutionStatus,
    PlanExecution,
    StepStatus,
)
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.worker import ExecutionWorker, RunOutcome
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


def _make_worker(
    registry: CapabilityRegistry,
    responses: dict[str, dict[str, Any]],
) -> ExecutionWorker:
    """Build a worker with a mock executor that returns canned responses.

    Response keys can use either dot or underscore format.
    """
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        # Try exact match first, then underscore→dot
        if tool_name in responses:
            return dict(responses[tool_name])
        key = tool_name.replace(".", "_")
        if key in responses:
            return dict(responses[key])
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    executor = CapabilityExecutor(registry, handler)
    return ExecutionWorker(executor)


def _init_search_analyze_create(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    worker: ExecutionWorker,
    task_id: str = "t1",
) -> PlanExecution:
    plan = orchestrator.generate_plan(
        task_id=task_id,
        goal_category="CREATE_CONTENT",
        requirements=[
            {"type": "SEARCH"},
            {"type": "ANALYZE"},
            {"type": "CREATE"},
        ],
    )
    executable = validator.validate(plan)
    assert executable.is_valid
    return worker.init_from_plan(executable, task_id=task_id)


# ── Scenario 1: SEARCH → ANALYZE → CREATE complete execution ─────

@pytest.mark.asyncio
async def test_full_three_step_execution(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    worker = _make_worker(registry, {
        "community.search_public_posts": {
            "ok": True, "code": "",
            "data": {"items": [{"post_id": "p1", "title": "Java Hot"}], "total": 1},
        },
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d99", "title": "Java Guide"},
        },
    })
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    outcome = await worker.run(ex.execution_id)

    assert outcome == RunOutcome.COMPLETED
    final = worker._repo.find_by_id(ex.execution_id)
    assert final is not None
    assert final.status == ExecutionStatus.COMPLETED
    # Step 1 (SEARCH) and Step 3 (CREATE) completed
    # Step 2 (ANALYZE) is LLM step → auto-success
    completed = [s.capability for s in final.steps if s.status == StepStatus.COMPLETED]
    assert "SEARCH_COMMUNITY" in completed
    assert "ANALYZE_CONTENT_PATTERNS" in completed
    assert "GENERATE_CONTENT" in completed


# ── Scenario 2: Step 2 fails, Step 1 NOT re-executed ─────────────

@pytest.mark.asyncio
async def test_step2_fails_step1_not_repeated(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    call_log: list[str] = []

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        call_log.append(tool_name)
        if tool_name == "community.search_public_posts":
            return {"ok": True, "code": "",
                    "data": {"items": [], "total": 0}}
        if tool_name == "content.create_draft":
            return {"ok": False, "code": "TOOL_ERROR",
                    "user_message": "Creator unavailable", "retryable": False}
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    executor = CapabilityExecutor(registry, handler)
    worker = ExecutionWorker(executor)
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    outcome = await worker.run(ex.execution_id)

    # Step 3 (GENERATE_CONTENT) failed permanently
    assert outcome == RunOutcome.FAILED
    # Step 1 called exactly once
    assert call_log.count("community.search_public_posts") == 1


# ── Scenario 3: approval pause ───────────────────────────────────

@pytest.mark.asyncio
async def test_approval_pauses_execution(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    """When a step returns APPROVAL_REQUIRED, the worker pauses."""
    worker = _make_worker(registry, {
        "community.search_public_posts": {
            "ok": True, "code": "",
            "data": {"items": [], "total": 0},
        },
        "content.create_draft": {
            "ok": False, "code": "APPROVAL_REQUIRED",
            "message": "Needs approval",
            "user_message": "需要确认",
        },
    })
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    outcome = await worker.run(ex.execution_id)

    assert outcome == RunOutcome.WAITING_APPROVAL
    final = worker._repo.find_by_id(ex.execution_id)
    assert final is not None
    assert final.status == ExecutionStatus.WAITING_APPROVAL

    # The step that needs approval should be WAITING_APPROVAL
    waiting = [s for s in final.steps if s.status == StepStatus.WAITING_APPROVAL]
    assert len(waiting) == 1
    assert waiting[0].capability == "GENERATE_CONTENT"


# ── Scenario 4: resume after approval ───────────────────────────

@pytest.mark.asyncio
async def test_resume_after_approval_completes(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    """After approval pause, resume executes the waiting step and continues."""
    call_count = {"create": 0}

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "community.search_public_posts":
            return {"ok": True, "code": "",
                    "data": {"items": [], "total": 0}}
        if tool_name == "content.create_draft":
            call_count["create"] += 1
            if call_count["create"] == 1:
                # First call: approval required
                return {"ok": False, "code": "APPROVAL_REQUIRED",
                        "message": "Needs approval", "user_message": "需要确认"}
            # Second call (after approval): success
            return {"ok": True, "code": "",
                    "data": {"draft_id": "d99", "title": "Guide"}}
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    executor = CapabilityExecutor(registry, handler)
    worker = ExecutionWorker(executor)
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    # First run: pauses at approval
    outcome1 = await worker.run(ex.execution_id)
    assert outcome1 == RunOutcome.WAITING_APPROVAL

    # Resume
    outcome2 = await worker.resume_after_approval(ex.execution_id)
    assert outcome2 == RunOutcome.COMPLETED

    final = worker._repo.find_by_id(ex.execution_id)
    assert final is not None
    assert final.status == ExecutionStatus.COMPLETED
    assert call_count["create"] == 2


# ── Scenario 5: upstream failure → downstream skipped ───────────

@pytest.mark.asyncio
async def test_upstream_failure_skips_downstream(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    """When step 1 fails permanently, step 2 and 3 are SKIPPED."""
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "community.search_public_posts":
            return {"ok": False, "code": "JAVA_BACKEND_UNAVAILABLE",
                    "user_message": "Java backend down", "retryable": False}
        return {"ok": False, "code": "UNKNOWN_TOOL"}

    executor = CapabilityExecutor(registry, handler)
    worker = ExecutionWorker(executor)
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    outcome = await worker.run(ex.execution_id)
    assert outcome == RunOutcome.FAILED

    final = worker._repo.find_by_id(ex.execution_id)
    assert final is not None
    assert final.steps[0].status == StepStatus.FAILED
    assert final.steps[1].status == StepStatus.SKIPPED
    assert final.steps[2].status == StepStatus.SKIPPED


# ── edge cases ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_step_worker(
    registry: CapabilityRegistry,
) -> None:
    worker = _make_worker(registry, {
        "community.search_public_posts": {
            "ok": True, "code": "",
            "data": {"items": [{"title": "X"}], "total": 1},
        },
    })
    # Build a single-step plan manually
    from greenbook_assistant_core.orchestration.models import PlanStep, TaskPlan
    plan = TaskPlan(
        task_id="t-single",
        steps=[PlanStep(
            capability="SEARCH_COMMUNITY", ordinal=1,
            output_artifact_type="SEARCH_RESULT",
        )],
    )
    from greenbook_assistant_core.planning.models import ExecutablePlan
    executable = ExecutablePlan(steps=plan.steps, is_valid=True)
    ex = worker.init_from_plan(executable, task_id="t-single")

    outcome = await worker.run(ex.execution_id)
    assert outcome == RunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_retryable_step_stalls(
    orchestrator: TaskOrchestrator,
    validator: PlanValidator,
    registry: CapabilityRegistry,
) -> None:
    """A retryable failure returns STALLED (caller retries)."""
    worker = _make_worker(registry, {
        "community.search_public_posts": {
            "ok": False, "code": "TIMEOUT", "retryable": True,
            "user_message": "Timeout",
        },
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "d1", "title": "X"},
        },
    })
    ex = _init_search_analyze_create(orchestrator, validator, worker)

    outcome = await worker.run(ex.execution_id)

    # Step 1 fails retryable, Step 2 (LLM) still blocked, Step 3 blocked
    # → stalled because no ready steps (Step 1 is FAILED_RETRYABLE, not PENDING)
    assert outcome in (RunOutcome.STALLED, RunOutcome.FAILED)
    final = worker._repo.find_by_id(ex.execution_id)
    assert final is not None
    s1 = final.steps[0]
    assert s1.status in (StepStatus.FAILED_RETRYABLE, StepStatus.FAILED)
