"""Phase 5 Step 3 — publication.update_schedule Runtime write + reconcile."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.clients import CapabilityGrant
from app.database import (
    Conversation,
    Database,
    Run,
    RunStep,
    ScheduledAction,
    SideEffect,
)
from app.read_tools import handle_search_posts
from app.schedule_repository import ScheduleRepository
from app.side_effect_ledger import SideEffectLedger
from app.tool_runtime import (
    LEGACY_BUILTIN_MIGRATION_BACKLOG,
    MIGRATED_WRITE_TOOLS,
    ToolCredentials,
    ToolErrorCode,
    ToolInvocationContext,
    ToolInvocationStatus,
    ToolRuntime,
)
from app.tools import CapabilityBudget, IdempotencyMode, TransportType, tool_registry
from app.worker import AgentWorker
from app.write_tools import UpdateScheduleServices, register_update_schedule_handler

NOW = datetime.now(timezone.utc)
JAVA_RUN_AT = NOW + timedelta(days=1)
FIT_RUN_AT = NOW + timedelta(days=1, hours=2)
SHA_A = "a" * 64
SHA_B = "b" * 64
PLAIN_TOKEN = "plain-capability-token-do-not-persist"


def _ctx(**overrides: Any) -> ToolInvocationContext:
    payload = {
        "run_id": "run-write-1",
        "user_id": "user-1",
        "tenant_id": "zhiguang",
        "conversation_id": "conv-1",
        "request_id": "req-1",
        "operation_key": "assistant-effect-run-write-1-1",
        "idempotency_key": "assistant-effect-run-write-1-1",
        "attempt": 1,
    }
    payload.update(overrides)
    return ToolInvocationContext(**payload)


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt-test", trace_id="trace-1")


class FakeCommunity:
    def __init__(self) -> None:
        self.issue_calls = 0
        self.issued: list[CapabilityGrant] = []
        self.revoked: list[str] = []
        self.fail_issue = False
        self.revoke_fail_ids: set[str] = set()

    async def issue_capability(self, **kwargs: Any) -> CapabilityGrant:
        self.issue_calls += 1
        if self.fail_issue:
            raise TimeoutError("capability issue timed out")
        grant = CapabilityGrant(
            token=PLAIN_TOKEN,
            capability_id=f"cap-new-{self.issue_calls}",
            expires_at="2099-01-01T00:00:00Z",
        )
        self.issued.append(grant)
        return grant

    async def revoke_capability(self, **kwargs: Any) -> None:
        capability_id = str(kwargs["capability_id"])
        if capability_id in self.revoke_fail_ids:
            raise RuntimeError(f"revoke failed for {capability_id}")
        self.revoked.append(capability_id)


@pytest_asyncio.fixture
async def harness(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'step3.db').as_posix()}")
    await db.initialize()
    worker_id = "worker-step3"
    async with db.sessions() as session, session.begin():
        session.add(Conversation(id="conv-1", user_id="user-1", title="step3"))
        session.add(
            Run(
                id="run-write-1",
                conversation_id="conv-1",
                user_id="user-1",
                tenant_id="zhiguang",
                prompt="update schedule",
                status="RUNNING",
                lease_owner=worker_id,
            )
        )
        session.add(
            RunStep(
                id="step-1",
                run_id="run-write-1",
                ordinal=1,
                kind="TOOL",
                tool_name="publication.update_schedule",
                label="update",
                status="RUNNING",
            )
        )
        session.add(
            ScheduledAction(
                id="sched-java",
                run_id="run-seed",
                user_id="user-1",
                draft_id="draft-java",
                expected_content_sha256=SHA_A,
                instruction="Java",
                run_at=JAVA_RUN_AT,
                status="SCHEDULED",
                idempotency_key="seed-java",
                capability_id="cap-old",
                capability_token="enc-old",
            )
        )
        session.add(
            ScheduledAction(
                id="sched-fit",
                run_id="run-seed",
                user_id="user-1",
                draft_id="draft-fit",
                expected_content_sha256=SHA_A,
                instruction="Fit",
                run_at=FIT_RUN_AT,
                status="SCHEDULED",
                idempotency_key="seed-fit",
                capability_id="cap-fit",
                capability_token="enc-fit",
            )
        )

    schedules = ScheduleRepository(db, encrypt_token=lambda t: f"enc:{t}")
    ledger = SideEffectLedger(db, worker_id=worker_id)
    community = FakeCommunity()
    runtime = ToolRuntime(definitions=tool_registry)
    register_update_schedule_handler(
        runtime,
        services=UpdateScheduleServices(
            schedules=schedules,
            ledger=ledger,
            community=community,  # type: ignore[arg-type]
            publication_min_lead_seconds=15,
            publication_max_schedule_days=6,
            consume_budget=AsyncMock(),
        ),
    )
    yield {
        "db": db,
        "runtime": runtime,
        "community": community,
        "schedules": schedules,
        "ledger": ledger,
        "worker_id": worker_id,
    }
    await db.close()


async def _invoke(
    harness: dict[str, Any],
    arguments: dict[str, Any],
    *,
    ordinal: int = 1,
    run_id: str = "run-write-1",
) -> Any:
    return await harness["runtime"].invoke(
        tool_name="publication.update_schedule",
        arguments=arguments,
        context=_ctx(run_id=run_id),
        credentials=_creds(),
        ordinal=ordinal,
        skip_policy=True,
        raise_on_failure=False,
    )


def _assert_no_plaintext(payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    assert PLAIN_TOKEN not in text
    assert "plain-capability" not in text


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_run_at_only(harness: dict[str, Any]) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.output["action_id"] == "sched-java"
    assert result.output["draft_id"] == "draft-java"
    assert result.output["status"] == "SCHEDULED"
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT + timedelta(minutes=10)
    assert snap.draft_id == "draft-java"
    assert harness["community"].issue_calls == 1
    assert "cap-old" in harness["community"].revoked


@pytest.mark.asyncio
async def test_rebind_draft_only(harness: dict[str, Any]) -> None:
    result = await _invoke(
        harness,
        {
            "action_id": "sched-java",
            "draft_id": "draft-java-v2",
            "expected_content_sha256": SHA_B,
        },
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.output["draft_id"] == "draft-java-v2"
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT
    assert snap.expected_content_sha256 == SHA_B
    assert snap.capability_id == "cap-new-1"


@pytest.mark.asyncio
async def test_atomic_time_and_draft(harness: dict[str, Any]) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=30)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(
        harness,
        {
            "action_id": "sched-java",
            "run_at": new_run_at,
            "draft_id": "draft-java-v3",
            "expected_content_sha256": SHA_B,
        },
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT + timedelta(minutes=30)
    assert snap.draft_id == "draft-java-v3"
    assert snap.expected_content_sha256 == SHA_B


@pytest.mark.asyncio
async def test_completed_replay_skips_upstream_and_db_write(
    harness: dict[str, Any],
) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    first = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert first.status == ToolInvocationStatus.SUCCESS
    assert first.replayed is False
    issue_before = harness["community"].issue_calls
    revoke_before = list(harness["community"].revoked)
    snap_before = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    second = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert second.status == ToolInvocationStatus.SUCCESS
    assert second.replayed is True
    assert harness["community"].issue_calls == issue_before
    assert harness["community"].revoked == revoke_before
    snap_after = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap_after == snap_before


# ---------------------------------------------------------------------------
# Capability / local failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_issue_failure_leaves_schedule_untouched(
    harness: dict[str, Any],
) -> None:
    harness["community"].fail_issue = True
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert result.status == ToolInvocationStatus.UNKNOWN
    assert result.error_code == ToolErrorCode.UNKNOWN_SIDE_EFFECT.value
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT
    assert snap.capability_id == "cap-old"
    assert harness["community"].issue_calls == 1


@pytest.mark.asyncio
async def test_local_cas_failure_revokes_new_capability(
    harness: dict[str, Any],
) -> None:
    schedules: ScheduleRepository = harness["schedules"]
    original = schedules.cas_update

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("simulated local tx failure")

    schedules.cas_update = boom  # type: ignore[method-assign]
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    schedules.cas_update = original  # type: ignore[method-assign]
    assert result.status == ToolInvocationStatus.UNKNOWN
    assert "cap-new-1" in harness["community"].revoked
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT


@pytest.mark.asyncio
async def test_old_capability_revoke_failure_still_succeeds(
    harness: dict[str, Any],
) -> None:
    harness["community"].revoke_fail_ids.add("cap-old")
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert result.status == ToolInvocationStatus.SUCCESS
    trace = harness["runtime"].last_trace(result.trace_id)
    assert trace is not None
    assert trace.attempts[0].metadata.get("capability_cleanup_pending") is True
    async with harness["db"].sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-write-1")
        )
    assert effect is not None
    assert effect.status == "COMPLETED"
    ledger = (effect.result or {}).get("ledger") or {}
    assert ledger.get("capability_cleanup_pending") is True
    _assert_no_plaintext(effect.result)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_confirmed_applied_after_crash_before_finish(
    harness: dict[str, Any],
) -> None:
    new_run_at = JAVA_RUN_AT + timedelta(minutes=10)
    # Simulate: local CAS committed, SideEffect left IN_FLIGHT (crash before COMPLETED).
    async with harness["db"].sessions() as session, session.begin():
        action = await session.get(ScheduledAction, "sched-java")
        assert action is not None
        before = {
            "action_id": action.id,
            "draft_id": action.draft_id,
            "expected_content_sha256": action.expected_content_sha256,
            "run_at": action.run_at.isoformat(),
            "status": action.status,
            "capability_id": action.capability_id,
        }
        action.run_at = new_run_at
        action.capability_id = "cap-applied"
        action.capability_token = "enc:applied"
        expected = {
            "run_at": new_run_at.isoformat(),
            "draft_id": "draft-java",
            "expected_content_sha256": SHA_A,
            "status": "SCHEDULED",
        }
        session.add(
            SideEffect(
                run_id="run-write-1",
                step_ordinal=1,
                tool_name="publication.update_schedule",
                operation_key="assistant-effect-run-write-1-1",
                request_hash="placeholder",
                resource_id="schedule:sched-java",
                status="IN_FLIGHT",
                attempts=1,
                result={
                    "ledger": {
                        "action_id": "sched-java",
                        "before": before,
                        "expected": expected,
                        "issued_capability_id": "cap-applied",
                    }
                },
            )
        )
    # Align request_hash with real prepare hash by first failing prepare mismatch —
    # instead re-seed via ledger.prepare path: delete and let prepare recreate? 
    # Easier: compute hash by calling prepare after fixing status, or update hash.
    from app.side_effect_ledger import stable_hash

    args = {
        "action_id": "sched-java",
        "run_at": new_run_at.isoformat().replace("+00:00", "Z"),
    }
    req_hash = stable_hash(
        {"tool": "publication.update_schedule", "arguments": args}
    )
    async with harness["db"].sessions() as session, session.begin():
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-write-1")
        )
        assert effect is not None
        effect.request_hash = req_hash

    issue_before = harness["community"].issue_calls
    result = await _invoke(harness, args)
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.replayed is True
    assert harness["community"].issue_calls == issue_before
    async with harness["db"].sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-write-1")
        )
    assert effect is not None
    assert effect.status == "COMPLETED"


@pytest.mark.asyncio
async def test_reconcile_confirmed_not_applied(harness: dict[str, Any]) -> None:
    from app.side_effect_ledger import stable_hash

    new_run_at = JAVA_RUN_AT + timedelta(minutes=10)
    args = {
        "action_id": "sched-java",
        "run_at": new_run_at.isoformat().replace("+00:00", "Z"),
    }
    async with harness["db"].sessions() as session, session.begin():
        action = await session.get(ScheduledAction, "sched-java")
        assert action is not None
        before = {
            "action_id": action.id,
            "draft_id": action.draft_id,
            "expected_content_sha256": action.expected_content_sha256,
            "run_at": action.run_at.isoformat(),
            "status": action.status,
            "capability_id": action.capability_id,
        }
        expected = {
            "run_at": new_run_at.isoformat(),
            "draft_id": "draft-java",
            "expected_content_sha256": SHA_A,
            "status": "SCHEDULED",
        }
        session.add(
            SideEffect(
                run_id="run-write-1",
                step_ordinal=1,
                tool_name="publication.update_schedule",
                operation_key="assistant-effect-run-write-1-1",
                request_hash=stable_hash(
                    {"tool": "publication.update_schedule", "arguments": args}
                ),
                resource_id="schedule:sched-java",
                status="UNKNOWN",
                attempts=1,
                result={
                    "ledger": {
                        "action_id": "sched-java",
                        "before": before,
                        "expected": expected,
                        "issued_capability_id": "cap-leaked",
                    }
                },
            )
        )
    result = await _invoke(harness, args)
    assert result.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert "cap-leaked" in harness["community"].revoked
    assert harness["community"].issue_calls == 0
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == JAVA_RUN_AT


@pytest.mark.asyncio
async def test_reconcile_conflicting_state(harness: dict[str, Any]) -> None:
    from app.side_effect_ledger import stable_hash

    new_run_at = JAVA_RUN_AT + timedelta(minutes=10)
    third = JAVA_RUN_AT + timedelta(minutes=20)
    args = {
        "action_id": "sched-java",
        "run_at": new_run_at.isoformat().replace("+00:00", "Z"),
    }
    async with harness["db"].sessions() as session, session.begin():
        action = await session.get(ScheduledAction, "sched-java")
        assert action is not None
        before = {
            "action_id": action.id,
            "draft_id": action.draft_id,
            "expected_content_sha256": action.expected_content_sha256,
            "run_at": action.run_at.isoformat(),
            "status": action.status,
            "capability_id": action.capability_id,
        }
        action.run_at = third
        expected = {
            "run_at": new_run_at.isoformat(),
            "draft_id": "draft-java",
            "expected_content_sha256": SHA_A,
            "status": "SCHEDULED",
        }
        session.add(
            SideEffect(
                run_id="run-write-1",
                step_ordinal=1,
                tool_name="publication.update_schedule",
                operation_key="assistant-effect-run-write-1-1",
                request_hash=stable_hash(
                    {"tool": "publication.update_schedule", "arguments": args}
                ),
                resource_id="schedule:sched-java",
                status="UNKNOWN",
                attempts=1,
                result={
                    "ledger": {
                        "action_id": "sched-java",
                        "before": before,
                        "expected": expected,
                        "issued_capability_id": "cap-x",
                    }
                },
            )
        )
    result = await _invoke(harness, args)
    assert result.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert result.error_code == ToolErrorCode.CONFLICT.value
    assert harness["community"].issue_calls == 0
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    assert snap.run_at == third


@pytest.mark.asyncio
async def test_unknown_restart_reconciles_without_reissue(
    harness: dict[str, Any],
) -> None:
    """Issue succeeded, local write not committed → NOT_APPLIED, no re-issue."""

    from app.side_effect_ledger import stable_hash

    new_run_at = JAVA_RUN_AT + timedelta(minutes=10)
    args = {
        "action_id": "sched-java",
        "run_at": new_run_at.isoformat().replace("+00:00", "Z"),
    }
    async with harness["db"].sessions() as session, session.begin():
        action = await session.get(ScheduledAction, "sched-java")
        assert action is not None
        before = {
            "action_id": action.id,
            "draft_id": action.draft_id,
            "expected_content_sha256": action.expected_content_sha256,
            "run_at": action.run_at.isoformat(),
            "status": action.status,
            "capability_id": action.capability_id,
        }
        expected = {
            "run_at": new_run_at.isoformat(),
            "draft_id": "draft-java",
            "expected_content_sha256": SHA_A,
            "status": "SCHEDULED",
        }
        session.add(
            SideEffect(
                run_id="run-write-1",
                step_ordinal=1,
                tool_name="publication.update_schedule",
                operation_key="assistant-effect-run-write-1-1",
                request_hash=stable_hash(
                    {"tool": "publication.update_schedule", "arguments": args}
                ),
                resource_id="schedule:sched-java",
                status="UNKNOWN",
                attempts=1,
                result={
                    "ledger": {
                        "action_id": "sched-java",
                        "before": before,
                        "expected": expected,
                        "issued_capability_id": "cap-issued-only",
                    }
                },
            )
        )
    result = await _invoke(harness, args)
    assert result.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert harness["community"].issue_calls == 0
    assert "cap-issued-only" in harness["community"].revoked


@pytest.mark.asyncio
async def test_concurrent_second_update_conflicts(
    harness: dict[str, Any],
) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    first = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert first.status == ToolInvocationStatus.SUCCESS

    # New ordinal + run step for a competing write with stale before semantics:
    # CAS against current row while another process already changed it.
    async with harness["db"].sessions() as session, session.begin():
        session.add(
            RunStep(
                id="step-2",
                run_id="run-write-1",
                ordinal=2,
                kind="TOOL",
                tool_name="publication.update_schedule",
                label="update-2",
                status="RUNNING",
            )
        )
    # Force CAS conflict: mutate schedule after reading via wrapper.
    schedules: ScheduleRepository = harness["schedules"]
    original_read = schedules.read_snapshot

    async def stale_then_current(**kwargs: Any) -> Any:
        snap = await original_read(**kwargs)
        # Advance schedule so CAS before-match fails.
        async with harness["db"].sessions() as session, session.begin():
            action = await session.get(ScheduledAction, "sched-java")
            assert action is not None
            action.run_at = JAVA_RUN_AT + timedelta(minutes=99)
        return snap

    schedules.read_snapshot = stale_then_current  # type: ignore[method-assign]
    later = (JAVA_RUN_AT + timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
    second = await _invoke(
        harness,
        {"action_id": "sched-java", "run_at": later},
        ordinal=2,
    )
    schedules.read_snapshot = original_read  # type: ignore[method-assign]
    assert second.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert second.error_code == ToolErrorCode.CONFLICT.value
    snap = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    assert snap is not None
    # Stale wrapper advanced to +99; competing write must not overwrite to +40.
    assert snap.run_at == JAVA_RUN_AT + timedelta(minutes=99)


@pytest.mark.asyncio
async def test_no_plaintext_token_in_side_effect_or_trace(
    harness: dict[str, Any],
) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert result.status == ToolInvocationStatus.SUCCESS
    async with harness["db"].sessions() as session:
        effect = await session.scalar(
            select(SideEffect).where(SideEffect.run_id == "run-write-1")
        )
    assert effect is not None
    _assert_no_plaintext(effect.result)
    _assert_no_plaintext(result.output)
    trace = harness["runtime"].last_trace(result.trace_id)
    assert trace is not None
    _assert_no_plaintext(
        {
            "metadata": trace.attempts[0].metadata,
            "argument_summary": trace.argument_summary,
        }
    )


@pytest.mark.asyncio
async def test_delay_ten_minutes_only_touches_java_schedule(
    harness: dict[str, Any],
) -> None:
    new_run_at = (JAVA_RUN_AT + timedelta(minutes=10)).isoformat().replace(
        "+00:00", "Z"
    )
    result = await _invoke(harness, {"action_id": "sched-java", "run_at": new_run_at})
    assert result.status == ToolInvocationStatus.SUCCESS
    java = await harness["schedules"].read_snapshot(
        action_id="sched-java", user_id="user-1"
    )
    fit = await harness["schedules"].read_snapshot(
        action_id="sched-fit", user_id="user-1"
    )
    assert java is not None and fit is not None
    assert java.run_at == JAVA_RUN_AT + timedelta(minutes=10)
    assert fit.run_at == FIT_RUN_AT
    assert fit.capability_id == "cap-fit"


# ---------------------------------------------------------------------------
# Search completeness (Step 2 follow-up) + migration hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_budget_exhaustion_sets_completeness_fields() -> None:
    class _Community:
        async def search_posts(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

    from app.tool_runtime import ToolAttemptTrace
    from dataclasses import replace

    definition = replace(
        tool_registry.get("community.search_posts"),
        capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=1),
    )
    attempt = ToolAttemptTrace(attempt=1, started_at=datetime.now(timezone.utc))
    output = await handle_search_posts(
        community=_Community(),  # type: ignore[arg-type]
        arguments={"query": "稀有关键词xyz", "limit": 5},
        context=_ctx(),
        definition=definition,
        capability=CapabilityGrant(
            token="t", capability_id="c", expires_at="2099-01-01T00:00:00Z"
        ),
        credentials=_creds(),
        deadline_at=None,
        attempt_trace=attempt,
    )
    assert output["results"] == []
    assert output["search_complete"] is False
    assert output["truncated"] is True
    assert output["stop_reason"] == "CAPABILITY_BUDGET_EXHAUSTED"


def test_update_schedule_definition_and_backlog() -> None:
    definition = tool_registry.get("publication.update_schedule")
    assert definition.transport == TransportType.BUILTIN
    assert definition.idempotency_mode == IdempotencyMode.SIDE_EFFECT_REQUIRED
    assert definition.retry_policy.max_attempts == 1
    assert "publication.update_schedule" in MIGRATED_WRITE_TOOLS
    assert "publication.update_schedule" not in LEGACY_BUILTIN_MIGRATION_BACKLOG
    source = inspect.getsource(AgentWorker._dispatch_builtin_tool)
    assert 'tool == "publication.update_schedule"' not in source
    assert "修改后的发布时间必须至少晚于当前时间" not in source


def test_schedule_repository_is_shared_read_surface() -> None:
    assert hasattr(ScheduleRepository, "get_own_schedule")
    assert hasattr(ScheduleRepository, "read_snapshot")
    assert hasattr(ScheduleRepository, "cas_update")
