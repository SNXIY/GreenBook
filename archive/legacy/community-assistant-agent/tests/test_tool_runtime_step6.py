"""Phase 5 Step 6 — publication.publish_now unified command + reconcile."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
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
from app.publication_commands import (
    PublishNowRequest,
    PublishNowServices,
    PublishReconcile,
    execute_publish_now,
    handle_publish_now,
    reconcile_publication_status,
)
from app.schedule_repository import ScheduleRepository
from app.side_effect_ledger import SideEffectLedger, stable_hash
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
SHA = "a" * 64


def _ctx(run_id: str = "run-p6") -> ToolInvocationContext:
    return ToolInvocationContext(
        run_id=run_id,
        user_id="user-1",
        tenant_id="zhiguang",
        conversation_id="conv-1",
        request_id=f"req-{run_id}",
        operation_key=f"assistant-effect-{run_id}-1",
        idempotency_key=f"assistant-effect-{run_id}-1",
        attempt=1,
    )


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt", trace_id="t")


def _trace() -> ToolAttemptTrace:
    return ToolAttemptTrace(attempt=1, started_at=NOW)


class FakeCommunity:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.publish_bodies: list[dict[str, Any]] = []
        self.fail_publish: Exception | None = None
        self.published_ids: set[str] = set()
        self.drafts: dict[str, dict[str, Any]] = {
            "draft-1": {
                "id": "draft-1",
                "status": "draft",
                "contentSha256": SHA,
            }
        }
        self.posts: dict[str, dict[str, Any]] = {}
        self.issue_calls = 0

    async def issue_capability(self, **_k: Any) -> CapabilityGrant:
        self.issue_calls += 1
        return CapabilityGrant(
            token=f"tok-{self.issue_calls}",
            capability_id=f"cap-{self.issue_calls}",
            expires_at="2099-01-01T00:00:00Z",
        )

    async def get_own_draft(self, post_id: str, **_k: Any) -> dict[str, Any]:
        if post_id in self.published_ids or post_id in self.posts:
            request = httpx.Request("GET", f"http://t/posts/{post_id}/draft-content")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("not draft", request=request, response=response)
        draft = self.drafts.get(post_id)
        if draft is None:
            request = httpx.Request("GET", f"http://t/posts/{post_id}/draft-content")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("missing", request=request, response=response)
        return draft

    async def get_post(self, post_id: str, **_k: Any) -> dict[str, Any]:
        post = self.posts.get(post_id)
        if post is None:
            request = httpx.Request("GET", f"http://t/posts/{post_id}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("missing", request=request, response=response)
        return post

    async def publish_ai_draft(self, **kwargs: Any) -> dict[str, Any]:
        self.publish_calls += 1
        self.publish_bodies.append(dict(kwargs))
        if self.fail_publish is not None:
            raise self.fail_publish
        post_id = str(kwargs["post_id"])
        if post_id in self.published_ids:
            return {"id": post_id, "status": "published", "replayed": True}
        self.published_ids.add(post_id)
        self.posts[post_id] = {"id": post_id, "status": "published"}
        self.drafts.pop(post_id, None)
        return {"id": post_id, "status": "published", "replayed": False}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 's6.db').as_posix()}")
    await database.initialize()
    yield database
    await database.close()


async def _seed(database: Database, *, run_id: str, worker_id: str = "w1") -> None:
    async with database.sessions() as session, session.begin():
        if await session.get(Conversation, "conv-1") is None:
            session.add(Conversation(id="conv-1", user_id="user-1", title="s6"))
        session.add(
            Run(
                id=run_id,
                conversation_id="conv-1",
                user_id="user-1",
                tenant_id="zhiguang",
                prompt="publish",
                status="RUNNING",
                lease_owner=worker_id,
            )
        )
        session.add(
            RunStep(
                id=f"step-{run_id}-1",
                run_id=run_id,
                ordinal=1,
                kind="TOOL",
                tool_name="publication.publish_now",
                label="publish",
                status="RUNNING",
            )
        )


def _services(
    database: Database, *, community: FakeCommunity | None = None, worker_id: str = "w1"
) -> tuple[PublishNowServices, FakeCommunity]:
    community = community or FakeCommunity()

    async def issue(**kwargs: Any) -> CapabilityGrant:
        return await community.issue_capability(**kwargs)

    services = PublishNowServices(
        community=community,  # type: ignore[arg-type]
        ledger=SideEffectLedger(database, worker_id=worker_id),
        issue_capability=issue,
        consume_budget=AsyncMock(),
    )
    return services, community


@pytest.mark.asyncio
async def test_user_publish_now_success(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community = _services(db)
    trace = _trace()
    out = await handle_publish_now(
        services=services,
        arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.publish_now"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=trace,
        ordinal=1,
    )
    assert out["status"] == "published"
    assert out["post_id"] == "draft-1"
    assert community.publish_calls == 1
    assert trace.metadata["source"] == "USER"
    assert "token" not in trace.metadata
    async with db.sessions() as session:
        effect = await session.scalar(select(SideEffect))
    assert effect is not None and effect.status == "COMPLETED"


@pytest.mark.asyncio
async def test_completed_replay_no_second_publish(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community = _services(db)
    args = {"draft_id": "draft-1", "expected_content_sha256": SHA}
    first = await handle_publish_now(
        services=services,
        arguments=args,
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.publish_now"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    second = await handle_publish_now(
        services=services,
        arguments=args,
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.publish_now"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=_trace(),
        ordinal=1,
    )
    assert first["post_id"] == second["post_id"]
    assert community.publish_calls == 1
    assert second.get("_runtime_replayed") is True


@pytest.mark.asyncio
async def test_timeout_then_status_reconcile_published(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community = _services(db)
    community.fail_publish = TimeoutError("lost response")
    with pytest.raises(UnknownSideEffectError):
        await handle_publish_now(
            services=services,
            arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
            context=_ctx("run-a"),
            definition=tool_registry.get("publication.publish_now"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    # Simulate Java actually published despite timeout.
    community.fail_publish = None
    community.published_ids.add("draft-1")
    community.posts["draft-1"] = {"id": "draft-1", "status": "published"}
    community.drafts.pop("draft-1", None)
    community.publish_calls = 0
    out = await handle_publish_now(
        services=services,
        arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.publish_now"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=_trace(),
        ordinal=1,
    )
    assert out["post_id"] == "draft-1"
    assert out.get("_runtime_reconciled") is True
    assert community.publish_calls == 0  # reconcile only, no re-publish


@pytest.mark.asyncio
async def test_timeout_then_not_published_recovery_same_key(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community = _services(db)
    community.fail_publish = TimeoutError("lost")
    with pytest.raises(UnknownSideEffectError):
        await handle_publish_now(
            services=services,
            arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
            context=_ctx("run-a"),
            definition=tool_registry.get("publication.publish_now"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    community.fail_publish = None
    # Still a draft — recovery submit with same operation_key.
    out = await handle_publish_now(
        services=services,
        arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
        context=_ctx("run-a"),
        definition=tool_registry.get("publication.publish_now"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    assert out["status"] == "published"
    assert len({b["idempotency_key"] for b in community.publish_bodies}) == 1
    assert community.publish_bodies[-1]["idempotency_key"] == "assistant-effect-run-a-1"


@pytest.mark.asyncio
async def test_scheduler_uses_shared_command_and_action_key(db: Database) -> None:
    community = FakeCommunity()
    result = await execute_publish_now(
        community=community,  # type: ignore[arg-type]
        registry=tool_registry,
        request=PublishNowRequest(
            draft_id="draft-1",
            expected_content_sha256=SHA,
            creator_id="user-1",
            idempotency_key="schedule-op-key",
            capability_token="stored-tok",
            source="SCHEDULER",
            run_id="run-sched",
        ),
    )
    assert result.source == "SCHEDULER"
    assert result.output["post_id"] == "draft-1"
    assert community.publish_bodies[0]["idempotency_key"] == "schedule-op-key"
    assert result.idempotency_key_hash == stable_hash("schedule-op-key")


@pytest.mark.asyncio
async def test_claim_reclaims_expired_running(db: Database) -> None:
    from app.database import utc_now
    from app.worker import AgentWorker
    from app.config import Settings

    now = utc_now()
    schedules = ScheduleRepository(db, encrypt_token=lambda t: f"enc:{t}")
    snap, _ = await schedules.create_idempotent(
        run_id="run-x",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=now - timedelta(minutes=1),
        idempotency_key="due-key",
        capability_id="cap-1",
        capability_token_plain="tok",
        instruction="x",
    )
    async with db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, snap.action_id)
        assert action is not None
        action.status = "RUNNING"
        action.lease_owner = "dead-worker"
        action.lease_expires_at = now - timedelta(minutes=1)
        action.attempts = 1

    settings = MagicMock(spec=Settings)
    settings.lease_seconds = 30
    worker = object.__new__(AgentWorker)
    worker.database = db
    worker.worker_id = "w-new"
    worker.settings = settings

    claimed = await AgentWorker._claim_scheduled_action(worker)
    assert claimed == snap.action_id
    async with db.sessions() as session:
        action = await session.get(ScheduledAction, snap.action_id)
    assert action is not None
    assert action.lease_owner == "w-new"
    assert action.status == "RUNNING"
    assert action.attempts == 2


@pytest.mark.asyncio
async def test_prepublish_gate_blocks_java(db: Database) -> None:
    from app.worker import AgentWorker
    from app.config import Settings

    community = FakeCommunity()
    schedules = ScheduleRepository(db, encrypt_token=lambda t: f"enc:{t}")
    snap, _ = await schedules.create_idempotent(
        run_id="run-x",
        user_id="user-1",
        draft_id="draft-1",
        expected_content_sha256=SHA,
        run_at=NOW - timedelta(minutes=1),
        idempotency_key="due-key",
        capability_id="cap-1",
        capability_token_plain="tok",
        instruction="x",
    )
    async with db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, snap.action_id)
        assert action is not None
        action.status = "RUNNING"
        action.lease_owner = "other"
        action.lease_expires_at = NOW + timedelta(minutes=5)

    worker = object.__new__(AgentWorker)
    worker.database = db
    worker.schedule_repository = schedules
    worker.worker_id = "w1"
    worker.community = community
    worker.token_vault = MagicMock()
    worker.registry = tool_registry
    worker.settings = MagicMock(spec=Settings)

    await AgentWorker._execute_scheduled_action(worker, snap.action_id)
    assert community.publish_calls == 0


@pytest.mark.asyncio
async def test_business_4xx_fails_without_retry(db: Database) -> None:
    await _seed(db, run_id="run-a")
    services, community = _services(db)
    request = httpx.Request("POST", "http://t/publish")
    response = httpx.Response(400, request=request)
    community.fail_publish = httpx.HTTPStatusError(
        "bad", request=request, response=response
    )
    with pytest.raises(httpx.HTTPStatusError):
        await handle_publish_now(
            services=services,
            arguments={"draft_id": "draft-1", "expected_content_sha256": SHA},
            context=_ctx("run-a"),
            definition=tool_registry.get("publication.publish_now"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    async with db.sessions() as session:
        effect = await session.scalar(select(SideEffect))
    assert effect is not None and effect.status == "FAILED"


@pytest.mark.asyncio
async def test_reconcile_confirmed_not_published() -> None:
    community = FakeCommunity()

    async def issue(**_k: Any) -> CapabilityGrant:
        return await community.issue_capability()

    outcome, evidence = await reconcile_publication_status(
        community=community,  # type: ignore[arg-type]
        issue_capability=issue,
        credentials=_creds(),
        context=_ctx(),
        draft_id="draft-1",
        expected_content_sha256=SHA,
    )
    assert outcome == PublishReconcile.CONFIRMED_NOT_PUBLISHED
    assert evidence is not None


def test_tool_definition_migrated() -> None:
    d = tool_registry.get("publication.publish_now")
    assert d.transport == TransportType.BUILTIN
    assert d.idempotency_mode == IdempotencyMode.SIDE_EFFECT_REQUIRED
    assert d.retry_policy.max_attempts == 1
    assert "publication.publish_now" in MIGRATED_WRITE_TOOLS
    assert "publication.publish_now" not in LEGACY_BUILTIN_MIGRATION_BACKLOG


def test_no_moderation_and_shared_command_module_exists() -> None:
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / "app" / "publication_commands.py"
    ).read_text(encoding="utf-8")
    assert "execute_publish_now" in text
    assert "asyncio.Lock" not in text
