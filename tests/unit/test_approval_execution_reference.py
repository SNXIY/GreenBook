"""Phase 11.5-C approval execution-reference tests."""

import pytest

from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.approval_service import ApprovalDecisionService
from greenbook_assistant_core.compatibility.history import RunExecutionAdapter
from greenbook_assistant_core.execution.events import EventType
from greenbook_assistant_core.execution.models import PlanExecution, StepExecution
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager


@pytest.mark.asyncio
async def test_runtime_approval_decision_stores_execution_reference() -> None:
    updated: list[dict] = []
    resumed: list[tuple[str, str]] = []

    async def update_status(approval_id: str, **fields):
        updated.append({"approval_id": approval_id, **fields})
        return updated[-1]

    async def resume_runtime(approval_id: str, decision: str):
        resumed.append((approval_id, decision))
        return RuntimeResult(
            status="COMPLETED", execution_path="runtime", execution_id="execution-1"
        )

    service = ApprovalDecisionService(
        update_status=update_status,
        resume_runtime=resume_runtime,
    )
    result = await service.decide(
        {"approval_id": "approval-1", "execution_id": "execution-1"},
        decision="APPROVE",
    )

    assert result["execution_id"] == "execution-1"
    assert resumed == [("approval-1", "ACCEPT")]
    assert updated == [{"approval_id": "approval-1", "status": "APPROVED"}]


@pytest.mark.asyncio
async def test_run_reference_can_resolve_the_same_runtime_approval() -> None:
    links = RunExecutionAdapter()
    links.bind_run_execution("run-1", "execution-1")
    updated: list[dict] = []

    async def update_status(approval_id: str, **fields):
        updated.append({"approval_id": approval_id, **fields})
        return updated[-1]

    async def resume_runtime(approval_id: str, decision: str):
        return RuntimeResult(status="RUNNING", execution_path="runtime", execution_id="execution-1")

    service = ApprovalDecisionService(
        update_status=update_status,
        resume_runtime=resume_runtime,
    )
    record = {
        "approval_id": "approval-2",
        "run_id": "run-1",
        "execution_id": links.resolve_execution("run-1"),
    }
    result = await service.decide(record, decision="APPROVE")

    assert result["execution_id"] == "execution-1"
    assert updated[0]["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_legacy_only_approval_keeps_run_reference() -> None:
    updated: list[dict] = []
    resumed = False

    async def update_status(approval_id: str, **fields):
        updated.append({"approval_id": approval_id, **fields})
        return updated[-1]

    async def resume_runtime(approval_id: str, decision: str):
        nonlocal resumed
        resumed = True
        return None

    service = ApprovalDecisionService(
        update_status=update_status,
        resume_runtime=resume_runtime,
    )
    result = await service.decide(
        {"approval_id": "approval-legacy", "run_id": "legacy-run", "execution_id": None},
        decision="APPROVE",
    )

    assert result["execution_id"] is None
    assert resumed is False
    assert updated == [{"approval_id": "approval-legacy", "status": "APPROVED"}]


def test_runtime_approval_event_is_canonical_execution_event() -> None:
    repository = ExecutionRepository()
    execution = PlanExecution(status="RUNNING")
    execution.steps = [
        StepExecution(
            execution_id=execution.execution_id,
            step_id="publish",
            capability="PUBLISH",
            ordinal=0,
        )
    ]
    repository.save(execution)
    state = ExecutionStateManager(repository)
    step = execution.steps[0]

    state.start_step(execution.execution_id, step.step_execution_id)
    state.pause_for_approval(execution.execution_id, step.step_execution_id)

    events = state.event_store.list_events(execution.execution_id)
    assert [event.event_type for event in events] == [EventType.APPROVAL_REQUIRED]
