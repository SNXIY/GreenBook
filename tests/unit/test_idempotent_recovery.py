"""Ledger-backed idempotent recovery tests."""

from greenbook_agent_core.agent.recovery import IdempotentRecoveryGuard
from greenbook_agent_core.execution.models import StepExecution, StepStatus
from greenbook_agent_core.execution.runtime.invocation_context import ToolInvocationContext
from greenbook_agent_core.execution.runtime.ledger import ToolExecutionLedger


def test_completed_ledger_entry_is_replayed_after_step_projection_lag() -> None:
    ledger = ToolExecutionLedger()
    context = ToolInvocationContext.build(
        task_id="task-1",
        execution_id="execution-1",
        step_id="publish",
        capability="SCHEDULE_PUBLISH",
        tool_name="publication.schedule",
        tool_args={"run_at": "2026-08-12T10:00:00+08:00"},
    )
    ledger.record_start(context)
    ledger.record_complete(context.invocation_id, {"ok": True, "data": {"schedule_id": "s-1"}}, 1.0)
    step = StepExecution(
        step_id="publish",
        status=StepStatus.RUNNING,
        idempotency_key=context.idempotency_key,
    )

    replay = IdempotentRecoveryGuard(ledger).completed_result(step)
    assert replay is not None
    assert replay["replayed"] is True
    assert IdempotentRecoveryGuard(ledger).should_execute(step) is False


def test_completed_step_artifact_is_reused_without_ledger() -> None:
    step = StepExecution(step_id="create", status=StepStatus.COMPLETED)
    replay = IdempotentRecoveryGuard().completed_result(step)
    assert replay is not None
    assert replay["status"] == "COMPLETED"
