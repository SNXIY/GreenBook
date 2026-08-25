"""Small invariant tests for dependency blocking and long-lived UNKNOWN."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from greenbook_agent_core.actionloop import ActionDecision, ActionDecisionType, ActionLoop
from greenbook_agent_core.execution.models import PlanExecution, StepExecution, StepStatus
from greenbook_agent_core.execution.operation_ledger import (
    LONG_RECONCILE_BACKOFF_SECONDS,
    MAX_RECONCILE_ATTEMPTS,
    OperationLedger,
)
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.reconciliation_worker import ReconciliationWorker
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.execution.worker import RunOutcome
from greenbook_agent_core.goal import Goal, GoalTree, select_ready_work
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
    TaskStatus,
)


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def _record(self, task: Any, event: str, detail: Any) -> None:
        self.events.append((event, detail))

    def _record_resource(
        self,
        task: Any,
        resource_id: str,
        resource_kind: str,
        title: str = "",
        content: str = "",
        objective_id: str = "",
    ) -> None:
        task.resource_index.append({
            "resource_id": resource_id,
            "resource_kind": resource_kind,
            "objective_id": objective_id,
            "title": title,
        })


class _Decisions:
    def __init__(self, decision: ActionDecision) -> None:
        self.decision = decision
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self.decision


def _task(objectives: list[Objective]) -> Task:
    return Task(
        task_id="task-dependency",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="dependency test",
        status=TaskStatus.RUNNING,
        objectives=objectives,
        resource_index=[],
        execution_refs=[],
    )


def _objective(objective_id: str, *, status: ObjectiveStatus = ObjectiveStatus.PENDING, dependencies: list[str] | None = None) -> Objective:
    return Objective(
        objective_id=objective_id,
        task_id="task-dependency",
        description=objective_id,
        intent="CREATE_DRAFT",
        status=status,
        required_capabilities=["GENERATE_CONTENT"],
        constraints={"title": objective_id, "instruction": "write"},
        dependencies=list(dependencies or []),
    )


@pytest.mark.asyncio
async def test_failed_predecessor_blocks_downstream_before_tool_runtime() -> None:
    predecessor = _objective("search", status=ObjectiveStatus.FAILED)
    downstream = _objective("create", dependencies=["search"])
    task = _task([predecessor, downstream])
    decisions = _Decisions(ActionDecision(
        decision=ActionDecisionType.CALL_TOOL,
        semantic_action="CREATE_DRAFT",
        arguments={"objective_id": "create", "title": "create", "instruction": "write"},
    ))
    writes: list[str] = []

    async def write_submitter(**kwargs: Any) -> dict[str, Any]:
        writes.append(str(kwargs.get("semantic_action")))
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-create"}

    result = await ActionLoop(
        decision_maker=decisions,
        write_submitter=write_submitter,
        task_store=_Store(),
        max_iterations=3,
    ).run(task, request=SimpleNamespace(run_id="run-1", trace_id="trace-1"))

    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "DEPENDENCY_BLOCKED"
    assert result.observations[0].outcome == "BLOCKED_BY_DEPENDENCY"
    assert writes == []
    assert decisions.calls == 0


@pytest.mark.asyncio
async def test_failed_dependency_does_not_stop_independent_sibling() -> None:
    predecessor = _objective("search", status=ObjectiveStatus.FAILED)
    downstream = _objective("create", dependencies=["search"])
    sibling = _objective("independent")
    task = _task([predecessor, downstream, sibling])
    decisions = _Decisions(ActionDecision(
        decision=ActionDecisionType.CALL_TOOL,
        semantic_action="CREATE_DRAFT",
        arguments={"objective_id": "independent", "title": "independent", "instruction": "write"},
    ))
    writes: list[str] = []

    async def write_submitter(**kwargs: Any) -> dict[str, Any]:
        writes.append(str(kwargs.get("objective_id")))
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-independent"}

    result = await ActionLoop(
        decision_maker=decisions,
        write_submitter=write_submitter,
        task_store=_Store(),
        max_iterations=4,
    ).run(task, request=SimpleNamespace(run_id="run-2", trace_id="trace-2"))

    assert writes == ["independent"]
    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "DEPENDENCY_BLOCKED"


@pytest.mark.asyncio
async def test_retryable_predecessor_waits_without_running_downstream() -> None:
    predecessor = _objective("search")
    downstream = _objective("create", dependencies=["search"])
    task = _task([predecessor, downstream])
    task.execution_refs = [TaskExecutionRef(
        execution_id="execution-search",
        task_id=task.task_id,
        goal_id="search",
        status="FAILED_RETRYABLE",
    )]
    writes: list[str] = []

    async def write_submitter(**kwargs: Any) -> dict[str, Any]:
        writes.append(str(kwargs.get("semantic_action")))
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-create"}

    result = await ActionLoop(
        decision_maker=_Decisions(ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="CREATE_DRAFT",
            arguments={"objective_id": "create"},
        )),
        write_submitter=write_submitter,
        task_store=_Store(),
        max_iterations=2,
    ).run(task, request=SimpleNamespace(run_id="run-retryable", trace_id="trace-retryable"))

    assert result.status == "WAITING_EXTERNAL"
    assert result.error_code == "DEPENDENCY_BLOCKED"
    assert writes == []


@pytest.mark.asyncio
async def test_unknown_objective_does_not_freeze_independent_sibling_actionloop() -> None:
    unknown = _objective("unknown")
    sibling = _objective("sibling")
    task = _task([unknown, sibling])
    task.execution_refs = [TaskExecutionRef(
        execution_id="execution-unknown",
        task_id=task.task_id,
        goal_id="unknown",
        status="RESULT_UNKNOWN",
    )]
    writes: list[str] = []

    async def write_submitter(**kwargs: Any) -> dict[str, Any]:
        writes.append(str(kwargs.get("objective_id")))
        return {"ok": True, "status": "COMPLETED", "resource_id": "draft-sibling"}

    result = await ActionLoop(
        decision_maker=_Decisions(ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="CREATE_DRAFT",
            arguments={"objective_id": "sibling"},
        )),
        write_submitter=write_submitter,
        task_store=_Store(),
        max_iterations=2,
    ).run(task, request=SimpleNamespace(run_id="run-independent", trace_id="trace-independent"))

    assert writes == ["sibling"]
    assert result.status == "WAITING_EXTERNAL"


def test_result_unknown_goal_is_not_fanout_ready() -> None:
    tree = GoalTree(root=Goal(
        goal_id="root",
        children=[Goal(
            goal_id="publish",
            publication_intent="IMMEDIATE_PUBLISH",
            required_capabilities=["PUBLISH_NOW"],
        )],
    ))
    assert select_ready_work(tree, {"publish": {"status": "RESULT_UNKNOWN"}}) == []
    assert select_ready_work(tree, {"publish": {"status": "FAILED_RETRYABLE"}}) == []


def test_unknown_objective_does_not_block_independent_goal() -> None:
    tree = GoalTree(root=Goal(
        goal_id="root",
        children=[
            Goal(
                goal_id="unknown",
                publication_intent="IMMEDIATE_PUBLISH",
                required_capabilities=["PUBLISH_NOW"],
            ),
            Goal(
                goal_id="sibling",
                publication_intent="DRAFT_ONLY",
                required_capabilities=["GENERATE_CONTENT"],
            ),
        ],
    ))
    ready = select_ready_work(tree, {
        "unknown": {"status": "RESULT_UNKNOWN"},
        "sibling": {"status": "PENDING"},
    })
    assert [item.goal_id for item in ready] == ["sibling"]


@pytest.mark.asyncio
async def test_unknown_reconciliation_moves_to_low_frequency_without_write_retry() -> None:
    store = ExternalOperationStore()
    clock = datetime(2026, 8, 22, tzinfo=UTC)
    ledger = OperationLedger(store, now_factory=lambda: clock)
    operation = ledger.begin_operation(
        idempotency_key="unknown-long-lived",
        execution_id="execution-unknown",
        semantic_action="PUBLISH_NOW",
    )
    claimed = ledger.claim(operation.operation_id, owner="worker-1")
    assert claimed is not None
    unknown = ledger.mark_result_unknown(claimed)
    assert unknown is not None
    store.save(unknown.model_copy(update={
        "reconcile_attempts": MAX_RECONCILE_ATTEMPTS,
        "next_reconcile_at": "",
    }))
    physical_writes = 0

    class UnknownAdapter:
        async def reconcile(self, operation: Any) -> OperationStatus:
            return OperationStatus.UNKNOWN

        async def submit(self, **kwargs: Any) -> None:
            nonlocal physical_writes
            physical_writes += 1

    worker = ReconciliationWorker(ledger, UnknownAdapter())
    await worker.reconcile_operation(store.get(operation.operation_id))
    fresh = store.get(operation.operation_id)
    assert fresh is not None
    assert fresh.status == OperationStatus.RESULT_UNKNOWN
    assert fresh.reconciliation_needed is True
    assert datetime.fromisoformat(fresh.next_reconcile_at) >= (
        clock + timedelta(seconds=LONG_RECONCILE_BACKOFF_SECONDS)
    )
    assert physical_writes == 0


def test_waiting_human_reconciliation_projects_as_result_unknown() -> None:
    execution_id = "execution-projection-unknown"
    execution = PlanExecution(
        execution_id=execution_id,
        task_id="task-projection",
        steps=[StepExecution(
            execution_id=execution_id,
            step_id="publish:0",
            capability="PUBLISH_NOW",
            status=StepStatus.RUNNING,
            checkpoint_data={"reconciliation_required": True},
        )],
    )

    class _Repo:
        def find_by_id(self, value: str) -> PlanExecution | None:
            return execution if value == execution_id else None

    service = RuntimeAgentService()
    result = service._finish_execution(
        ctx=SimpleNamespace(
            run_id="run-projection",
            user_message="publish",
            execution_input=SimpleNamespace(steps=[]),
            trace_id="trace-projection",
        ),
        worker=SimpleNamespace(_repo=_Repo()),
        execution_id=execution_id,
        outcome=RunOutcome.WAITING_HUMAN,
        task_id="task-projection",
        t0=0.0,
        trace=None,
        collector=None,
    )

    assert result.status == "RESULT_UNKNOWN"
    assert result.error_code == "RESULT_UNKNOWN"
    assert result.partial_results == {"reconciliation_needed": True}
