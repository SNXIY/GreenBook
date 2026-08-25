"""Crash-point recovery matrix for the existing durable boundaries.

The first case in this file is deliberately written against the real queue
handler boundary.  It injects a process crash after the durable OperationLedger
completion and before ActionObservation persistence, then lets a fresh queue
worker reclaim the same message.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueStatus,
)
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.execution.input import ExecutionInput, ExecutionStepInput
from greenbook_agent_core.execution.operation_ledger import OperationLedger
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    OperationStatus,
)
from greenbook_agent_core.execution.queue_execution_handler import (
    RuntimeExecutionQueueHandler,
)
from greenbook_agent_core.execution.repository import ExecutionRepository
from greenbook_agent_core.execution.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.execution.runtime_context import RuntimeContext, TaskContext
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task import (
    InMemoryTaskRepository,
    TaskConfirmationState,
    TaskManager,
)
from greenbook_contracts.identity import AuthContext


@pytest.fixture(autouse=True)
def _isolate_memory_execution_repository():
    ExecutionRepository.clear()
    yield
    ExecutionRepository.clear()


class _InjectedProcessCrashError(RuntimeError):
    pass


def _payload() -> dict[str, Any]:
    return {
        "task_id": "task-crash-e",
        "conversation_id": "conversation-crash-e",
        "run_id": "run-crash-e",
        "auth_context": {"user_id": "user-1", "tenant_id": "tenant-1"},
        "execution_input": {
            "task_id": "task-crash-e",
            "conversation_id": "conversation-crash-e",
            "goal": "publish the draft",
            "goal_category": "COMPOSITE",
            "execution_metadata": {
                "plan_mode": "INCREMENTAL",
                "capability": "PUBLISH_NOW",
            },
            "steps": [
                {
                    "step_id": "objective-a:publish",
                    "goal_id": "objective-a",
                    "capability": "PUBLISH_NOW",
                    "tool_name": "publication.publish",
                }
            ],
        },
    }


class _ReplayableService:
    def __init__(self) -> None:
        self.calls = 0
        self.physical_mutations = 0

    async def execute_queued(self, message: Any, **_: Any) -> RuntimeResult:
        self.calls += 1
        if self.calls == 1:
            # The first pass represents the one Java mutation that completed
            # before the process died.  A replay of a terminal Execution is a
            # projection recovery, not another physical mutation.
            self.physical_mutations += 1
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id="run-crash-e",
            task_id="task-crash-e",
            execution_id=message.execution_id,
            artifacts=[
                {
                    "artifact_id": "artifact-a",
                    "artifact_type": "POST",
                    "resource_id": "java-post-a",
                }
            ],
        )


class _CompletionPublisher:
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.calls = 0

    async def __call__(self, *_args: Any) -> None:
        self.calls += 1
        if self.crash:
            raise _InjectedProcessCrashError("crash after durable completion")


class _ObservationWriter:
    def __init__(self) -> None:
        self.results: list[RuntimeResult] = []

    async def __call__(self, _message: Any, result: RuntimeResult, _auth: Any, **_: Any) -> None:
        self.results.append(result)


class _NeverStartActionLoop:
    async def run(self, **_: Any) -> None:
        raise AssertionError("pending confirmation reached ActionLoop after restart")


def _auth(_message: Any) -> AuthContext:
    return AuthContext(
        user_id="user-1",
        tenant_id="tenant-1",
        timezone="Asia/Shanghai",
        raw_access_token="test-token",
    )


@pytest.mark.asyncio
async def test_confirmation_pending_survives_restart_without_queue_or_write() -> None:
    """A: confirmation is canonical Task state, not an in-process flag."""

    repository = InMemoryTaskRepository()
    first_process = TaskManager(repository)
    task = await first_process.create_task(
        conversation_id="conversation-crash-a",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="two writes",
    )
    pending = await first_process.set_confirmation_pending(
        task.task_id,
        snapshot_hash="snapshot-a",
        resume_run_id="run-crash-a",
    )

    restarted = TaskManager(repository)
    reloaded = await restarted.get_required(task.task_id)
    assert reloaded.confirmation_state == TaskConfirmationState.CONFIRMATION_PENDING
    assert reloaded.confirmation_version == pending.confirmation_version

    from apps.agent_api.greenbook_agent_api.services.action_loop_executor import (
        ActionLoopExecutor,
    )

    executor = ActionLoopExecutor(
        adapter=object(),
        task_manager=restarted,
        action_loop=_NeverStartActionLoop(),
    )
    result = await executor.resume_task(
        task_id=task.task_id,
        conversation_id="conversation-crash-a",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-crash-a",
        trace_id="trace-crash-a",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=None,
        auth=None,
        command=None,
    )
    assert result.status == "WAITING_HUMAN"
    assert result.error_code == "SEMANTIC_CONFIRMATION_REQUIRED"


def test_execution_and_ledger_restart_reuse_one_queue_identity() -> None:
    """C: an Execution/Ledger row created before queue submit is idempotent."""

    queue = ExecutionQueue()
    operation_store = ExternalOperationStore()
    first_ledger = OperationLedger(operation_store)
    first = first_ledger.begin_operation(
        idempotency_key="conversation-crash-c:task-crash-c:PUBLISH_NOW:objective-c:publish",
        conversation_id="conversation-crash-c",
        task_id="task-crash-c",
        execution_id="execution-crash-c",
        semantic_action="PUBLISH_NOW",
        resource_id="draft-c",
        resource_type="DRAFT",
        expected_postcondition={"expected": {"status": "PUBLISHED"}},
        claim_owner="process-a",
    )
    # Crash before Queue.submit: the durable operation remains PENDING.
    restarted_ledger = OperationLedger(operation_store)
    same = restarted_ledger.begin_operation(
        idempotency_key=first.idempotency_key or "",
        conversation_id="conversation-crash-c",
        task_id="task-crash-c",
        execution_id="execution-crash-c",
        semantic_action="PUBLISH_NOW",
        resource_id="draft-c",
        resource_type="DRAFT",
        expected_postcondition={"expected": {"status": "PUBLISHED"}},
        claim_owner="process-b",
    )
    first_message = queue.enqueue("execution-crash-c", payload={"safe": "c"})
    duplicate_message = queue.enqueue("execution-crash-c", payload={"safe": "c"})
    assert same.operation_id == first.operation_id
    assert duplicate_message.message_id == first_message.message_id
    assert operation_store.count() == 1


@pytest.mark.asyncio
async def test_crash_after_ledger_success_before_observation_replays_projection_only() -> None:
    now = [datetime(2026, 8, 21, 0, 0, tzinfo=UTC)]
    queue = ExecutionQueue(now_factory=lambda: now[0])
    operation_store = ExternalOperationStore()
    ledger = OperationLedger(operation_store)
    service = _ReplayableService()
    observation_writer = _ObservationWriter()
    message = queue.enqueue("execution-crash-e", payload=_payload())
    claimed = queue.claim(now[0], worker_id="worker-a", lease_seconds=1)[0]

    crashing_handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=service,  # type: ignore[arg-type]
        credential_resolver=_auth,
        completion_publisher=_CompletionPublisher(crash=True),
        observation_writer=observation_writer,
        operation_ledger=ledger,
        worker_id="worker-a",
    )

    with pytest.raises(_InjectedProcessCrashError):
        await crashing_handler(claimed)

    operation_id = next(iter(operation_store._records))
    assert operation_store.get(operation_id).status == OperationStatus.SUCCEEDED
    assert observation_writer.results == []
    assert queue.get(message.message_id).status == ExecutionQueueStatus.CLAIMED

    # A fresh process/worker reclaims the same queue row after the lease.
    now[0] += timedelta(seconds=2)
    recovered_handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=service,  # type: ignore[arg-type]
        credential_resolver=_auth,
        completion_publisher=_CompletionPublisher(),
        observation_writer=observation_writer,
        operation_ledger=OperationLedger(operation_store),
        worker_id="worker-b",
    )
    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=recovered_handler,
        worker_id="worker-b",
        lease_seconds=1,
    )
    await worker.run_once(now=now[0])

    assert queue.get(message.message_id).status == ExecutionQueueStatus.ACKED
    assert service.calls == 2
    assert service.physical_mutations == 1
    assert len(observation_writer.results) == 1
    assert observation_writer.results[0].execution_id == message.execution_id
    assert operation_store.count() == 1


@pytest.mark.asyncio
async def test_crash_after_operation_claim_reclaims_expired_unstarted_write() -> None:
    """A claimed-but-unstarted operation must not be ACKed as unfinished."""

    now = [datetime.now(UTC)]
    queue = ExecutionQueue(now_factory=lambda: now[0])
    operation_store = ExternalOperationStore()
    ledger = OperationLedger(operation_store)
    service = _ReplayableService()
    observation_writer = _ObservationWriter()
    message = queue.enqueue("execution-crash-reclaim", payload=_payload())
    queue.claim(now[0], worker_id="worker-a", lease_seconds=1)

    operation = ledger.begin_operation(
        idempotency_key=(
            "conversation-crash-e:task-crash-e:PUBLISH_NOW:objective-a:publish"
        ),
        conversation_id="conversation-crash-e",
        task_id="task-crash-e",
        execution_id=message.execution_id,
        semantic_action="PUBLISH_NOW",
    )
    claimed = ledger.claim(operation.operation_id, owner="worker-a")
    assert claimed is not None
    operation_store.save(
        claimed.model_copy(
            update={"lease_expires_at": (now[0] - timedelta(seconds=1)).isoformat()}
        )
    )

    now[0] += timedelta(seconds=2)
    recovered_handler = RuntimeExecutionQueueHandler(
        mcp=None,
        service=service,  # type: ignore[arg-type]
        credential_resolver=_auth,
        completion_publisher=_CompletionPublisher(),
        observation_writer=observation_writer,
        operation_ledger=OperationLedger(operation_store),
        worker_id="worker-b",
    )
    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=recovered_handler,
        worker_id="worker-b",
        lease_seconds=1,
    )

    await worker.run_once(now=now[0])

    assert queue.get(message.message_id).status == ExecutionQueueStatus.ACKED
    assert service.calls == 1
    assert operation_store.get(operation.operation_id).status == OperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_crash_after_execution_create_republishes_same_dispatch_envelope() -> None:
    """B: the execution-create/queue-submit window is recoverable in place."""

    queue = ExecutionQueue()
    container = RuntimeContainer.for_testing()
    service = RuntimeAgentService(
        container=container,
        execution_queue=queue,
        dispatch_mode="queue",
    )
    execution_input = ExecutionInput(
        task_id="task-crash-b",
        conversation_id="conversation-crash-b",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="search posts",
        goal_category="COMPOSITE",
        steps=[
            ExecutionStepInput(
                step_id="objective-b:search",
                goal_id="objective-b",
                capability="SEARCH_COMMUNITY",
                tool_name="community.search_public_posts",
                output_artifact_type="SEARCH_RESULT",
            )
        ],
    )
    context = RuntimeContext(
        conversation_id="conversation-crash-b",
        run_id="run-crash-b",
        trace_id="trace-crash-b",
        task_id="task-crash-b",
        task_context=TaskContext(
            task_id="task-crash-b",
            goal="search posts",
            execution_input=execution_input,
        ),
        execution_input=execution_input,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    plan = TaskPlan(
        task_id="task-crash-b",
        steps=[
            PlanStep(
                step_id="objective-b:search",
                goal_id="objective-b",
                ordinal=1,
                capability="SEARCH_COMMUNITY",
                tool_name="community.search_public_posts",
                output_artifact_type="SEARCH_RESULT",
            )
        ],
    )

    original_enqueue = queue.enqueue

    def crash_before_submit(*_: Any, **__: Any) -> None:
        raise _InjectedProcessCrashError("crash before queue submit")

    queue.enqueue = crash_before_submit  # type: ignore[method-assign]
    with pytest.raises(_InjectedProcessCrashError):
        await service.submit_plan(context, plan)

    persisted = container.execution_repository.list_all()
    assert len(persisted) == 1
    execution = persisted[0]
    assert execution.status.value == "PENDING"
    envelope = execution.steps[0].checkpoint_data["dispatch_payload"]
    assert envelope["execution_input"]["task_id"] == "task-crash-b"
    assert envelope["execution_input"]["steps"][0]["step_id"] == (
        "objective-b:search"
    )
    assert queue.list() == []

    # New process/startup instance: the existing queue reconciliation helper
    # republishes the same execution identity and safe envelope.
    queue.enqueue = original_enqueue  # type: ignore[method-assign]
    persistence = SimpleNamespace(
        execution_repository=container.execution_repository,
        execution_queue=queue,
    )
    from greenbook_agent_api.main import _recover_unqueued_executions

    assert _recover_unqueued_executions(persistence) == 1
    recovered = queue.get_by_execution_id(execution.execution_id)
    assert recovered is not None
    assert recovered.payload == envelope
    assert container.execution_repository.list_all()[0].execution_id == (
        recovered.execution_id
    )
