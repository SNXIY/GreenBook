"""Phase 3 concurrency and state-convergence proofs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_api.runner import AgentRun, AgentRunEventStore, AgentRunner, AgentRunStore
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.execution.execution_queue import ExecutionQueue
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.goal import (
    Goal,
    GoalTree,
    WorkAccess,
    resource_conflict,
    select_ready_work,
)
from greenbook_agent_core.task.models import Task, TaskGoal, TaskStatus
from greenbook_agent_core.task.provider import TaskProvider, TaskProviderError, TaskScope


def _independent_tree(*, dependent: bool = False) -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="root",
            description="root",
            children=[
                Goal(
                    goal_id="g1",
                    description="first",
                    publication_intent="DRAFT_ONLY",
                    required_capabilities=["READ"],
                ),
                Goal(
                    goal_id="g2",
                    description="second",
                    publication_intent="DRAFT_ONLY",
                    required_capabilities=["READ"],
                    dependencies=["g1"] if dependent else [],
                ),
            ],
        )
    )


def test_ready_selector_allows_independent_goals() -> None:
    ready = select_ready_work(_independent_tree())
    assert [item.goal_id for item in ready] == ["g1", "g2"]


def test_ready_selector_blocks_unmet_dependency_but_keeps_sibling_failure_isolated() -> None:
    tree = _independent_tree(dependent=True)
    assert [item.goal_id for item in select_ready_work(tree)] == ["g1"]
    ready_after_failure = select_ready_work(
        tree,
        {"g1": {"status": "FAILED"}, "g2": {"status": "PENDING"}},
    )
    assert ready_after_failure == []

    independent = _independent_tree()
    ready_with_failed_sibling = select_ready_work(
        independent,
        {"g1": {"status": "FAILED"}, "g2": {"status": "PENDING"}},
    )
    assert [item.goal_id for item in ready_with_failed_sibling] == ["g2"]


def test_ready_selector_skips_waiting_and_inflight_work() -> None:
    tree = _independent_tree()
    ready = select_ready_work(
        tree,
        {"g1": {"status": "WAITING_APPROVAL"}},
        in_flight_goal_ids={"g2"},
    )
    assert ready == []


def test_resource_conflict_is_structured_and_conservative() -> None:
    read_a = WorkAccess(resource_keys=("draft:d1",), access_mode="READ")
    read_b = WorkAccess(resource_keys=("draft:d1",), access_mode="READ")
    write = WorkAccess(resource_keys=("draft:d1",), access_mode="WRITE")
    other = WorkAccess(resource_keys=("draft:d2",), access_mode="WRITE")
    assert resource_conflict(read_a, read_b) is False
    assert resource_conflict(write, read_a) is True
    assert resource_conflict(write, other) is False


@pytest.mark.asyncio
async def test_execution_queue_runs_independent_messages_overlapping() -> None:
    queue = ExecutionQueue()
    for execution_id in ("a", "b", "c"):
        queue.enqueue(execution_id)
    timeline: dict[str, tuple[float, float]] = {}

    async def handler(message) -> None:
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(0.03)
        timeline[message.execution_id] = (start, asyncio.get_running_loop().time())

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        batch_size=3,
        max_concurrency=3,
    )
    handled = await worker.run_once()
    assert {item.execution_id for item in handled} == {"a", "b", "c"}
    assert timeline["b"][0] < timeline["a"][1]
    assert timeline["c"][0] < timeline["a"][1]


@pytest.mark.asyncio
async def test_execution_queue_serializes_same_resource_mutations() -> None:
    queue = ExecutionQueue()
    queue.enqueue("write-a", payload={"draft_id": "d1", "access_mode": "WRITE"})
    queue.enqueue("write-b", payload={"draft_id": "d1", "access_mode": "WRITE"})
    started: list[str] = []

    async def handler(message) -> None:
        started.append(message.execution_id)

    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        batch_size=2,
        max_concurrency=2,
    )
    handled = await worker.run_once()
    assert len(handled) == 1
    assert started == [handled[0].execution_id]
    assert {handled[0].execution_id, "write-a", "write-b"} >= {
        "write-a",
        "write-b",
    }
    remaining = queue.get_by_execution_id(
        "write-a" if handled[0].execution_id == "write-b" else "write-b"
    )
    assert remaining is not None


def test_new_request_rejects_legacy_whole_plan() -> None:
    with pytest.raises(TaskProviderError) as error:
        ConversationRuntimeAdapter._require_new_request_incremental_plan(
            type("Plan", (), {"plan_source": "WHOLE_PLAN"})()
        )
    assert error.value.code == "WHOLE_PLAN_NEW_REQUEST_DISABLED"


def test_continuation_rehydrates_session_payload_before_direct_reads() -> None:
    session = ConversationRuntimeAdapter._coerce_session(
        {"timezone": "Asia/Shanghai"},
        conversation_id="conversation-1",
        user_id="u1",
        tenant_id="t1",
        timezone="Asia/Shanghai",
    )
    assert isinstance(session, SessionContext)
    assert session.conversation_id == "conversation-1"
    assert session.user_id == "u1"
    assert session.tenant_id == "t1"


def test_agent_run_claim_allows_multiple_runs_with_per_conversation_limit() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = AgentRunStore(engine, create_tables=True)
    for run_id in ("a", "b", "c"):
        store.create(
            AgentRun(
                run_id=run_id,
                conversation_id="conversation-1",
                user_id="u1",
                tenant_id="t1",
                payload={},
            )
        )
    claimed = store.claim(
        worker_id="runner",
        limit=3,
        max_concurrent_per_conversation=2,
    )
    assert {item.run_id for item in claimed} == {"a", "b"}
    assert len(store.claim(
        worker_id="runner-2",
        limit=1,
        max_concurrent_per_conversation=2,
    )) == 0
    engine.dispose()


def test_waiting_run_does_not_consume_agent_runner_capacity() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = AgentRunStore(engine, create_tables=True)
    store.create(
        AgentRun(
            run_id="waiting",
            conversation_id="conversation-1",
            user_id="u1",
            tenant_id="t1",
            status="WAITING_USER",
            payload={},
        )
    )
    store.create(
        AgentRun(
            run_id="ready",
            conversation_id="conversation-1",
            user_id="u1",
            tenant_id="t1",
            payload={},
        )
    )
    claimed = store.claim(
        worker_id="runner",
        limit=1,
        max_concurrent_per_conversation=1,
    )
    assert [item.run_id for item in claimed] == ["ready"]
    engine.dispose()


@pytest.mark.asyncio
async def test_agent_runner_executes_same_conversation_runs_overlapping() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = AgentRunStore(engine, create_tables=True)
    for run_id in ("run-a", "run-b"):
        store.create(
            AgentRun(
                run_id=run_id,
                conversation_id="conversation-1",
                user_id="u1",
                tenant_id="t1",
                payload={},
            )
        )
    timeline: dict[str, tuple[float, float]] = {}

    async def execute(run: AgentRun):
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.03)
        timeline[run.run_id] = (started, asyncio.get_running_loop().time())
        return type("Result", (), {"success": True, "status": "COMPLETED"})()

    async def handle_result(_run: AgentRun, _result: Any) -> None:
        return None

    runner = AgentRunner(
        run_store=store,
        event_store=AgentRunEventStore(),
        execute=execute,
        result_handler=handle_result,
        poll_interval_seconds=0.001,
        max_concurrent_runs=2,
        max_concurrent_per_conversation=2,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.08)
    runner.request_shutdown()
    await task
    assert set(timeline) == {"run-a", "run-b"}
    assert timeline["run-b"][0] < timeline["run-a"][1]
    assert timeline["run-a"][0] < timeline["run-b"][1]
    engine.dispose()


@pytest.mark.asyncio
async def test_agent_runner_keeps_queued_run_running_until_execution_converges() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = AgentRunStore(engine, create_tables=True)
    store.create(
        AgentRun(
            run_id="queued-run",
            conversation_id="conversation-1",
            user_id="u1",
            tenant_id="t1",
            payload={},
        )
    )

    async def execute(_run: AgentRun):
        return type("Result", (), {"success": False, "status": "QUEUED"})()

    runner = AgentRunner(
        run_store=store,
        event_store=AgentRunEventStore(),
        execute=execute,
        result_handler=lambda _run, _result: asyncio.sleep(0),
        poll_interval_seconds=0.001,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.02)
    runner.request_shutdown()
    await task

    assert store.get("queued-run").status == "RUNNING"
    assert not any(
        event.event_type == "RUN_COMPLETED"
        for event in runner._event_store.list_since("queued-run")
    )
    engine.dispose()


class _ConcurrentTaskRegistry:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.lock = asyncio.Lock()

    async def get_task(self, task_id: str) -> Task | None:
        return self.task.model_copy(deep=True) if task_id == self.task.task_id else None

    async def update_task(self, task_id: str, *, expected_version: int | None = None, **fields: Any) -> Task:
        async with self.lock:
            assert task_id == self.task.task_id
            if expected_version is not None:
                assert expected_version == self.task.version
            await asyncio.sleep(0.005)
            values = self.task.model_dump(mode="python")
            values.update(fields)
            values["goals"] = [TaskGoal.model_validate(item) for item in fields.get("goals", values["goals"])]
            values["version"] = self.task.version + 1
            self.task = Task.model_validate(values)
            return self.task.model_copy(deep=True)


@pytest.mark.asyncio
async def test_out_of_order_observations_preserve_both_goal_owners() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="two goals",
            children=[
                Goal(goal_id="g1", description="one", publication_intent="DRAFT_ONLY"),
                Goal(goal_id="g2", description="two", publication_intent="DRAFT_ONLY"),
            ],
        )
    )
    task = Task(
        task_id="task-1",
        conversation_id="conversation-1",
        user_id="u1",
        tenant_id="t1",
        status=TaskStatus.RUNNING,
        goals=[
            TaskGoal(task_id="task-1", goal_id="root", description="two goals"),
            TaskGoal(task_id="task-1", goal_id="g1", description="one"),
            TaskGoal(task_id="task-1", goal_id="g2", description="two"),
        ],
        goal_tree_snapshot=tree.model_dump(mode="json"),
        active_execution_id="execution-a",
    )
    registry = _ConcurrentTaskRegistry(task)

    @asynccontextmanager
    async def sessions():
        yield object()

    provider = TaskProvider(
        session_context_factory=sessions,
        registry_factory=lambda _session: registry,
    )
    scope = TaskScope(
        user_id="u1",
        tenant_id="t1",
        conversation_id="conversation-1",
    )

    async def complete(goal_id: str, execution_id: str, resource_id: str):
        return await provider.persist_completion_projection(
            scope,
            task_id="task-1",
            execution_id=execution_id,
            status="COMPLETED",
            goal_id=goal_id,
            artifacts=[
                {
                    "artifact_id": f"artifact-{goal_id}",
                    "type": "DRAFT",
                    "resource_type": "DRAFT",
                    "resource_id": resource_id,
                }
            ],
        )

    # Deliberately submit B before A; completion order is not a valid owner.
    await asyncio.gather(
        complete("g2", "execution-b", "draft-b"),
        complete("g1", "execution-a", "draft-a"),
    )
    projected = registry.task
    owners = {goal.goal_id: goal.execution_id for goal in projected.goals}
    statuses = {goal.goal_id: goal.status for goal in projected.goals}
    assert owners["g1"] == "execution-a"
    assert owners["g2"] == "execution-b"
    assert statuses["g1"] == statuses["g2"] == "COMPLETED"
    assert {item.execution_id for item in projected.execution_refs} == {
        "execution-a",
        "execution-b",
    }
