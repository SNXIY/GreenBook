"""Immediate-accept Agent Run tests (Phase 2.8).

POST persists a durable Run and returns 202 without waiting for the first-turn
LLM reasoning; the background runner claims ACCEPTED Runs atomically, recovers
crashed RUNNING runs by lease expiry, pushes real semantic-activity events
before any tool completes, and marks reasoning failures as FAILED instead of
leaking them into the HTTP response.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_api.runner import (
    EVENT_REASONING_STARTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_SEMANTIC_ACTION,
    RUN_ACCEPTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_WAITING_APPROVAL,
    RUN_WAITING,
    AgentRun,
    AgentRunEventStore,
    AgentRunner,
    AgentRunStore,
)


@pytest.fixture
def bind() -> Any:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    yield engine
    engine.dispose()


@pytest.fixture
def run_store(bind: Any) -> AgentRunStore:
    return AgentRunStore(bind, create_tables=True)


def _run(**overrides: Any) -> AgentRun:
    values = dict(
        run_id="run-1",
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="ten1",
        payload={"message": "hello", "session": {}},
    )
    values.update(overrides)
    return AgentRun(**values)


# ── §39 Run persisted before acceptance ─────────────────────────────────


def test_run_persisted_before_acceptance(run_store: AgentRunStore) -> None:
    run_store.create(_run())
    persisted = run_store.get("run-1")
    assert persisted is not None
    assert persisted.status == RUN_ACCEPTED
    assert persisted.payload["message"] == "hello"


def test_explicit_idempotency_key_reuses_existing_run(run_store: AgentRunStore) -> None:
    first = run_store.create(
        _run(
            run_id="run-key-1",
            idempotency_key="client-retry-1",
            payload={"message": "hello", "idempotency_key": "client-retry-1"},
        )
    )
    duplicate = run_store.create(
        _run(
            run_id="run-key-2",
            idempotency_key="client-retry-1",
            payload={"message": "hello", "idempotency_key": "client-retry-1"},
        )
    )
    assert first.run_id == "run-key-1"
    assert duplicate.run_id == first.run_id
    assert run_store.get_by_idempotency_key(
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="ten1",
        idempotency_key="client-retry-1",
    ).run_id == "run-key-1"
    assert len(run_store.list_recent(limit=10)) == 1


# ── §40 Runner claim is exclusive ───────────────────────────────────────


def test_runner_claim_is_exclusive(run_store: AgentRunStore) -> None:
    run_store.create(_run())
    first = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=1)
    second = run_store.claim(worker_id="worker-b", lease_seconds=300, limit=1)
    assert len(first) == 1
    assert first[0].claimed_by == "worker-a"
    assert second == [], "a second runner must not claim the same Run"


# ── §41 Crash recovery by lease expiry ──────────────────────────────────


def test_crash_recovery_reclaims_expired_run(run_store: AgentRunStore) -> None:
    run_store.create(_run())
    claimed = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=1)
    assert len(claimed) == 1
    # Expire the lease, then a new runner reclaims the same Run.
    run_store.mark_status("run-1", RUN_RUNNING)
    current = run_store.get("run-1")
    assert current is not None
    # Simulate an expired lease by writing a past lease_until.
    with run_store._bind.begin() as connection:
        connection.execute(
            run_store._table.update()
            .where(run_store._table.c.run_id == "run-1")
            .values(lease_until="2000-01-01T00:00:00+00:00")
        )
    recovered = run_store.claim(worker_id="worker-b", lease_seconds=300, limit=1)
    assert len(recovered) == 1
    assert recovered[0].run_id == "run-1"
    assert recovered[0].claimed_by == "worker-b"
    assert run_store.get("run-1") is not None  # one logical Run, no duplicate


# ── §43 Reasoning failure marks Run FAILED (not an HTTP 500) ────────────


def test_runner_reasoning_failure_marks_run_failed(run_store: AgentRunStore, bind: Any) -> None:
    run_store.create(_run())
    events = AgentRunEventStore()

    async def failing_execute(_run):
        raise RuntimeError("LLM unreachable")

    async def result_handler(_run, _result):
        raise AssertionError("result handler must not run on failure")

    runner = AgentRunner(
        run_store=run_store,
        event_store=events,
        execute=failing_execute,
        result_handler=result_handler,
        poll_interval_seconds=0.01,
    )
    claimed = run_store.claim(worker_id="w", lease_seconds=300, limit=1)
    import asyncio

    asyncio.run(runner._process(claimed[0]))
    assert run_store.get("run-1") is not None
    assert run_store.get("run-1").status == RUN_FAILED  # type: ignore[union-attr]
    event_types = [event.event_type for event in events.list_since("run-1")]
    assert EVENT_RUN_FAILED in event_types


def test_runner_success_marks_completed(run_store: AgentRunStore, bind: Any) -> None:
    run_store.create(_run())
    events = AgentRunEventStore()
    handled: list[str] = []

    class _Result:
        success = True
        status = "COMPLETED"
        error_code = ""
        error_message = ""
        error = ""

    async def execute(_run):
        return _Result()

    async def result_handler(_run, result):
        handled.append(_run.run_id)

    runner = AgentRunner(
        run_store=run_store,
        event_store=events,
        execute=execute,
        result_handler=result_handler,
        poll_interval_seconds=0.01,
    )
    claimed = run_store.claim(worker_id="w", lease_seconds=300, limit=1)
    import asyncio

    asyncio.run(runner._process(claimed[0]))
    assert handled == ["run-1"]
    assert run_store.get("run-1").status == RUN_COMPLETED  # type: ignore[union-attr]
    event_types = [event.event_type for event in events.list_since("run-1")]
    assert EVENT_RUN_COMPLETED in event_types


def test_runner_waiting_external_stays_running_until_all_mutations_verify(
    run_store: AgentRunStore,
) -> None:
    """A successful write acceptance is not a completed multi-mutation Run."""
    run_store.create(_run())

    class _Result:
        success = True
        status = "WAITING_EXTERNAL"
        execution_id = "execution-A"
        approval_id = ""
        error_code = ""
        error_message = ""
        error = ""
        # The status itself is the durable contract.  Older adapters may not
        # have copied execution_ids into partial_results yet.
        partial_results = {}

    async def execute(_run):
        return _Result()

    async def result_handler(_run, _result):
        return None

    runner = AgentRunner(
        run_store=run_store,
        event_store=AgentRunEventStore(),
        execute=execute,
        result_handler=result_handler,
        poll_interval_seconds=0.01,
    )
    claimed = run_store.claim(worker_id="w", lease_seconds=300, limit=1)
    import asyncio

    asyncio.run(runner._process(claimed[0]))
    persisted = run_store.get("run-1")
    assert persisted is not None
    assert persisted.status == RUN_RUNNING
    assert persisted.status != RUN_COMPLETED


def test_runner_waiting_approval_preserves_durable_identity(
    run_store: AgentRunStore,
) -> None:
    run_store.create(_run())

    class _Result:
        success = False
        status = "WAITING_HUMAN"
        execution_id = "execution-1"
        approval_id = "approval-1"
        error_code = "WAITING_HUMAN"
        error_message = "approval required"
        error = ""
        partial_results = {}

    async def execute(_run):
        return _Result()

    async def result_handler(_run, _result):
        return None

    runner = AgentRunner(
        run_store=run_store,
        event_store=AgentRunEventStore(),
        execute=execute,
        result_handler=result_handler,
        poll_interval_seconds=0.01,
    )
    claimed = run_store.claim(worker_id="w", lease_seconds=300, limit=1)
    import asyncio

    asyncio.run(runner._process(claimed[0]))
    persisted = run_store.get("run-1")
    assert persisted is not None
    assert persisted.status == RUN_WAITING_APPROVAL
    assert persisted.payload["execution_id"] == "execution-1"
    assert persisted.payload["approval_id"] == "approval-1"
    assert persisted.status != RUN_WAITING


# ── §42 Mid-turn follow-up serialization ────────────────────────────────


def test_follow_up_run_waits_behind_working_parent(run_store: AgentRunStore) -> None:
    run_store.create(_run(run_id="parent-1"))
    run_store.claim(worker_id="worker-a", lease_seconds=300, limit=1)
    run_store.create(_run(
        run_id="follow-1",
        payload={"message": "补充", "session": {}, "follow_up_of": "parent-1"},
    ))
    # While the parent is RUNNING, the follow-up must stay ACCEPTED (unclaimed).
    claimed = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=4)
    assert [run.run_id for run in claimed] == [], (
        "a follow-up must not race a still-working parent"
    )
    assert run_store.get("follow-1") is not None
    assert run_store.get("follow-1").status == RUN_ACCEPTED  # type: ignore[union-attr]
    # Once the parent reaches a terminal state, the follow-up is claimed.
    run_store.mark_status("parent-1", RUN_COMPLETED)
    claimed = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=4)
    assert [run.run_id for run in claimed] == ["follow-1"]


def test_follow_up_is_claimed_when_parent_was_already_terminal(run_store: AgentRunStore) -> None:
    run_store.create(_run(run_id="parent-1"))
    run_store.mark_status("parent-1", RUN_COMPLETED)
    run_store.create(_run(
        run_id="follow-1",
        payload={"message": "补充", "session": {}, "follow_up_of": "parent-1"},
    ))
    claimed = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=4)
    assert [run.run_id for run in claimed] == ["follow-1"]


def test_follow_up_wait_does_not_block_independent_runs(run_store: AgentRunStore) -> None:
    run_store.create(_run(run_id="parent-1"))
    run_store.claim(worker_id="worker-a", lease_seconds=300, limit=1)
    run_store.create(_run(
        run_id="follow-1",
        payload={"message": "补充", "session": {}, "follow_up_of": "parent-1"},
    ))
    run_store.create(_run(run_id="other-1", conversation_id="conv-2"))
    claimed = run_store.claim(worker_id="worker-a", lease_seconds=300, limit=4)
    assert [run.run_id for run in claimed] == ["other-1"]


# ── Event store cursor / SSE basis ──────────────────────────────────────


def test_event_store_since_cursor() -> None:
    events = AgentRunEventStore()
    first = events.append("run-1", EVENT_REASONING_STARTED, {"run_id": "run-1"})
    second = events.append("run-1", EVENT_SEMANTIC_ACTION, {"semantic_action": "SEARCH_COMMUNITY"})
    assert events.list_since("run-1", after_event_id=0) == [first, second]
    assert events.list_since("run-1", after_event_id=first.event_id) == [second]
    assert events.list_since("run-2") == []


def test_semantic_action_event_has_no_execution_dependency() -> None:
    events = AgentRunEventStore()
    event = events.append("run-1", EVENT_SEMANTIC_ACTION, {
        "semantic_action": "SEARCH_COMMUNITY",
        "goal_id": "g1",
    })
    # §7/§15: the first meaningful activity must not require execution_id.
    assert "execution_id" not in event.payload
