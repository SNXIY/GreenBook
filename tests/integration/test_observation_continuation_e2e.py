"""Observation-Driven continuation E2E over real PostgreSQL.

Verifies the durable closed loop without live Java/Creator/LLM services:
INCREMENTAL execution completion -> ActionObservation persisted -> new
process (restart) recovers it -> idempotent claim -> crash recovery.

The decision layer (AgentLoop resuming to SCHEDULE_PUBLISH) is covered by
tests/unit/test_action_observation_continuation.py; this file proves the
durability, idempotency, and crash-safety of the observation marker.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from greenbook_agent_core.execution.action_observation import (
    OBSERVATION_DONE,
    OBSERVATION_PENDING,
    ActionObservationWriter,
    PostgresActionObservationStore,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.queue_execution_handler import RuntimeExecutionQueueHandler
from greenbook_agent_core.execution.runtime_result import RuntimeResult

pytestmark = pytest.mark.integration


def _database_url() -> str:
    # The Runtime store adapters are synchronous; normalize any asyncpg URL
    # that may leak in from the environment.
    url = os.getenv(
        "GREENBOOK_AGENT_DATABASE_URL",
        "postgresql+psycopg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
    )
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


@pytest.fixture
def bind() -> Any:
    import sqlalchemy as sa

    url = _database_url()
    try:
        engine = sa.create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
        )
        with engine.connect():
            pass
    except Exception as exc:
        engine = None
        pytest.skip(f"PostgreSQL unavailable ({exc}) — DB-backed test skipped")
    yield engine
    if engine is not None:
        engine.dispose()


@pytest.fixture
def store(bind: Any) -> PostgresActionObservationStore:
    store_instance = PostgresActionObservationStore(bind, create_tables=True)
    with bind.begin() as connection:
        connection.execute(store_instance._table.delete())
    return store_instance


def _incremental_message(*, execution_id: str, capability: str = "GENERATE_CONTENT") -> ExecutionQueueMessage:
    return ExecutionQueueMessage(
        execution_id=execution_id,
        trace_id=f"trace-{execution_id}",
        payload={
            "conversation_id": f"conv-{execution_id}",
            "task_id": f"task-{execution_id}",
            "run_id": f"run-{execution_id}",
            "session": {
                "conversation_id": f"conv-{execution_id}",
                "user_id": "u-e2e",
                "tenant_id": "ten-e2e",
                "timezone": "Asia/Shanghai",
            },
            "execution_input": {
                "goal_id": "g1",
                "execution_metadata": {
                    "plan_mode": "INCREMENTAL",
                    "goal_tree": {
                        "root": {
                            "goal_id": "g1",
                            "description": "Write a Redis post and schedule it",
                            "goal_type": "CREATE",
                            "required_capabilities": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                            "publication_intent": "SCHEDULED_PUBLISH",
                        }
                    },
                    "command": {"type": "CREATE", "goal": "Write a Redis post and schedule it"},
                },
                "steps": [{"step_id": "g1:1", "goal_id": "g1", "capability": capability}],
            },
        },
    )


def _completed_result(execution_id: str, *, draft_id: str = "draft-e2e") -> RuntimeResult:
    return RuntimeResult(
        success=True,
        status="COMPLETED",
        run_id=f"run-{execution_id}",
        task_id=f"task-{execution_id}",
        execution_id=execution_id,
        summary="Draft created",
        artifacts=[
            {
                "artifact_id": f"art-{execution_id}",
                "artifact_type": "DRAFT",
                "type": "DRAFT",
                "resource_type": "DRAFT",
                "resource_id": draft_id,
                "title": "Redis 高并发优化",
                "status": "DRAFT",
            }
        ],
    )


def _auth() -> Any:
    from greenbook_contracts.identity import AuthContext

    return AuthContext(user_id="u-e2e", tenant_id="ten-e2e", raw_access_token="")


async def _run_handler(handler: RuntimeExecutionQueueHandler, message: ExecutionQueueMessage) -> None:
    asyncio.run(handler(message))


def test_completion_persists_observation_then_restart_recovers(bind: Any, store: Any) -> None:
    execution_id = f"e1-{uuid.uuid4().hex[:8]}"
    writer = ActionObservationWriter(store=store)
    handled: list[str] = []

    async def execute_queued(message, **_kwargs):
        handled.append(message.execution_id)
        return _completed_result(message.execution_id)

    def resolve(_message):
        return _auth()

    handler = RuntimeExecutionQueueHandler(
        service=SimpleNamespace(execute_queued=execute_queued),
        mcp=None,
        credential_resolver=resolve,
        completion_publisher=None,
        observation_writer=writer,
    )
    message = _incremental_message(execution_id=execution_id)
    asyncio.run(handler(message))

    assert handled == [execution_id]
    persisted = store.get_by_execution(execution_id)
    assert persisted is not None
    assert persisted.status == "COMPLETED"
    assert persisted.draft_id == "draft-e2e"
    assert persisted.goal_id == "g1"
    assert persisted.capability == "GENERATE_CONTENT"
    assert persisted.state == OBSERVATION_PENDING
    assert persisted.payload["goal_tree"]
    assert persisted.payload["command"]
    assert persisted.payload["session"]

    # ── restart: a brand-new store instance over the same bind ──
    restarted = PostgresActionObservationStore(bind, create_tables=False)
    recovered = restarted.get_by_execution(execution_id)
    assert recovered is not None
    assert recovered.draft_id == "draft-e2e"
    assert recovered.business_result["draft_id"] == "draft-e2e"


def test_repeated_terminal_hook_is_idempotent(bind: Any, store: Any) -> None:
    execution_id = f"e2-{uuid.uuid4().hex[:8]}"
    writer = ActionObservationWriter(store=store)
    message = _incremental_message(execution_id=execution_id)
    writer.write(message, _completed_result(execution_id), _auth())
    writer.write(message, _completed_result(execution_id), _auth())
    assert store.count() == 1
    assert store.get_by_execution(execution_id) is not None


def test_whole_plan_message_writes_no_observation(bind: Any, store: Any) -> None:
    execution_id = f"e3-{uuid.uuid4().hex[:8]}"
    writer = ActionObservationWriter(store=store)
    message = _incremental_message(execution_id=execution_id)
    message.payload["execution_input"]["execution_metadata"]["plan_mode"] = "WHOLE_PLAN"
    assert writer.write(message, _completed_result(execution_id), _auth()) is None
    assert store.get_by_execution(execution_id) is None


def test_claim_and_done_then_crash_recovery(bind: Any, store: Any) -> None:
    execution_id = f"e4-{uuid.uuid4().hex[:8]}"
    writer = ActionObservationWriter(store=store)
    writer.write(
        _incremental_message(execution_id=execution_id),
        _completed_result(execution_id),
        _auth(),
    )
    claimed = store.claim_pending(batch_size=5)
    assert len(claimed) == 1
    assert claimed[0].execution_id == execution_id

    # Simulated crash before mark_done: an immediate re-poll with a zero
    # timeout recovers the same observation exactly once.
    recovered = store.claim_pending(batch_size=5, dispatch_timeout_seconds=0)
    assert [item.execution_id for item in recovered] == [execution_id]
    store.mark_done(recovered[0].observation_id)
    assert store.list_pending() == []
    done = store.get_by_execution(execution_id)
    assert done is not None and done.state == OBSERVATION_DONE


def test_schedule_observation_records_schedule_id(bind: Any, store: Any) -> None:
    execution_id = f"e5-{uuid.uuid4().hex[:8]}"
    writer = ActionObservationWriter(store=store)
    result = _completed_result(execution_id, draft_id="draft-e2e")
    result.schedule_id = "schedule-e2e"
    result.schedule = {"schedule_id": "schedule-e2e", "draft_id": "draft-e2e", "status": "SCHEDULED"}
    message = _incremental_message(execution_id=execution_id, capability="SCHEDULE_PUBLISH")
    observation = writer.write(message, result, _auth())
    assert observation is not None
    assert observation.capability == "SCHEDULE_PUBLISH"
    assert observation.schedule_id == "schedule-e2e"
    assert observation.business_result["schedule"]["schedule_id"] == "schedule-e2e"


from types import SimpleNamespace  # noqa: E402  (kept at module end for clarity)
