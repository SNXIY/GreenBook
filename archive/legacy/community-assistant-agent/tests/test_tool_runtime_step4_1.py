"""Phase 5 Step 4.1 — revision claims + Artifact recovery hardening."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.artifacts import (
    publish_step_artifact,
    set_artifact_before_insert_hook,
)
from app.clients import CapabilityGrant
from app.creator_tools import CreatorToolServices, handle_creator_tool
from app.database import (
    Artifact,
    ArtifactRelation,
    Conversation,
    Database,
    Run,
    RunStep,
    SideEffect,
    ToolExecutionReceipt,
)
from app.revision_claim import (
    REVISION_CLAIM_STATUSES,
    RevisionClaimConflict,
    revision_resource_id,
)
from app.side_effect_ledger import SideEffectLedger
from app.tool_dependency import DependencyPending
from app.tool_runtime import ToolCredentials, ToolInvocationContext
from app.tools import tool_registry
from tests.test_tool_runtime_step4 import FakeCommunity, FakeCreator

SHA_V1 = "a" * 64
SHA_V2 = "b" * 64


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt", trace_id="t")


def _ctx(run_id: str, user_id: str = "user-1") -> ToolInvocationContext:
    return ToolInvocationContext(
        run_id=run_id,
        user_id=user_id,
        tenant_id="zhiguang",
        conversation_id="conv-1",
        request_id=f"req-{run_id}",
        operation_key=f"assistant-effect-{run_id}-1",
        idempotency_key=f"assistant-effect-{run_id}-1",
        attempt=1,
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'r41.db').as_posix()}")
    await database.initialize()
    yield database
    await database.close()


async def _seed_run(
    database: Database,
    *,
    run_id: str,
    worker_id: str,
    ordinal: int = 1,
    tool: str = "creator.revise_draft",
) -> None:
    async with database.sessions() as session, session.begin():
        if run_id.endswith("1") or run_id == "run-a":
            existing = await session.get(Conversation, "conv-1")
            if existing is None:
                session.add(
                    Conversation(id="conv-1", user_id="user-1", title="r41")
                )
        session.add(
            Run(
                id=run_id,
                conversation_id="conv-1",
                user_id="user-1",
                tenant_id="zhiguang",
                prompt="revise",
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
                label="revise",
                status="RUNNING",
            )
        )


def _services(
    database: Database,
    *,
    worker_id: str,
    creator: FakeCreator | None = None,
    community: FakeCommunity | None = None,
) -> tuple[CreatorToolServices, FakeCreator, FakeCommunity]:
    creator = creator or FakeCreator()
    community = community or FakeCommunity()
    community.drafts["draft-java"] = {
        "id": "draft-java",
        "status": "READY",
        "contentSha256": SHA_V1,
        "title": "Java",
    }

    async def issue(**_k: Any) -> CapabilityGrant:
        return CapabilityGrant(
            token="c", capability_id="c1", expires_at="2099-01-01T00:00:00Z"
        )

    async def load_target(ctx: ToolInvocationContext) -> dict[str, Any]:
        return {
            "draft_id": "draft-java",
            "content_sha256": SHA_V1,
            "goal_id": "goal-1",
        }

    services = CreatorToolServices(
        creator=creator,  # type: ignore[arg-type]
        community=community,  # type: ignore[arg-type]
        ledger=SideEffectLedger(database, worker_id=worker_id),
        issue_capability=issue,
        consume_budget=AsyncMock(),
        load_content_target=load_target,
    )
    return services, creator, community


@pytest.mark.asyncio
async def test_concurrent_revise_only_one_claim(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    await _seed_run(db, run_id="run-b", worker_id=worker)
    services_a, creator, _ = _services(db, worker_id=worker)
    services_b, _, _ = _services(db, worker_id=worker, creator=creator)

    args = {
        "instruction": "加入实战",
        "draft_id": "draft-java",
        "expected_content_sha256": SHA_V1,
        "references": [],
    }

    async def revise(run_id: str, services: CreatorToolServices) -> str:
        try:
            await handle_creator_tool(
                services=services,
                tool_name="creator.revise_draft",
                arguments=args,
                context=_ctx(run_id),
                definition=tool_registry.get("creator.revise_draft"),
                credentials=_creds(),
                deadline_at=None,
                attempt_trace=None,
                ordinal=1,
            )
            return "ok"
        except DependencyPending:
            return "waiting"
        except LookupError:
            return "conflict"

    first, second = await asyncio.gather(
        revise("run-a", services_a),
        revise("run-b", services_b),
    )
    outcomes = {first, second}
    assert "waiting" in outcomes
    assert "conflict" in outcomes
    assert creator.submit_calls == 1
    async with db.sessions() as session:
        claiming = list(
            (
                await session.scalars(
                    select(SideEffect).where(
                        SideEffect.tool_name == "creator.revise_draft",
                        SideEffect.status.in_(sorted(REVISION_CLAIM_STATUSES)),
                    )
                )
            ).all()
        )
    assert len(claiming) == 1


@pytest.mark.asyncio
async def test_waiting_dependency_blocks_second_revise(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    await _seed_run(db, run_id="run-b", worker_id=worker)
    services, creator, _ = _services(db, worker_id=worker)
    args = {
        "instruction": "改",
        "draft_id": "draft-java",
        "expected_content_sha256": SHA_V1,
        "references": [],
    }
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-a"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 1
    with pytest.raises(LookupError, match="并发|修订"):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-b"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 1


@pytest.mark.asyncio
async def test_unknown_claim_blocks_second_revise(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    await _seed_run(db, run_id="run-b", worker_id=worker)
    services, creator, _ = _services(db, worker_id=worker)
    creator.fail_submit = True
    args = {
        "instruction": "改",
        "draft_id": "draft-java",
        "expected_content_sha256": SHA_V1,
        "references": [],
    }
    with pytest.raises(Exception):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-a"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    async with db.sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-a")
        )
    assert effect is not None
    assert effect.status == "UNKNOWN"
    creator.fail_submit = False
    with pytest.raises(LookupError):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-b"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    # Second run must not submit; first UNKNOWN may recover on its own key later.
    assert creator.submit_calls == 1


@pytest.mark.asyncio
async def test_failed_precheck_releases_claim_for_new_operation(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    await _seed_run(db, run_id="run-b", worker_id=worker)
    services, creator, community = _services(db, worker_id=worker)
    # Force pre-check conflict after claim: sha mismatch.
    community.drafts["draft-java"]["contentSha256"] = SHA_V2
    args = {
        "instruction": "改",
        "draft_id": "draft-java",
        "expected_content_sha256": SHA_V1,
        "references": [],
    }
    with pytest.raises(LookupError):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-a"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 0
    async with db.sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-a")
        )
    assert effect is not None
    assert effect.status == "FAILED"
    # Restore live sha and allow a new operation_key to claim.
    community.drafts["draft-java"]["contentSha256"] = SHA_V1
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-b"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 1


@pytest.mark.asyncio
async def test_different_base_sha_allows_new_revise(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    await _seed_run(db, run_id="run-b", worker_id=worker)
    services, creator, community = _services(db, worker_id=worker)
    # Simulate completed v1 claim occupying rev resource for SHA_V1.
    resource = revision_resource_id(
        user_id="user-1", draft_id="draft-java", base_content_sha256=SHA_V1
    )
    async with db.sessions() as session, session.begin():
        session.add(
            SideEffect(
                run_id="run-a",
                step_ordinal=1,
                tool_name="creator.revise_draft",
                operation_key="assistant-effect-run-a-1",
                request_hash="x",
                resource_id=resource,
                status="COMPLETED",
                result={
                    "output": {"draft_id": "draft-v2", "content_sha256": SHA_V2},
                    "claim": {
                        "source_draft_id": "draft-java",
                        "base_content_sha256": SHA_V1,
                    },
                },
            )
        )
    community.drafts["draft-v2"] = {
        "id": "draft-v2",
        "status": "READY",
        "contentSha256": SHA_V2,
    }

    async def load_v2(_ctx: ToolInvocationContext) -> dict[str, Any]:
        return {"draft_id": "draft-v2", "content_sha256": SHA_V2}

    services.load_content_target = load_v2
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments={
                "instruction": "再改",
                "draft_id": "draft-v2",
                "expected_content_sha256": SHA_V2,
                "references": [],
            },
            context=_ctx("run-b"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 1


@pytest.mark.asyncio
async def test_same_operation_key_resume_not_conflict(db: Database) -> None:
    worker = "w1"
    await _seed_run(db, run_id="run-a", worker_id=worker)
    services, creator, _ = _services(db, worker_id=worker)
    args = {
        "instruction": "改",
        "draft_id": "draft-java",
        "expected_content_sha256": SHA_V1,
        "references": [],
    }
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-a"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    with pytest.raises(DependencyPending):
        await handle_creator_tool(
            services=services,
            tool_name="creator.revise_draft",
            arguments=args,
            context=_ctx("run-a"),
            definition=tool_registry.get("creator.revise_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    assert creator.submit_calls == 1


@pytest.mark.asyncio
async def test_artifact_idempotent_after_insert_fault(db: Database) -> None:
    """SideEffect COMPLETED, Artifact INSERT fails once, replay creates one row."""

    worker = "w1"
    run_id = "run-art"
    await _seed_run(
        db, run_id=run_id, worker_id=worker, tool="creator.create_draft"
    )
    services, creator, _ = _services(db, worker_id=worker)
    with pytest.raises(DependencyPending) as pending:
        await handle_creator_tool(
            services=services,
            tool_name="creator.create_draft",
            arguments={"instruction": "写 Java", "references": []},
            context=_ctx(run_id),
            definition=tool_registry.get("creator.create_draft"),
            credentials=_creds(),
            deadline_at=None,
            attempt_trace=None,
            ordinal=1,
        )
    creator.tasks[pending.value.task_id]["status"] = "COMPLETED"
    output = await handle_creator_tool(
        services=services,
        tool_name="creator.create_draft",
        arguments={"instruction": "写 Java", "references": []},
        context=_ctx(run_id),
        definition=tool_registry.get("creator.create_draft"),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=None,
        ordinal=1,
    )
    assert creator.submit_calls == 1
    assert creator.handoff_calls == 1

    calls = {"n": 0}

    async def boom(**_k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected artifact insert failure")

    set_artifact_before_insert_hook(boom)
    try:
        async with db.sessions() as session, session.begin():
            step = await session.scalar(
                select(RunStep).where(RunStep.run_id == run_id)
            )
            assert step is not None
            with pytest.raises(RuntimeError, match="injected"):
                await publish_step_artifact(
                    session,
                    step=step,
                    output=output,
                    artifact_type="CONTENT_DRAFT",
                    provenance_key=f"assistant-effect-{run_id}-1",
                )
        # Replay — no second Creator call; single artifact.
        async with db.sessions() as session, session.begin():
            step = await session.scalar(
                select(RunStep).where(RunStep.run_id == run_id)
            )
            assert step is not None
            first = await publish_step_artifact(
                session,
                step=step,
                output=output,
                artifact_type="CONTENT_DRAFT",
                provenance_key=f"assistant-effect-{run_id}-1",
            )
            second = await publish_step_artifact(
                session,
                step=step,
                output=output,
                artifact_type="CONTENT_DRAFT",
                provenance_key=f"assistant-effect-{run_id}-1",
            )
            assert first.id == second.id
        async with db.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(Artifact))
        assert count == 1
        assert creator.submit_calls == 1
        assert creator.handoff_calls == 1
    finally:
        set_artifact_before_insert_hook(None)


@pytest.mark.asyncio
async def test_supersedes_relation_idempotent(db: Database) -> None:
    from app.worker import AgentWorker

    async with db.sessions() as session, session.begin():
        session.add(Conversation(id="conv-rel", user_id="user-1", title="r"))
        session.add(
            Run(
                id="run-rel",
                conversation_id="conv-rel",
                user_id="user-1",
                prompt="x",
                status="RUNNING",
            )
        )
        parent = Artifact(
            id="art-old",
            run_id="run-rel",
            step_id=None,
            task_key="t",
            agent_name="H",
            artifact_type="CONTENT_DRAFT",
            version=1,
            content={"draft_id": "d1"},
            content_hash="1" * 64,
            provenance_key="prov-old",
        )
        child = Artifact(
            id="art-new",
            run_id="run-rel",
            step_id=None,
            task_key="t",
            agent_name="H",
            artifact_type="CONTENT_DRAFT",
            version=2,
            content={"draft_id": "d2"},
            content_hash="2" * 64,
            provenance_key="prov-new",
        )
        session.add(parent)
        session.add(child)
    async with db.sessions() as session, session.begin():
        await AgentWorker._ensure_artifact_relation(
            session=session,
            source_artifact_id="art-new",
            target_artifact_id="art-old",
            relation_type="SUPERSEDES",
        )
        await AgentWorker._ensure_artifact_relation(
            session=session,
            source_artifact_id="art-new",
            target_artifact_id="art-old",
            relation_type="SUPERSEDES",
        )
    async with db.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(ArtifactRelation)
        )
    assert count == 1


def test_revision_claim_not_process_local_lock() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "app" / "revision_claim.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.Lock" not in text
    assert "threading.Lock" not in text
    assert "pg_advisory_xact_lock" in text


def test_claim_resource_id_stable() -> None:
    a = revision_resource_id(
        user_id="u", draft_id="d", base_content_sha256=SHA_V1
    )
    b = revision_resource_id(
        user_id="u", draft_id="d", base_content_sha256=SHA_V1
    )
    c = revision_resource_id(
        user_id="u", draft_id="d", base_content_sha256=SHA_V2
    )
    assert a == b
    assert a != c
