"""Phase 5 Step 5 — publication.schedule / cancel_schedule + scheduler gate."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.clients import CapabilityGrant
from app.database import (
    Conversation,
    Database,
    Run,
    RunStep,
    ScheduledAction,
    SideEffect,
)
from app.schedule_commands import (
    ScheduleCommandServices,
    cancel_schedule_command,
    handle_cancel_schedule,
    handle_create_schedule,
    register_schedule_command_handlers,
    revoke_after_cancel,
)
from app.schedule_repository import ScheduleRepository
from app.side_effect_ledger import SideEffectLedger
from app.tool_runtime import (
    LEGACY_BUILTIN_MIGRATION_BACKLOG,
    MIGRATED_WRITE_TOOLS,
    ToolAttemptTrace,
    ToolCredentials,
    ToolInvocationContext,
    UnknownSideEffectError,
)
from app.tools import IdempotencyMode, TransportType, tool_registry

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
RUN_AT = datetime(2026, 8, 5, 0, 10, tzinfo=timezone.utc)
SHA = "a" * 64
PLAIN = "plain-cap-token"


def _ctx(run_id: str = "run-s5", ordinal: int = 1) -> ToolInvocationContext:
    return ToolInvocationContext(
        run_id=run_id,
        user_id="user-1",
        tenant_id="zhiguang",
        conversation_id="conv-1",
        request_id=f"req-{run_id}",
        operation_key=f"assistant-effect-{run_id}-{ordinal}",
        idempotency_key=f"assistant-effect-{run_id}-{ordinal}",
        attempt=1,
    )


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt", trace_id="t")


def _trace() -> ToolAttemptTrace:
    return ToolAttemptTrace(attempt=1, started_at=NOW)


class FakeCommunity:
    def __init__(self) -> None:
        self.issue_calls = 0
        self.revoke_calls = 0
        self.issued: list[str] = []
        self.revoked: list[str] = []
        self.fail_issue: Exception | None = None
        self.definite_reject = False
        self.revoke_fail_ids: set[str] = set()
        self.publish_calls = 0

    async def issue_capability(self, **_k: Any) -> CapabilityGrant:
        self.issue_calls += 1
        if self.definite_reject:
            import httpx

            request = httpx.Request("POST", "http://test/capabilities")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "forbidden", request=request, response=response
            )
        if self.fail_issue is not None:
            raise self.fail_issue
        cap_id = f"cap-{self.issue_calls}"
        self.issued.append(cap_id)
        return CapabilityGrant(
            token=PLAIN, capability_id=cap_id, expires_at="2099-01-01T00:00:00Z"
        )

    async def revoke_capability(self, **kwargs: Any) -> None:
        self.revoke_calls += 1
        capability_id = str(kwargs["capability_id"])
        if capability_id in self.revoke_fail_ids:
            raise RuntimeError("revoke failed")
        self.revoked.append(capability_id)

    async def publish_ai_draft(self, **_k: Any) -> dict[str, Any]:
        self.publish_calls += 1
        return {"post_id": "p1", "status": "PUBLISHED", "draft_id": "draft-1"}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 's5.db').as_posix()}")
    await database.initialize()
    yield database
    await database.close()


async def _seed(
    database: Database,
    *,
    run_id: str,
    worker_id: str = "w1",
    tool: str = "publication.schedule",
    ordinal: int = 1,
) -> None:
    async with database.sessions() as session, session.begin():
        if await session.get(Conversation, "conv-1") is None:
            session.add(Conversation(id="conv-1", user_id="user-1", title="s5"))
        session.add(
            Run(
                id=run_id,
                conversation_id="conv-1",
                user_id="user-1",
                tenant_id="zhiguang",
                prompt="schedule",
                status="RUNNING",
                lease_owner=worker_id,
            )
        )
        session.add(
            RunStep(
                id=f"step-{run_id}-{ordinal}",
                run_id=run_id,
                ordinal=ordinal,
                kind="TOOL",
                tool_name=tool,
                label=tool,
                status="RUNNING",
            )
        )


def _services(
    database: Database, *, worker_id: str = "w1", community: FakeCommunity | None = None
) -> tuple[ScheduleCommandServices, FakeCommunity, ScheduleRepository]:
    community = community or FakeCommunity()
    schedules = ScheduleRepository(database, encrypt_token=lambda t: f"enc:{t}")
    services = ScheduleCommandServices(
        schedules=schedules,
        ledger=SideEffectLedger(database, worker_id=worker_id),
        community=community,  # type: ignore[arg-type]
        consume_budget=AsyncMock(),
        run_prompt_loader=AsyncMock(return_value="prompt"),
    )
    return services, community, schedules


@pytest.mark.asyncio
async def test_schedule_happy_path(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community, _ = _services(db)
    out = await handle_create_schedule(
        services=services,
        arguments={
            "run_at": RUN_AT.isoformat(),
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
        },
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=_trace(),
        ordinal=1,
    )
    assert out["status"] == "SCHEDULED"
    assert out["draft_id"] == "draft-1"
    assert community.issue_calls == 1
    async with db.sessions() as session:
        row = await session.scalar(select(ScheduledAction))
    assert row is not None
    assert row.idempotency_key == "assistant-effect-run-a-1"
    assert row.capability_token == f"enc:{PLAIN}"


@pytest.mark.asyncio
async def test_schedule_completed_replay_no_reissue(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community, _ = _services(db)
    args = {
        "run_at": RUN_AT.isoformat(),
        "draft_id": "draft-1",
        "expected_content_sha256": SHA,
    }
    first = await handle_create_schedule(
        services=services,
        arguments=args,
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    second = await handle_create_schedule(
        services=services,
        arguments=args,
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=_trace(),
        ordinal=1,
    )
    assert first["action_id"] == second["action_id"]
    assert community.issue_calls == 1
    assert second.get("_runtime_replayed") is True


@pytest.mark.asyncio
async def test_schedule_resume_after_insert_before_finish_no_reissue(
    db: Database,
) -> None:
    """ScheduledAction exists; SideEffect not COMPLETED → resume without re-issue."""

    await _seed(db, run_id="run-a")
    services, community, schedules = _services(db)
    op_key = "assistant-effect-run-a-1"
    snap, _ = await schedules.create_idempotent(
        run_id="run-a",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=RUN_AT,
        idempotency_key=op_key,
        capability_id="cap-existing",
        capability_token_plain=PLAIN,
    )
    # Prepare SideEffect as IN_FLIGHT without COMPLETED.
    record = await services.ledger.prepare(
        run_id="run-a",
        ordinal=1,
        tool_name="publication.schedule",
        arguments={
            "run_at": RUN_AT.isoformat(),
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
        },
        resource_id="post:draft-1",
    )
    assert record.status != "COMPLETED"
    out = await handle_create_schedule(
        services=services,
        arguments={
            "run_at": RUN_AT.isoformat(),
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
        },
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=_trace(),
        ordinal=1,
    )
    assert out["action_id"] == snap.action_id
    assert community.issue_calls == 0
    async with db.sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.operation_key == op_key)
        )
        count = await session.scalar(select(func.count()).select_from(ScheduledAction))
    assert effect is not None and effect.status == "COMPLETED"
    assert count == 1


@pytest.mark.asyncio
async def test_capability_orphan_cleanup_pending(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community, schedules = _services(db)
    community.revoke_fail_ids.add("cap-1")

    async def boom(**_k: Any):
        raise RuntimeError("insert failed")

    schedules.create_idempotent = boom  # type: ignore[method-assign]
    with pytest.raises(UnknownSideEffectError):
        await handle_create_schedule(
            services=services,
            arguments={
                "run_at": RUN_AT.isoformat(),
                "draft_id": "draft-1",
                "expected_content_sha256": SHA,
            },
            context=_ctx("run-a"),
            definition=tool_registry.get("publication.schedule"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert community.issue_calls == 1
    async with db.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(ScheduledAction))
        effect = await session.scalar(select(SideEffect))
    assert count == 0
    assert effect is not None
    assert effect.status == "UNKNOWN"
    ledger = (effect.result or {}).get("ledger") or effect.result or {}
    assert ledger.get("capability_cleanup_pending") is True
    # Resume must not re-issue.
    with pytest.raises(UnknownSideEffectError):
        await handle_create_schedule(
            services=services,
            arguments={
                "run_at": RUN_AT.isoformat(),
                "draft_id": "draft-1",
                "expected_content_sha256": SHA,
            },
            context=_ctx("run-a"),
            definition=tool_registry.get("publication.schedule"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert community.issue_calls == 1


@pytest.mark.asyncio
async def test_different_operation_keys_allow_multiple_schedules(db: Database) -> None:
    await _seed(db, run_id="run-a", ordinal=1)
    await _seed(db, run_id="run-b", ordinal=1)
    services, community, _ = _services(db)
    args = {
        "run_at": RUN_AT.isoformat(),
        "draft_id": "draft-1",
        "expected_content_sha256": SHA,
    }
    a = await handle_create_schedule(
        services=services,
        arguments=args,
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    b = await handle_create_schedule(
        services=services,
        arguments=args,
        context=_ctx("run-b"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    assert a["action_id"] != b["action_id"]
    assert community.issue_calls == 2


@pytest.mark.asyncio
async def test_cancel_scheduled_and_idempotent_recancel(db: Database) -> None:
    await _seed(db, run_id="run-a", tool="publication.schedule")
    await _seed(db, run_id="run-c", tool="publication.cancel_schedule")
    await _seed(db, run_id="run-d", tool="publication.cancel_schedule")
    services, community, _ = _services(db)
    created = await handle_create_schedule(
        services=services,
        arguments={
            "run_at": RUN_AT.isoformat(),
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
        },
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    trace = _trace()
    out = await handle_cancel_schedule(
        services=services,
        arguments={"action_id": created["action_id"]},
        context=_ctx("run-c"),
        definition=tool_registry.get("publication.cancel_schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=trace,
        ordinal=1,
    )
    assert out["status"] == "CANCELLED"
    assert community.revoke_calls == 1
    assert trace.metadata.get("noop") is False

    trace2 = _trace()
    again = await handle_cancel_schedule(
        services=services,
        arguments={"action_id": created["action_id"]},
        context=_ctx("run-d"),
        definition=tool_registry.get("publication.cancel_schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=trace2,
        ordinal=1,
    )
    assert again["status"] == "CANCELLED"
    assert trace2.metadata.get("noop") is True
    assert trace2.metadata.get("already_cancelled") is True
    assert community.revoke_calls == 1  # no second revoke


@pytest.mark.asyncio
async def test_cancel_running_conflicts(db: Database) -> None:
    await _seed(db, run_id="run-c", tool="publication.cancel_schedule")
    services, _, schedules = _services(db)
    snap, _ = await schedules.create_idempotent(
        run_id="run-c",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=RUN_AT,
        idempotency_key="k1",
        capability_id="cap-1",
        capability_token_plain=PLAIN,
    )
    async with db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, snap.action_id)
        assert action is not None
        action.status = "RUNNING"
        action.lease_owner = "scheduler-1"
        action.lease_expires_at = NOW + timedelta(minutes=5)
    with pytest.raises(LookupError, match="正在执行|不能取消"):
        await handle_cancel_schedule(
            services=services,
            arguments={"action_id": snap.action_id},
            context=_ctx("run-c"),
            definition=tool_registry.get("publication.cancel_schedule"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )


@pytest.mark.asyncio
async def test_cancel_revoke_failure_still_succeeds(db: Database) -> None:
    await _seed(db, run_id="run-a")
    await _seed(db, run_id="run-c", tool="publication.cancel_schedule")
    services, community, _ = _services(db)
    created = await handle_create_schedule(
        services=services,
        arguments={
            "run_at": RUN_AT.isoformat(),
            "draft_id": "draft-1",
            "expected_content_sha256": SHA,
        },
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    community.revoke_fail_ids.add(community.issued[0])
    trace = _trace()
    out = await handle_cancel_schedule(
        services=services,
        arguments={"action_id": created["action_id"]},
        context=_ctx("run-c"),
        definition=tool_registry.get("publication.cancel_schedule"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=trace,
        ordinal=1,
    )
    assert out["status"] == "CANCELLED"
    assert trace.metadata.get("capability_cleanup_pending") is True
    async with db.sessions() as session:
        action = await session.get(ScheduledAction, created["action_id"])
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-c")
        )
    assert action is not None and action.status == "CANCELLED"
    assert effect is not None and effect.status == "COMPLETED"
    ledger = (effect.result or {}).get("ledger") or {}
    assert ledger.get("capability_cleanup_pending") is True


@pytest.mark.asyncio
async def test_http_and_tool_cancel_share_command(db: Database) -> None:
    services, community, schedules = _services(db)
    snap, _ = await schedules.create_idempotent(
        run_id="run-x",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=RUN_AT,
        idempotency_key="http-tool",
        capability_id="cap-shared",
        capability_token_plain=PLAIN,
    )
    community.revoke_fail_ids.add("cap-shared")
    tool_result = await cancel_schedule_command(
        schedules, action_id=snap.action_id, user_id="user-1"
    )
    assert tool_result.output["status"] == "CANCELLED"
    cleanup = await revoke_after_cancel(
        community,  # type: ignore[arg-type]
        access_token="jwt",
        capability_id=tool_result.old_capability_id,
    )
    assert cleanup["capability_cleanup_pending"] is True
    # Second path (HTTP-equivalent) is noop success.
    again = await cancel_schedule_command(
        schedules, action_id=snap.action_id, user_id="user-1"
    )
    assert again.noop is True
    assert again.already_cancelled is True


@pytest.mark.asyncio
async def test_scheduler_prepublish_gate_blocks_java(db: Database) -> None:
    from app.worker import AgentWorker
    from app.config import Settings
    from unittest.mock import MagicMock

    services, community, schedules = _services(db)
    snap, _ = await schedules.create_idempotent(
        run_id="run-x",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=NOW - timedelta(minutes=1),
        idempotency_key="due-1",
        capability_id="cap-1",
        capability_token_plain=PLAIN,
    )
    async with db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, snap.action_id)
        assert action is not None
        action.status = "RUNNING"
        action.lease_owner = "other-worker"
        action.lease_expires_at = NOW + timedelta(minutes=5)

    settings = MagicMock(spec=Settings)
    settings.lease_seconds = 30
    worker = object.__new__(AgentWorker)
    worker.database = db
    worker.schedule_repository = schedules
    worker.worker_id = "w1"
    worker.community = community
    worker.token_vault = MagicMock()
    worker.token_vault.decrypt = lambda t: t.replace("enc:", "")
    worker.registry = tool_registry
    worker.settings = settings

    await AgentWorker._execute_scheduled_action(worker, snap.action_id)
    assert community.publish_calls == 0
    async with db.sessions() as session:
        action = await session.get(ScheduledAction, snap.action_id)
    assert action is not None
    assert action.status == "RUNNING"  # not completed by stale worker


@pytest.mark.asyncio
async def test_cancel_vs_claim_race(db: Database) -> None:
    """Cancel before claim wins; cancel after RUNNING fails."""

    _, _, schedules = _services(db)
    snap, _ = await schedules.create_idempotent(
        run_id="run-x",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=NOW - timedelta(seconds=1),
        idempotency_key="race-1",
        capability_id="cap-1",
        capability_token_plain=PLAIN,
    )
    cancelled = await cancel_schedule_command(
        schedules, action_id=snap.action_id, user_id="user-1"
    )
    assert cancelled.outcome == "cancelled"
    async with db.sessions() as session, session.begin():
        action = await session.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.id == snap.action_id)
            .with_for_update()
        )
        assert action is not None
        assert action.status == "CANCELLED"
        # Claim would skip CANCELLED rows.
        assert action.status not in {"SCHEDULED", "RETRYING"}

    snap2, _ = await schedules.create_idempotent(
        run_id="run-y",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=NOW - timedelta(seconds=1),
        idempotency_key="race-2",
        capability_id="cap-2",
        capability_token_plain=PLAIN,
    )
    async with db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, snap2.action_id)
        assert action is not None
        action.status = "RUNNING"
        action.lease_owner = "sched"
        action.lease_expires_at = NOW + timedelta(minutes=5)
    with pytest.raises(LookupError):
        await cancel_schedule_command(
            schedules, action_id=snap2.action_id, user_id="user-1"
        )


def test_tool_definitions_migrated() -> None:
    for name in ("publication.schedule", "publication.cancel_schedule"):
        d = tool_registry.get(name)
        assert d.transport == TransportType.BUILTIN
        assert d.idempotency_mode == IdempotencyMode.SIDE_EFFECT_REQUIRED
        assert d.retry_policy.max_attempts == 1
        assert name in MIGRATED_WRITE_TOOLS
        assert name not in LEGACY_BUILTIN_MIGRATION_BACKLOG


def test_no_process_local_lock_in_schedule_commands() -> None:
    text = (
        __import__("pathlib")
        .Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("app", "schedule_commands.py")
        .read_text(encoding="utf-8")
    )
    assert "asyncio.Lock" not in text
    assert "threading.Lock" not in text
