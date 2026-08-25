"""Round-1 historical residue isolation contracts.

These tests do not rewrite old Task/Execution/Operation rows.  They prove the
read/current projections exclude unresolved residue while preserving the
authoritative ledger state for a later manual boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.context import ContextBuilder, SessionContext
from greenbook_agent_core.execution.operation_ledger import is_reconciliation_exhausted
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationRecord,
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.task.models import (
    Objective,
    Task,
    TaskExecutionRef,
    TaskStatus,
)
from greenbook_agent_core.task import TaskManager
from greenbook_agent_core.task.repository import InMemoryTaskRepository
from greenbook_agent_core.task.objective_reducer import is_context_isolated_task
from greenbook_agent_core.task.provider import TaskScope
from greenbook_agent_core.command.models import TaskDelta, TaskDeltaOperation


def _task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    execution_status: str = "COMPLETED",
    active_execution_id: str | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conversation-residue",
        user_id="user-residue",
        tenant_id="tenant-residue",
        status=status,
        active_execution_id=active_execution_id,
        objectives=[
            Objective(
                task_id=task_id,
                objective_id=f"objective-{task_id}",
                description="historical objective",
                status="PENDING" if status == TaskStatus.RUNNING else "FAILED",
            )
        ],
        execution_refs=[
            TaskExecutionRef(
                execution_id=f"execution-{task_id}",
                task_id=task_id,
                status=execution_status,
            )
        ],
    )


def _exhausted_store() -> ExternalOperationStore:
    store = ExternalOperationStore()
    store.save(
        ExternalOperationRecord(
            operation_id="operation-exhausted",
            execution_id="execution-unknown",
            step_id="step-unknown",
            conversation_id="conversation-residue",
            status=OperationStatus.RESULT_UNKNOWN,
            reconciliation_needed=True,
            verified_status="VERIFIED_UNKNOWN",
            verified_reason="reconciliation budget exhausted; awaiting manual handling",
            next_reconcile_at="",
        )
    )
    return store


def test_residue_classifier_keeps_live_work_and_isolates_orphans_and_hitl() -> None:
    orphan = _task("orphan")
    approval = _task("approval", execution_status="WAITING_APPROVAL")
    live = _task(
        "live",
        execution_status="RUNNING",
        active_execution_id="execution-live",
    )

    assert is_context_isolated_task(orphan)
    assert is_context_isolated_task(approval)
    assert not is_context_isolated_task(live)


@pytest.mark.asyncio
async def test_manager_terminal_bind_cannot_persist_detached_running_task() -> None:
    manager = TaskManager(InMemoryTaskRepository())
    task = await manager.create_task(
        conversation_id="conversation-residue",
        user_id="user-residue",
        tenant_id="tenant-residue",
        root_goal="historical invariant",
    )
    task = await manager.bind_execution(task.task_id, "execution-bind", status="RUNNING")
    task = await manager.bind_execution(task.task_id, "execution-bind", status="COMPLETED")

    assert task.status == TaskStatus.READY
    assert task.active_execution_id is None


@pytest.mark.asyncio
async def test_context_and_target_projection_exclude_residue_without_rewriting_ledger() -> None:
    store = _exhausted_store()
    orphan = _task("orphan")
    unknown = _task("unknown", status=TaskStatus.FAILED, execution_status="FAILED")

    class Tasks:
        async def list_tasks(self, _scope: TaskScope) -> list[Task]:
            return [orphan, unknown]

    builder = ContextBuilder(
        task_provider=Tasks(),
        external_operation_store=store,
    )
    snapshot = await builder.build(
        conversation_id="conversation-residue",
        user_id="user-residue",
        tenant_id="tenant-residue",
        session=SessionContext(
            conversation_id="conversation-residue",
            user_id="user-residue",
            tenant_id="tenant-residue",
        ),
    )

    serialized_ids = {
        str(item.get("task_id") or "")
        for item in snapshot.active_tasks + snapshot.target_candidates
    }
    assert "orphan" not in serialized_ids
    assert "unknown" not in serialized_ids
    assert is_reconciliation_exhausted(store.get("operation-exhausted"))
    assert store.get("operation-exhausted").status == OperationStatus.RESULT_UNKNOWN

    class Manager:
        async def get_resolvable_tasks(self, **_: Any) -> list[Task]:
            return [orphan, unknown]

    adapter = ConversationRuntimeAdapter(
        task_manager=Manager(),
        external_operation_store=store,
    )
    delta = TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"id": "unknown"},
    )
    resolved = await adapter._resolve_delta_target(
        delta,
        SessionContext(
            conversation_id="conversation-residue",
            user_id="user-residue",
            tenant_id="tenant-residue",
        ),
        conversation_id="conversation-residue",
        user_id="user-residue",
        tenant_id="tenant-residue",
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_exhausted_result_unknown_is_not_queried_again() -> None:
    from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
    from greenbook_agent_core.execution.operation_ledger import OperationLedger

    store = _exhausted_store()
    calls = 0

    class ReadOnlyAdapter:
        async def reconcile(self, _operation: Any) -> OperationStatus:
            nonlocal calls
            calls += 1
            return OperationStatus.SUCCEEDED

    outcomes = await ReconciliationWorker(
        OperationLedger(store),
        adapter=ReadOnlyAdapter(),
    ).reconcile_due()

    assert outcomes == [OperationStatus.RESULT_UNKNOWN.value]
    assert calls == 0
    assert store.get("operation-exhausted").status == OperationStatus.RESULT_UNKNOWN
