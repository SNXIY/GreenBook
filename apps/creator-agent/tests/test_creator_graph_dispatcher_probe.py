import asyncio
from contextlib import AsyncExitStack
import logging
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.creator.api.dispatcher import CreatorLocalRunDispatcher
from app.creator.application.harness import CreatorAgentHarness, CreatorHarnessPolicy
from app.creator.domain.models import (
    CreateCreatorTaskCommand,
    CreatorRunStatus,
    CreatorTaskKind,
    CreatorTaskStatus,
)
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.runtime.composition import build_creator_runtime
from app.creator.runtime.checkpoints import open_creator_checkpointer
from test_creator_graph_direct_probe import DeterministicModel


class TracingRuntime:
    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self.name = runtime.name
        self.graph_runtime = runtime

    async def start(self, request, **kwargs):
        config = self._runtime._config(request.thread_id)
        print(
            f"RUNTIME_START_ENTERED task_id={request.task_id} run_id={request.run_id} "
            f"thread_id={request.thread_id} checkpoint_ns="
            f"{config['configurable'].get('checkpoint_ns', '')!r} loop_id={id(asyncio.get_running_loop())} "
            f"thread_ident={threading.get_ident()}",
            flush=True,
        )
        print(
            f"GRAPH_CONFIG_BUILT config_keys={tuple(config)} "
            f"configurable_keys={tuple(config['configurable'])} "
            f"recursion_limit={config['recursion_limit']}",
            flush=True,
        )
        result = await self._runtime.start(request, **kwargs)
        print(
            f"RUNTIME_OUTCOME_CREATED status={result.status.value} "
            f"final_artifact_id={result.final_artifact_id} "
            f"checkpoint_id={result.checkpoint_id}",
            flush=True,
        )
        return result

    async def resume(self, request, **kwargs):
        return await self._runtime.resume(request, **kwargs)


class GraphLogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.anyio
async def test_real_creator_graph_through_production_dispatcher() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    test_loop_id = id(asyncio.get_running_loop())
    test_thread_id = threading.get_ident()
    print(
        f"R2_TEST_STARTED loop_id={test_loop_id} thread_id={test_thread_id}",
        flush=True,
    )

    database_path = Path(tempfile.gettempdir()) / f"greenbook-r2-{uuid4().hex}.sqlite"
    database = CreatorDatabase.from_url(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await database.create_schema_for_development()
    settings = type(
        "R2Settings",
        (),
        {
            "creator_specialist_timeout_seconds": 10.0,
            "creator_max_supervisor_turns": 24,
            "creator_max_agent_dispatches": 24,
            "creator_max_model_calls": 24,
            "creator_max_output_tokens": 40_000,
            "creator_max_replans": 4,
            "creator_max_writer_revisions": 2,
        },
    )()
    checkpoint_path = Path(tempfile.gettempdir()) / f"greenbook-r2-checkpoint-{uuid4().hex}.sqlite"
    checkpoint_settings = type(
        "R2CheckpointSettings",
        (),
        {
            "creator_checkpoint_backend": "sqlite",
            "creator_checkpoint_sqlite_path": str(checkpoint_path),
            "creator_checkpoint_postgres_url": "",
            "creator_checkpoint_auto_setup": True,
            "creator_checkpoint_diagnostics": True,
        },
    )()
    checkpoint_stack = AsyncExitStack()
    checkpointer = await checkpoint_stack.enter_async_context(
        open_creator_checkpointer(checkpoint_settings)
    )
    runtime = build_creator_runtime(
        settings=settings,
        ai_client=None,
        artifact_store=database.artifact_store,
        checkpointer=checkpointer,
        model_gateway=DeterministicModel(),
    )
    traced_runtime = TracingRuntime(runtime)
    compiled_graph = runtime._graph.compiled
    print(
        f"GRAPH_CREATED_OR_REUSED compiled_graph_object_id={id(compiled_graph)} "
        f"graph_build_loop_id={test_loop_id} graph_build_thread_id={test_thread_id}",
        flush=True,
    )
    print(
        f"CHECKPOINTER type={type(runtime._graph._checkpointer).__name__} "
        f"object_id={id(runtime._graph._checkpointer)} "
        f"created_loop_id={test_loop_id}",
        flush=True,
    )
    print("R2_INITIAL_STATE_SOURCE app.creator.runtime.runtime.LangGraphCreatorRuntime.start", flush=True)
    print(
        "R2_INITIAL_STATE_TYPE CreatorGraphState; keys="
        "identity,goal,limits,usage,plan,plan_history,executions,artifacts,facts," 
        "progress,errors,decision,control_status,final_artifact_id," 
        "pending_decision_artifact_id,applied_decision_id",
        flush=True,
    )

    harness = CreatorAgentHarness(
        uow_factory=database.uow_factory,
        runtime=traced_runtime,
        policy=CreatorHarnessPolicy(
            max_runtime_attempts=1,
            retry_delay_seconds=0,
            run_lease_seconds=60,
        ),
    )
    dispatcher = CreatorLocalRunDispatcher(
        harness,
        worker_prefix="r2-real-dispatcher",
        concurrency=1,
        tenant_concurrency=1,
        user_concurrency=1,
        retry_delay_seconds=0,
        shutdown_grace_seconds=10,
    )
    print(
        f"DISPATCHER_STARTED instance_id={dispatcher.dispatcher_instance_id} "
        f"loop_id={test_loop_id} thread_ident={test_thread_id}",
        flush=True,
    )

    try:
        created = await harness.create_task(
            CreateCreatorTaskCommand(
                tenant_id="r2-tenant-new",
                creator_id="r2-creator-new",
                kind=CreatorTaskKind.CREATE_CONTENT,
                goal="Create a concise knowledge post for the R2 integration probe",
                constraints={
                    "approval_mode": "AUTO",
                    "format": "POST",
                    "interaction_mode": "AUTO",
                    "language": "en",
                },
                source_scope={
                    "include_community_posts": False,
                    "include_creator_profile": False,
                },
                idempotency_key=f"r2-idempotency-{uuid4().hex}",
            )
        )
        print(
            f"TASK_CREATED task_id={created.task_id} run_id={created.run_id} "
            f"status={created.status.value}",
            flush=True,
        )
        assert dispatcher.schedule_run(created.run_id) is True
        print(
            f"SCHEDULE_RUN_CALLED run_id={created.run_id} "
            f"dispatcher_instance_id={dispatcher.dispatcher_instance_id}",
            flush=True,
        )

        terminal = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            async with database.uow_factory() as uow:
                task = await uow.tasks.get(created.task_id)
                run = await uow.runs.get(created.run_id)
            if task is not None and task.status in {
                CreatorTaskStatus.COMPLETED,
                CreatorTaskStatus.FAILED,
                CreatorTaskStatus.CANCELLED,
            }:
                terminal = (task, run)
                break
            await asyncio.sleep(0.05)

        if terminal is None:
            config = runtime._config(created.thread_id)
            snapshot = await compiled_graph.aget_state(config)
            history = [item async for item in compiled_graph.aget_state_history(config)]
            diagnostics = dispatcher.diagnostics()
            print(
                f"R2_TIMEOUT_SNAPSHOT next={snapshot.next} tasks={snapshot.tasks} "
                f"interrupts={snapshot.interrupts} metadata={snapshot.metadata} "
                f"parent_config={snapshot.parent_config} "
                f"value_keys={tuple(snapshot.values)} history_count={len(history)}",
                flush=True,
            )
            print(f"R2_TIMEOUT_DISPATCHER {diagnostics}", flush=True)
            raise AssertionError("R2 task did not reach a terminal state")

        task, run = terminal
        artifacts = await database.artifact_store.list_for_run(created.run_id)
        print(
            f"RUN_TERMINAL_PERSISTED run_id={run.id} status={run.status.value} "
            f"checkpoint_id={run.checkpoint_id}",
            flush=True,
        )
        print(
            f"TASK_TERMINAL_PERSISTED task_id={task.id} status={task.status.value} "
            f"final_artifact_id={task.final_artifact_id}",
            flush=True,
        )
        print(
            f"R2_ARTIFACTS count={len(artifacts)} "
            f"kinds={[artifact.kind.value for artifact in artifacts]}",
            flush=True,
        )
        assert task.status == CreatorTaskStatus.COMPLETED
        assert run.status == CreatorRunStatus.COMPLETED
        assert task.final_artifact_id
        assert any(artifact.id == task.final_artifact_id for artifact in artifacts)

        await dispatcher.wait_idle()
        diagnostics = dispatcher.diagnostics()
        print(f"DISPATCHER_DIAGNOSTICS {diagnostics}", flush=True)
        assert diagnostics["dispatcher_alive"] is True
        assert diagnostics["active_task_count"] == 0
        assert diagnostics["dispatcher_last_error"] is None
    finally:
        print("DISPATCHER_STOP_STARTED", flush=True)
        await dispatcher.aclose()
        print("DISPATCHER_STOPPED", flush=True)
        await checkpoint_stack.aclose()
        await database.dispose()
        database_path.unlink(missing_ok=True)
        checkpoint_path.unlink(missing_ok=True)
        print("R2_TEST_FINISHED", flush=True)


@pytest.mark.anyio
async def test_creator_graph_sequential_create_and_revise_stability() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    logger = logging.getLogger("uvicorn.error")
    logger.setLevel(logging.INFO)
    collector = GraphLogCollector()
    logger.addHandler(collector)
    database_path = Path(tempfile.gettempdir()) / f"greenbook-r2-seq-{uuid4().hex}.sqlite"
    checkpoint_path = Path(tempfile.gettempdir()) / f"greenbook-r2-seq-checkpoint-{uuid4().hex}.sqlite"
    database = CreatorDatabase.from_url(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await database.create_schema_for_development()
    settings = type(
        "R2SequenceSettings",
        (),
        {
            "creator_specialist_timeout_seconds": 10.0,
            "creator_max_supervisor_turns": 24,
            "creator_max_agent_dispatches": 24,
            "creator_max_model_calls": 24,
            "creator_max_output_tokens": 40_000,
            "creator_max_replans": 4,
            "creator_max_writer_revisions": 2,
        },
    )()
    checkpoint_settings = type(
        "R2SequenceCheckpointSettings",
        (),
        {
            "creator_checkpoint_backend": "sqlite",
            "creator_checkpoint_sqlite_path": str(checkpoint_path),
            "creator_checkpoint_postgres_url": "",
            "creator_checkpoint_auto_setup": True,
            "creator_checkpoint_diagnostics": True,
        },
    )()
    checkpoint_stack = AsyncExitStack()
    checkpointer = await checkpoint_stack.enter_async_context(
        open_creator_checkpointer(checkpoint_settings)
    )
    runtime = build_creator_runtime(
        settings=settings,
        ai_client=None,
        artifact_store=database.artifact_store,
        checkpointer=checkpointer,
        model_gateway=DeterministicModel(),
    )
    harness = CreatorAgentHarness(
        uow_factory=database.uow_factory,
        runtime=TracingRuntime(runtime),
        policy=CreatorHarnessPolicy(
            max_runtime_attempts=1,
            retry_delay_seconds=0,
            run_lease_seconds=60,
        ),
    )
    dispatcher = CreatorLocalRunDispatcher(
        harness,
        worker_prefix="r2-seq-dispatcher",
        concurrency=1,
        tenant_concurrency=1,
        user_concurrency=1,
        retry_delay_seconds=0,
        shutdown_grace_seconds=10,
    )

    async def run_one(index: int, kind: CreatorTaskKind) -> None:
        before = len(collector.messages)
        constraints = {
            "approval_mode": "AUTO",
            "format": "POST",
            "interaction_mode": "AUTO",
            "language": "en",
        }
        if kind == CreatorTaskKind.IMPROVE_DRAFT:
            constraints["draft"] = {
                "title": f"R2 source draft {index}",
                "body_markdown": "# R2 source draft\n\nA source draft for the sequential probe.",
            }
        created = await harness.create_task(
            CreateCreatorTaskCommand(
                tenant_id=f"r2-seq-tenant-{index}",
                creator_id=f"r2-seq-creator-{index}",
                kind=kind,
                goal=f"R2 sequential {'revise' if kind == CreatorTaskKind.IMPROVE_DRAFT else 'create'} {index}",
                constraints=constraints,
                source_scope={
                    "include_community_posts": False,
                    "include_creator_profile": False,
                },
                idempotency_key=f"r2-seq-idempotency-{index}-{uuid4().hex}",
            )
        )
        async with database.uow_factory() as uow:
            created_run = await uow.runs.get(created.run_id)
        assert created_run is not None
        config = runtime._config(created_run.thread_id)
        print(
            f"SEQUENTIAL_TASK_CREATED index={index} kind={kind.value} "
            f"task_id={created.task_id} run_id={created.run_id} "
            f"thread_id={config['configurable']['thread_id']} "
            f"checkpoint_ns={config['configurable'].get('checkpoint_ns', '')!r}",
            flush=True,
        )
        assert dispatcher.schedule_run(created.run_id) is True
        deadline = time.monotonic() + 45
        terminal = None
        while time.monotonic() < deadline:
            async with database.uow_factory() as uow:
                task = await uow.tasks.get(created.task_id)
                run = await uow.runs.get(created.run_id)
            if task is not None and task.status in {
                CreatorTaskStatus.COMPLETED,
                CreatorTaskStatus.FAILED,
                CreatorTaskStatus.CANCELLED,
            }:
                terminal = task, run
                break
            await asyncio.sleep(0.05)
        assert terminal is not None
        task, run = terminal
        artifacts = await database.artifact_store.list_for_run(created.run_id)
        messages = collector.messages[before:]
        supervise_started = any(
            "graph_node_started node_name=supervise" in message
            for message in messages
        )
        execute_started = any(
            "graph_node_started node_name=execute_agent" in message
            for message in messages
        )
        print(
            f"SEQUENTIAL_TASK_RESULT index={index} task_id={task.id} run_id={run.id} "
            f"thread_id={config['configurable']['thread_id']} "
            f"checkpoint_ns={config['configurable'].get('checkpoint_ns', '')!r} "
            f"supervise_started={supervise_started} execute_agent_started={execute_started} "
            f"artifact_count={len(artifacts)} final_artifact_id={task.final_artifact_id} "
            f"task_status={task.status.value} run_status={run.status.value}",
            flush=True,
        )
        assert supervise_started
        assert execute_started
        assert task.status == CreatorTaskStatus.COMPLETED
        assert run.status == CreatorRunStatus.COMPLETED
        assert task.final_artifact_id
        assert artifacts
        await dispatcher.wait_idle()
        diagnostics = dispatcher.diagnostics()
        assert diagnostics["active_task_count"] == 0
        assert diagnostics["dispatcher_last_error"] is None

    try:
        sequence = (
            (1, CreatorTaskKind.CREATE_CONTENT),
            (2, CreatorTaskKind.IMPROVE_DRAFT),
            (3, CreatorTaskKind.CREATE_CONTENT),
            (4, CreatorTaskKind.CREATE_CONTENT),
            (5, CreatorTaskKind.CREATE_CONTENT),
            (6, CreatorTaskKind.IMPROVE_DRAFT),
            (7, CreatorTaskKind.IMPROVE_DRAFT),
            (8, CreatorTaskKind.IMPROVE_DRAFT),
        )
        for index, kind in sequence:
            await run_one(index, kind)
        print(
            f"SEQUENTIAL_DISPATCHER_FINAL {dispatcher.diagnostics()}",
            flush=True,
        )
    finally:
        await dispatcher.aclose()
        await checkpoint_stack.aclose()
        await database.dispose()
        database_path.unlink(missing_ok=True)
        checkpoint_path.unlink(missing_ok=True)
        logger.removeHandler(collector)
