import asyncio
import logging
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from app.creator.api.dispatcher import CreatorLocalRunDispatcher
from app.creator.application.harness import CreatorAgentHarness, CreatorHarnessPolicy
from app.creator.domain.models import (
    CreateCreatorTaskCommand,
    CreatorRunStatus,
    CreatorTaskKind,
    CreatorTaskStatus,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeOutcomeStatus,
)
from app.creator.infrastructure.database import CreatorDatabase


class DispatcherProbeState(TypedDict):
    value: str
    visited: list[str]


def build_dispatcher_probe_graph():
    async def probe(state: DispatcherProbeState) -> dict:
        print(
            f"PROBE_NODE_STARTED loop_id={id(asyncio.get_running_loop())} "
            f"thread_id={threading.get_ident()}",
            flush=True,
        )
        return {
            "value": state["value"],
            "visited": [*state.get("visited", []), "probe"],
        }

    builder = StateGraph(DispatcherProbeState)
    builder.add_node("probe", probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    return builder.compile()


class DispatcherProbeRuntime:
    name = "m2-dispatcher-probe-runtime"

    def __init__(self) -> None:
        self.graph = build_dispatcher_probe_graph()
        self.graph_results: dict[str, dict] = {}
        self.loop_ids: list[int] = []
        self.thread_ids: list[int] = []

    async def start(self, request, **kwargs):
        del kwargs
        loop_id = id(asyncio.get_running_loop())
        thread_id = threading.get_ident()
        self.loop_ids.append(loop_id)
        self.thread_ids.append(thread_id)
        print(
            f"GRAPH_INVOKE_STARTED task_id={request.task_id} "
            f"loop_id={loop_id} thread_id={thread_id}",
            flush=True,
        )
        result = await self.graph.ainvoke({"value": "m2", "visited": []})
        self.graph_results[request.task_id] = result
        print(f"GRAPH_INVOKE_FINISHED result={result}", flush=True)
        return RuntimeOutcome(
            status=RuntimeOutcomeStatus.COMPLETED,
            final_artifact_id=f"m2-final-{request.task_id}",
            checkpoint_id="m2-no-checkpoint",
            events=(),
            state_summary={"graph_result": result},
        )

    async def resume(self, request):
        raise AssertionError("M2 probe must not resume a human decision")


@pytest.mark.anyio
async def test_production_dispatcher_drives_minimal_graph() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    test_loop_id = id(asyncio.get_running_loop())
    test_thread_id = threading.get_ident()
    print(
        f"M2_TEST_STARTED loop_id={test_loop_id} thread_id={test_thread_id}",
        flush=True,
    )

    database_path = Path(tempfile.gettempdir()) / f"greenbook-m2-{uuid4().hex}.sqlite"
    database = CreatorDatabase.from_url(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await database.create_schema_for_development()
    runtime = DispatcherProbeRuntime()
    print("GRAPH_MERMAID", flush=True)
    print(runtime.graph.get_graph().draw_mermaid(), flush=True)
    harness = CreatorAgentHarness(
        uow_factory=database.uow_factory,
        runtime=runtime,
        policy=CreatorHarnessPolicy(
            max_runtime_attempts=1,
            retry_delay_seconds=0,
            run_lease_seconds=10,
        ),
    )
    dispatcher = CreatorLocalRunDispatcher(
        harness,
        worker_prefix="m2-probe-dispatcher",
        concurrency=1,
        tenant_concurrency=1,
        user_concurrency=1,
        retry_delay_seconds=0,
        shutdown_grace_seconds=5,
    )
    print(
        f"DISPATCHER_STARTED instance_id={dispatcher.dispatcher_instance_id} "
        f"loop_id={test_loop_id} thread_id={test_thread_id}",
        flush=True,
    )

    try:
        created = await harness.create_task(
            CreateCreatorTaskCommand(
                tenant_id="m2-tenant",
                creator_id="m2-creator",
                kind=CreatorTaskKind.CREATE_CONTENT,
                goal="M2 dispatcher probe",
                idempotency_key="m2-idempotency-new",
            )
        )
        task_id = created.task_id
        print(
            f"TASK_CREATED task_id={created.task_id} run_id={created.run_id} "
            f"status={created.status.value} loop_id={test_loop_id} "
            f"thread_id={test_thread_id}",
            flush=True,
        )

        scheduled = dispatcher.schedule_run(created.run_id)
        assert scheduled is True
        claimed_seen = False
        terminal = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            async with database.uow_factory() as uow:
                task = await uow.tasks.get(created.task_id)
                run = await uow.runs.get(created.run_id)
            if run is not None and run.execution_attempts >= 1:
                if not claimed_seen:
                    claimed_seen = True
                    print(
                        f"TASK_CLAIMED task_id={task.id} run_id={run.id} "
                        f"attempt={run.execution_attempts} status={run.status.value} "
                        f"loop_id={test_loop_id} thread_id={test_thread_id}",
                        flush=True,
                    )
            if task is not None and task.status in {
                CreatorTaskStatus.COMPLETED,
                CreatorTaskStatus.FAILED,
                CreatorTaskStatus.CANCELLED,
            }:
                terminal = (task, run)
                break
            await asyncio.sleep(0.02)

        assert terminal is not None, "M2 task did not reach a terminal state"
        task, run = terminal
        print(
            f"TASK_MARKED_COMPLETED task_id={task.id} status={task.status.value} "
            f"run_status={run.status.value} attempt={run.execution_attempts} "
            f"loop_id={test_loop_id} thread_id={test_thread_id}",
            flush=True,
        )
        assert claimed_seen
        assert run.execution_attempts == 1
        assert task.status == CreatorTaskStatus.COMPLETED
        assert run.status == CreatorRunStatus.COMPLETED
        assert runtime.graph_results[task.id] == {"value": "m2", "visited": ["probe"]}

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
        await database.dispose()
        database_path.unlink(missing_ok=True)
        print("M2_TEST_FINISHED", flush=True)
