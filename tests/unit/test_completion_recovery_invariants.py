"""Completion/recovery invariants for the canonical Objective Task path.

These tests intentionally exercise projection boundaries rather than adding a
new state model.  Durable Execution/Operation and Observation tests cover the
remaining retry/reconcile mechanics; this file protects the cross-layer
ownership rules that used to be easy to regress.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from greenbook_agent_api.main import (
    _reconcile_agent_run_status,
    _runtime_result_opens_continuation,
)
from greenbook_agent_api.runner import (
    EVENT_RUN_COMPLETED,
    RUN_COMPLETED,
    RUN_RUNNING,
    AgentRun,
    AgentRunEventStore,
    AgentRunStore,
    AgentRunner,
)
from greenbook_agent_core.actionloop import ActionLoop
from greenbook_agent_core.task.manager import (
    TaskManager,
    TaskStateTransitionError,
)
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
    TaskStatus,
)
from greenbook_agent_core.task.objective_reducer import (
    ObjectiveStateReducer,
    all_objectives_satisfied,
)
from greenbook_agent_core.task.repository import InMemoryTaskRepository
from greenbook_agent_core.execution.action_observation import ActionObservation, ActionObservationStore
from greenbook_agent_core.execution.completion_projection import CompletionProjectionCoordinator


def _objective(task_id: str, objective_id: str) -> Objective:
    return Objective(
        task_id=task_id,
        objective_id=objective_id,
        description=objective_id,
        intent=objective_id,
        required_capabilities=["GENERATE_CONTENT"],
    )


def _task(
    *,
    objectives: list[Objective],
    execution_refs: list[TaskExecutionRef] | None = None,
    resources: list[dict[str, Any]] | None = None,
) -> Task:
    return Task(
        task_id="task-1",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        status=TaskStatus.RUNNING,
        objectives=objectives,
        execution_refs=list(execution_refs or []),
        resource_index=list(resources or []),
    )


def test_objective_projection_is_owned_and_pending_siblings_survive() -> None:
    a = _objective("task-1", "objective-a")
    b = _objective("task-1", "objective-b")
    a.related_resource_ids = ["draft-a"]
    task = _task(
        objectives=[a, b],
        execution_refs=[
            TaskExecutionRef(
                execution_id="execution-a",
                task_id="task-1",
                goal_id="objective-a",
                status="COMPLETED",
            ),
            TaskExecutionRef(
                execution_id="execution-b",
                task_id="task-1",
                goal_id="objective-b",
                status="FAILED",
            ),
        ],
        resources=[
            {
                "resource_id": "draft-a",
                "resource_kind": "DRAFT",
                "objective_id": "objective-a",
            }
        ],
    )

    ObjectiveStateReducer().reduce(task)

    assert a.status == ObjectiveStatus.COMPLETED
    assert b.status == ObjectiveStatus.FAILED
    assert all_objectives_satisfied(task) is False


def test_waiting_external_is_nonterminal_and_does_not_complete_task() -> None:
    a = _objective("task-1", "objective-a")
    b = _objective("task-1", "objective-b")
    task = _task(
        objectives=[a, b],
        execution_refs=[
            TaskExecutionRef(
                execution_id="execution-a",
                task_id="task-1",
                goal_id="objective-a",
                status="WAITING_EXTERNAL",
            )
        ],
    )

    ObjectiveStateReducer().reduce(task)

    assert a.status == ObjectiveStatus.WAITING
    assert b.status == ObjectiveStatus.PENDING
    assert all_objectives_satisfied(task) is False


def test_cancelled_approval_execution_closes_mutation_objective() -> None:
    objective = Objective(
        task_id="task-1",
        objective_id="mutation-delete",
        description="Delete Draft",
        intent="DELETE_DRAFT",
        required_capabilities=["DELETE_DRAFT"],
        status=ObjectiveStatus.PENDING,
        constraints={
            "semantic_action": "DELETE_DRAFT",
            "mutation_domain": "DRAFT",
            "mutation_expected_state": {"lifecycle": "DELETED"},
            "mutation_status": "ACTIVE",
        },
        related_resource_ids=["draft-1"],
    )
    task = _task(
        objectives=[objective],
        execution_refs=[
            TaskExecutionRef(
                execution_id="rejected-execution",
                task_id="task-1",
                goal_id="mutation-delete",
                status="CANCELLED",
            )
        ],
    )

    ObjectiveStateReducer().reduce(task)

    assert objective.status == ObjectiveStatus.CANCELLED
    assert all_objectives_satisfied(task) is False


def test_action_loop_never_returns_a_terminal_objective_as_next_work() -> None:
    a = _objective("task-1", "objective-a")
    b = _objective("task-1", "objective-b")
    a.status = ObjectiveStatus.COMPLETED
    b.status = ObjectiveStatus.COMPLETED
    task = _task(objectives=[a, b])

    assert ActionLoop._current_objective(task) is None


def test_direct_task_completion_requires_verified_objective_success() -> None:
    async def run() -> None:
        repository = InMemoryTaskRepository()
        task = _task(objectives=[_objective("task-1", "objective-a")])
        await repository.create(task)
        manager = TaskManager(repository)

        with pytest.raises(TaskStateTransitionError):
            await manager.complete_task(task.task_id)

        assert (await repository.get(task.task_id)).status != TaskStatus.COMPLETED  # type: ignore[union-attr]

    import asyncio

    asyncio.run(run())


def test_runtime_success_flag_does_not_open_unknown_or_waiting_continuation() -> None:
    assert _runtime_result_opens_continuation(
        type("Result", (), {"success": True, "status": "WAITING_EXTERNAL"})()
    ) is False
    assert _runtime_result_opens_continuation(
        type("Result", (), {"success": True, "status": "RESULT_UNKNOWN"})()
    ) is False
    assert _runtime_result_opens_continuation(
        type("Result", (), {"success": True, "status": "COMPLETED"})()
    ) is True


def test_observation_projection_is_idempotent_by_execution() -> None:
    store = ActionObservationStore()
    first = ActionObservation(
        execution_id="execution-a",
        task_id="task-1",
        status="COMPLETED",
    )
    second = first.model_copy(update={"observation_id": "different"})

    store.save(first)
    stored = store.save(second)

    assert store.count() == 1
    assert stored.observation_id == first.observation_id


def test_local_run_projection_uses_aggregate_task_status() -> None:
    coordinator = CompletionProjectionCoordinator.__new__(CompletionProjectionCoordinator)
    coordinator._run_store = {}
    projection = type(
        "Projection",
        (),
        {
            "run_id": "run-1",
            "conversation_id": "conversation-1",
            "task_status": "RUNNING",
            "status": "COMPLETED",
            "trace_id": "trace-1",
            "execution_id": "execution-a",
            "task_id": "task-1",
            "artifacts": [],
            "assistant_response": {},
            "error_code": "",
            "error_message": "",
        },
    )()
    response = type("Response", (), {"message": "child completed"})()
    result = type(
        "Result",
        (),
        {
            "plan_id": "",
            "steps": [],
            "partial_results": {},
            "events": [],
            "error_code": "",
            "error_message": "",
            "error": "",
        },
    )()

    coordinator._update_run_store(projection, response, result)

    assert coordinator._run_store["run-1"]["status"] == "RUNNING"


@pytest.fixture
def run_store() -> AgentRunStore:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = AgentRunStore(engine, create_tables=True)
    yield store
    engine.dispose()


def _run() -> AgentRun:
    return AgentRun(
        run_id="run-1",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        payload={"message": "two objectives"},
    )


def test_run_terminal_status_is_latched(run_store: AgentRunStore) -> None:
    run_store.create(_run())
    assert run_store.mark_status("run-1", RUN_COMPLETED) is True
    assert run_store.mark_status("run-1", RUN_RUNNING) is False
    assert run_store.get("run-1").status == RUN_COMPLETED  # type: ignore[union-attr]


def test_agent_runner_defers_task_execution_terminality_to_canonical_projection(
    run_store: AgentRunStore,
) -> None:
    run_store.create(_run())
    events = AgentRunEventStore()

    class Result:
        success = True
        status = "COMPLETED"
        task_id = "task-1"
        execution_id = "execution-a"
        approval_id = ""
        error_code = ""
        error_message = ""
        error = ""
        partial_results = {
            "task_ids": ["task-1"],
            "execution_ids": ["execution-a"],
        }

    async def execute(_run: AgentRun) -> Result:
        return Result()

    async def result_handler(_run: AgentRun, _result: Result) -> None:
        return None

    runner = AgentRunner(
        run_store=run_store,
        event_store=events,
        execute=execute,
        result_handler=result_handler,
    )
    claimed = run_store.claim(worker_id="worker-1", limit=1)

    import asyncio

    asyncio.run(runner._process(claimed[0]))

    persisted = run_store.get("run-1")
    assert persisted is not None
    assert persisted.status == RUN_RUNNING
    assert persisted.status != RUN_COMPLETED
    assert EVENT_RUN_COMPLETED not in {
        event.event_type for event in events.list_since("run-1")
    }


@pytest.mark.asyncio
async def test_mixed_objective_outcome_projects_run_as_partial_not_success() -> None:
    class Run:
        status = RUN_RUNNING
        version = 1
        payload = {"task_id": "task-1"}
        user_id = "user-1"
        tenant_id = "tenant-1"
        conversation_id = "conversation-1"
        created_at = "2026-08-21T00:00:00+00:00"

    class RunStore:
        def __init__(self) -> None:
            self.run = Run()
            self.statuses: list[str] = []

        def get(self, _run_id: str) -> Run:
            return self.run

        def mark_status(self, _run_id: str, status: str, **_kwargs: Any) -> bool:
            self.statuses.append(status)
            self.run.status = status
            return True

    class Queue:
        def list(self) -> list[Any]:
            return [
                type("Message", (), {"execution_id": "execution-a", "payload": {"run_id": "run-1", "task_id": "task-1"}})(),
            ]

    class Repository:
        def find_by_id(self, execution_id: str) -> Any:
            return type(
                "Execution",
                (),
                {"status": "COMPLETED" if execution_id == "execution-a" else "FAILED"},
            )()

    class Persistence:
        execution_queue = Queue()
        execution_repository = Repository()
        observation_store = None

    class TaskProvider:
        async def get_task(self, _scope: Any, _task_id: str) -> Task:
            a = _objective("task-1", "objective-a")
            b = _objective("task-1", "objective-b")
            a.status = ObjectiveStatus.COMPLETED
            b.status = ObjectiveStatus.FAILED
            return Task(
                task_id="task-1",
                conversation_id="conversation-1",
                user_id="user-1",
                tenant_id="tenant-1",
                status=TaskStatus.FAILED,
                objectives=[a, b],
                execution_refs=[
                    TaskExecutionRef(
                        execution_id="execution-b",
                        task_id="task-1",
                        goal_id="objective-b",
                        status="FAILED",
                    )
                ],
            )

    run_store_instance = RunStore()
    app = type(
        "App",
        (),
        {
            "state": type(
                "State",
                (),
                {
                    "agent_run_store": run_store_instance,
                    "runtime_persistence": Persistence(),
                    "task_provider": TaskProvider(),
                },
            )(),
        },
    )()

    await _reconcile_agent_run_status(
        app=app,
        run_id="run-1",
        result=type("Result", (), {"status": "COMPLETED", "task_id": "task-1"})(),
    )

    assert run_store_instance.statuses == ["PARTIAL_SUCCESS"]
