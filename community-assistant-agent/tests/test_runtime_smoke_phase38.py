"""Phase 3.8: real Agent Runtime smoke (Postgres + Redis + Worker + HTTP stubs).

Proves:
  HTTP → Run QUEUED → Worker claim → control plane → ToolRuntime → stub Java
  → SideEffect Ledger → COMPLETED

Does not monkeypatch Worker execution outcomes. LLM Adaptive Router is stubbed
only so the control-plane path can run without DeepSeek; ToolRuntime, HTTP
clients, SideEffect ledger, and DB mutations are real.

Requires live Postgres/Redis (see ASSISTANT_DATABASE_URL / ASSISTANT_REDIS_URL).
Marked ``external`` so the portable canonical suite
``pytest -m "regression and not external"`` stays runnable without middleware.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import delete, func, select
from starlette.responses import JSONResponse, Response

# Force project Postgres/Redis before Settings is cached.
os.environ.setdefault(
    "ASSISTANT_DATABASE_URL",
    "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
)
os.environ.setdefault(
    "ASSISTANT_REDIS_URL",
    "redis://:mindflow@127.0.0.1:26379/0",
)
os.environ.setdefault(
    "ASSISTANT_SERVICE_SHARED_SECRET", "phase38-smoke-secret-key-32b!!"
)
os.environ.setdefault("ASSISTANT_ALLOW_INSECURE_HTTP", "true")
os.environ.setdefault("ASSISTANT_DISTRIBUTED_LIMITS_ENABLED", "true")
os.environ.setdefault("ASSISTANT_DISTRIBUTED_LIMITS_REQUIRED", "false")
os.environ.setdefault("ASSISTANT_SEMANTIC_MEMORY_ENABLED", "false")
os.environ.setdefault("ASSISTANT_EPISODIC_MEMORY_ENABLED", "false")
os.environ.setdefault("DEEPSEEK_API_KEY", "smoke-unused")

from app.clients import CommunityClient, CreatorClient
from app.config import Settings, get_settings
from app.database import (
    AgentEvent,
    Approval,
    Artifact,
    ArtifactRelation,
    Conversation,
    ConversationGoal as ConversationGoalRecord,
    Database,
    IdempotencyRecord,
    IntentDelta as IntentDeltaRecord,
    Message,
    PolicyAudit,
    Run,
    RunStep,
    ScheduledAction,
    ScheduledActionAttempt,
    SideEffect,
    TargetBinding as TargetBindingRecord,
    ToolExecutionReceipt,
    ToolJob,
    utc_now,
)
from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    MessageCreate,
    Principal,
    TargetBinding,
    TargetContext,
)
from app.llm import DeepSeekClient
from app.main import Runtime, send_message
from app.mcp_gateway import McpGateway
from app.memory import AssistantMemory
from app.rate_limit import DistributedRateLimiter
from app.tools import tool_registry
from app.worker import AgentWorker

SH = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
_TEST_NOW_UTC = datetime.now(UTC).replace(microsecond=0)
MSG_CREATED = _TEST_NOW_UTC.astimezone(SH)
JAVA_RUN_AT = (_TEST_NOW_UTC + timedelta(days=1)).astimezone(SH)
FIT_RUN_AT = JAVA_RUN_AT + timedelta(hours=2)
EXPECTED_TOOL_RUN_AT = (
    (JAVA_RUN_AT + timedelta(minutes=10))
    .astimezone(UTC)
    .strftime("%Y-%m-%dT%H:%M:%SZ")
)
_expected_local = (JAVA_RUN_AT + timedelta(minutes=10)).astimezone(SH)
_expected_period = "上午" if _expected_local.hour < 12 else "下午"
_expected_hour = _expected_local.hour % 12 or 12
EXPECTED_USER_TIME = (
    f"{_expected_local.year}年{_expected_local.month}月{_expected_local.day}日"
    f"{_expected_period}{_expected_hour}:{_expected_local.minute:02d}（北京时间）"
)
JAVA_TITLE = "如何高效学好 Java：一份实用的学习路线图"
FIT_TITLE = "科学减肥：从饮食、运动到生活习惯的完整指南"
CONTENT_SHA = "a" * 64

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.external,
    pytest.mark.regression,
]


class JavaCapabilityStub:
    """Minimal real HTTP stub for Community capability issue/revoke."""

    def __init__(self) -> None:
        self.issue_calls: list[dict[str, Any]] = []
        self.fail_next_issue = 0
        self._counter = 0
        self._app = FastAPI()
        self._register_routes()

    def _register_routes(self) -> None:
        app = self._app

        @app.post("/api/v1/assistant-tools/capabilities")
        async def issue(request: Request) -> Response:
            try:
                payload = await request.json()
                self.issue_calls.append(payload)
                if self.fail_next_issue > 0:
                    self.fail_next_issue -= 1
                    return JSONResponse({"error": "unavailable"}, status_code=503)
                self._counter += 1
                return JSONResponse(
                    {
                        "token": f"cap-token-{self._counter}",
                        "capabilityId": f"cap-{self._counter}",
                        "expiresAt": "2026-08-06T00:00:00Z",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                return JSONResponse(
                    {"error": str(exc), "type": type(exc).__name__},
                    status_code=500,
                )

        @app.delete("/api/v1/assistant-tools/capabilities/{capability_id}")
        async def revoke(capability_id: str) -> dict[str, Any]:
            return {"revoked": True, "capabilityId": capability_id}

    def bind_client(self, community: CommunityClient) -> None:
        """Point CommunityClient at this stub over real httpx MockTransport."""

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.issue_calls.append(
                {
                    "method": request.method,
                    "path": path,
                    "body": request.content.decode("utf-8", errors="replace"),
                }
            )
            if request.method == "POST" and path.rstrip("/").endswith("/capabilities"):
                if self.fail_next_issue > 0:
                    self.fail_next_issue -= 1
                    return httpx.Response(503, json={"error": "unavailable"})
                self._counter += 1
                return httpx.Response(
                    200,
                    json={
                        "token": f"cap-token-{self._counter}",
                        "capabilityId": f"cap-{self._counter}",
                        "expiresAt": "2026-08-06T00:00:00Z",
                    },
                )
            if request.method == "DELETE" and "/capabilities/" in path:
                return httpx.Response(200, json={"revoked": True})
            return httpx.Response(
                200,
                json={"token": "cap-fallback", "capabilityId": "cap-fallback", "expiresAt": "2026-08-06T00:00:00Z"},
            )

        community.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://java-capability-stub",
            timeout=30.0,
            trust_env=False,
        )


@pytest_asyncio.fixture
async def java_stub() -> AsyncIterator[JavaCapabilityStub]:
    yield JavaCapabilityStub()


@asynccontextmanager
async def smoke_runtime(
    java_stub: JavaCapabilityStub,
) -> AsyncIterator[Runtime]:
    get_settings.cache_clear()
    settings = Settings(
        database_url=os.environ["ASSISTANT_DATABASE_URL"],
        redis_url=os.environ["ASSISTANT_REDIS_URL"],
        java_base_url="http://java-capability-stub",
        creator_base_url="http://127.0.0.1:9",
        service_shared_secret=os.environ["ASSISTANT_SERVICE_SHARED_SECRET"],
        allow_insecure_http=True,
        distributed_limits_enabled=True,
        distributed_limits_required=False,
        semantic_memory_enabled=False,
        episodic_memory_enabled=False,
        process_role="api",
        deepseek_api_key="smoke-unused",
    )
    rt = Runtime(settings)
    rt.llm = StubLLM(settings, tool_registry)
    rt.community = CommunityClient(settings)
    java_stub.bind_client(rt.community)
    rt.creator = CreatorClient(settings)
    rt.mcp = McpGateway(settings)
    rt.memory = AssistantMemory(settings, rt.database)
    rt.rate_limiter = DistributedRateLimiter(
        redis_url=settings.redis_url,
        enabled=settings.distributed_limits_enabled,
        required=settings.distributed_limits_required,
        global_requests_per_minute=settings.model_requests_per_minute,
        user_requests_per_minute=settings.user_model_requests_per_minute,
    )
    rt.worker = AgentWorker(
        settings=settings,
        database=rt.database,
        llm=rt.llm,
        community=rt.community,
        creator=rt.creator,
        mcp=rt.mcp,
        registry=tool_registry,
        rate_limiter=rt.rate_limiter,
        memory=rt.memory,
    )
    # Handlers live on Worker.execution_runtime (instance-isolated).
    await rt.database.initialize()
    await rt.rate_limiter.start()
    try:
        yield rt
    finally:
        await rt.rate_limiter.close()
        await rt.community.close()
        await rt.creator.close()
        await rt.llm.close()
        await rt.database.close()
        get_settings.cache_clear()


class StubLLM(DeepSeekClient):
    """Deterministic Adaptive Router for schedule UPDATE smoke paths only."""

    def deterministic_execution(self, **kwargs: Any) -> AdaptiveExecutionDecision | None:
        prompt = str(kwargs.get("prompt") or "")
        return AdaptiveExecutionDecision(
            execution_path="ORCHESTRATED",
            classification_summary="Phase3.8 smoke: UPDATE_SCHEDULE",
            intent=CommunityIntent(
                domain="content_publish",
                goal=prompt.strip(),
                required_capabilities=["schedule_publish"],
                risk="high",
                confidence=1.0,
            ),
            turn_relation="MODIFY",
            primary_operation="UPDATE_SCHEDULE",
        )

    async def decide_execution(self, **kwargs: Any) -> AdaptiveExecutionDecision:
        decision = self.deterministic_execution(**kwargs)
        assert decision is not None
        return decision

    async def answer(self, **kwargs: Any) -> str:
        return "已完成发布时间调整。"

    async def plan(self, **kwargs: Any) -> Any:
        raise RuntimeError("Phase 3.8 smoke must not fall through to LLM planning")

    async def verify(self, **kwargs: Any) -> Any:
        raise RuntimeError("Phase 3.8 smoke must not fall through to LLM verify")


async def _cleanup_fixed_ids(database: Database, conversation_id: str) -> None:
    seed_run_ids = ("seed-run-goal-java", "seed-run-goal-fit")
    goal_ids = ("goal-java", "goal-fit")
    schedule_ids = ("sched-java", "sched-fit")
    artifact_ids = ("artifact-draft-java", "artifact-draft-fit")
    async with database.sessions() as session, session.begin():
        run_ids_subq = select(Run.id).where(
            (Run.conversation_id == conversation_id)
            | Run.id.in_(seed_run_ids)
            | Run.goal_id.in_(goal_ids)
        )
        step_ids_subq = select(RunStep.id).where(RunStep.run_id.in_(run_ids_subq))
        artifact_ids_subq = select(Artifact.id).where(
            Artifact.run_id.in_(run_ids_subq) | Artifact.id.in_(artifact_ids)
        )
        await session.execute(
            delete(ArtifactRelation).where(
                ArtifactRelation.source_artifact_id.in_(artifact_ids_subq)
                | ArtifactRelation.target_artifact_id.in_(artifact_ids_subq)
            )
        )
        await session.execute(
            delete(ToolExecutionReceipt).where(
                ToolExecutionReceipt.run_id.in_(run_ids_subq)
            )
        )
        await session.execute(delete(ToolJob).where(ToolJob.run_id.in_(run_ids_subq)))
        await session.execute(
            delete(SideEffect).where(SideEffect.run_id.in_(run_ids_subq))
        )
        await session.execute(
            delete(PolicyAudit).where(PolicyAudit.run_id.in_(run_ids_subq))
        )
        await session.execute(
            delete(Approval).where(Approval.run_id.in_(run_ids_subq))
        )
        await session.execute(
            delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids_subq))
        )
        await session.execute(
            delete(Artifact).where(Artifact.id.in_(artifact_ids_subq))
        )
        await session.execute(delete(RunStep).where(RunStep.id.in_(step_ids_subq)))
        await session.execute(
            delete(IntentDeltaRecord).where(
                IntentDeltaRecord.goal_id.in_(goal_ids)
                | IntentDeltaRecord.run_id.in_(run_ids_subq)
            )
        )
        await session.execute(
            delete(TargetBindingRecord).where(
                TargetBindingRecord.goal_id.in_(goal_ids)
            )
        )
        await session.execute(
            delete(ScheduledActionAttempt).where(
                ScheduledActionAttempt.action_id.in_(schedule_ids)
            )
        )
        await session.execute(
            delete(ScheduledAction).where(ScheduledAction.id.in_(schedule_ids))
        )
        await session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.run_id.in_(run_ids_subq))
        )
        await session.execute(delete(Message).where(Message.run_id.in_(run_ids_subq)))
        await session.execute(
            delete(Run).where(
                Run.id.in_(run_ids_subq) | Run.goal_id.in_(goal_ids)
            )
        )
        await session.execute(
            delete(ConversationGoalRecord).where(
                ConversationGoalRecord.id.in_(goal_ids)
            )
        )
        await session.execute(
            delete(Message).where(Message.conversation_id == conversation_id)
        )
        await session.execute(
            delete(Conversation).where(Conversation.id == conversation_id)
        )


async def _seed_dual_tasks(
    database: Database,
    *,
    user_id: str,
    conversation_id: str,
    tenant_id: str = "zhiguang",
) -> None:
    await _cleanup_fixed_ids(database, conversation_id)

    java_content = TargetBinding(
        target_type="DRAFT",
        role="CONTENT",
        target_id="draft-java",
        artifact_id="artifact-draft-java",
        content_sha256=CONTENT_SHA,
        schedule_id="sched-java",
        resolution_method="TOOL_OUTPUT",
    )
    java_schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id="sched-java",
        schedule_id="sched-java",
        resolution_method="TOOL_OUTPUT",
    )
    fit_content = TargetBinding(
        target_type="DRAFT",
        role="CONTENT",
        target_id="draft-fit",
        artifact_id="artifact-draft-fit",
        content_sha256=CONTENT_SHA,
        schedule_id="sched-fit",
        resolution_method="TOOL_OUTPUT",
    )
    fit_schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id="sched-fit",
        schedule_id="sched-fit",
        resolution_method="TOOL_OUTPUT",
    )
    now = utc_now()
    async with database.sessions() as session, session.begin():
        session.add(
            Conversation(
                id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                title="Phase3.8 smoke",
            )
        )
        for goal_id, title, content, schedule, minutes_ago in (
            ("goal-java", JAVA_TITLE, java_content, java_schedule, 10),
            ("goal-fit", FIT_TITLE, fit_content, fit_schedule, 1),
        ):
            session.add(
                ConversationGoalRecord(
                    id=goal_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    intent="CONTENT_PUBLISH",
                    summary=title,
                    aliases=[title, "Java学习路线" if "Java" in title else "减肥"],
                    status="ACTIVE",
                    phase="SCHEDULED",
                    active_target_ref=f"draft:{content.target_id}",
                    target_context=TargetContext(
                        content_target=content,
                        schedule_target=schedule,
                    ).model_dump(mode="json"),
                    version=1,
                    created_at=now - timedelta(minutes=minutes_ago + 5),
                    updated_at=now - timedelta(minutes=minutes_ago),
                )
            )
            await session.flush()
            session.add(
                TargetBindingRecord(
                    goal_id=goal_id,
                    target_type=content.target_type,
                    role="CONTENT",
                    target_id=content.target_id,
                    artifact_id=content.artifact_id,
                    content_sha256=CONTENT_SHA,
                    version=1,
                    schedule_id=content.schedule_id,
                    resolution_method="TOOL_OUTPUT",
                )
            )
            session.add(
                TargetBindingRecord(
                    goal_id=goal_id,
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id=schedule.target_id,
                    version=2,
                    schedule_id=schedule.schedule_id,
                    resolution_method="TOOL_OUTPUT",
                )
            )
            seed_run = Run(
                id=f"seed-run-{goal_id}",
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                goal_id=goal_id,
                prompt=f"创建{title}",
                status="COMPLETED",
                delegated_token="seed",
                client_timezone="Asia/Shanghai",
                runtime_identity={"smoke": True},
            )
            session.add(seed_run)
            await session.flush()
            session.add(
                Artifact(
                    id=f"artifact-{content.target_id}"[:36],
                    run_id=seed_run.id,
                    task_key="seed-draft",
                    agent_name="PublishAgent",
                    artifact_type="content_draft",
                    version=1,
                    content={
                        "title": title,
                        "draft_id": content.target_id,
                        "bodyMarkdown": "seed",
                    },
                    content_hash=hashlib.sha256(title.encode()).hexdigest(),
                )
            )
        session.add(
            ScheduledAction(
                id="sched-java",
                run_id="seed-run-goal-java",
                user_id=user_id,
                draft_id="draft-java",
                expected_content_sha256=CONTENT_SHA,
                instruction=JAVA_TITLE,
                run_at=JAVA_RUN_AT.astimezone(UTC),
                status="SCHEDULED",
                idempotency_key=f"sched-java-{conversation_id}",
                capability_id="cap-seed-java",
            )
        )
        session.add(
            ScheduledAction(
                id="sched-fit",
                run_id="seed-run-goal-fit",
                user_id=user_id,
                draft_id="draft-fit",
                expected_content_sha256=CONTENT_SHA,
                instruction=FIT_TITLE,
                run_at=FIT_RUN_AT.astimezone(UTC),
                status="SCHEDULED",
                idempotency_key=f"sched-fit-{conversation_id}",
                capability_id="cap-seed-fit",
            )
        )


async def _force_claim_run(worker: AgentWorker, run_id: str) -> None:
    """Claim a specific run for this worker, clearing retry backoff."""

    now = utc_now()
    async with worker.database.sessions() as session, session.begin():
        run = await session.get(Run, run_id, with_for_update=True)
        assert run is not None
        run.status = "RUNNING"
        run.lease_owner = worker.worker_id
        run.lease_expires_at = now + timedelta(seconds=worker.settings.lease_seconds)
        run.retry_after = None
        if run.started_at is None:
            run.started_at = now
        run.updated_at = now
        run.attempts = max(int(run.attempts or 0), 1)


async def _claim_and_execute(
    worker: AgentWorker, *, expected_run_id: str
) -> str:
    await _force_claim_run(worker, expected_run_id)
    await worker._execute_run(expected_run_id)
    return expected_run_id


async def _wait_until_completed(
    worker: AgentWorker, run_id: str, *, rounds: int = 8
) -> Run:
    for _ in range(rounds):
        async with worker.database.sessions() as session:
            current = await session.get(Run, run_id)
            assert current is not None
            status = current.status
            error = current.error
            summary = current.summary
            if status == "COMPLETED":
                return current
            if status in {"FAILED", "CANCELLED"}:
                raise AssertionError(
                    f"run {run_id} ended as {status}: {error or summary}"
                )
        if status in {"RETRYING", "QUEUED", "WAITING_DEPENDENCY", "WAITING_LANE"}:
            await _force_claim_run(worker, run_id)
            await worker._execute_run(run_id)
        else:
            await asyncio.sleep(0.2)
    async with worker.database.sessions() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        raise AssertionError(f"run {run_id} stuck in {run.status}: {run.error}")


async def _post_message(
    *,
    rt: Runtime,
    conversation_id: str,
    user_id: str,
    content: str,
    idempotency_key: str,
) -> str:
    principal = Principal(
        user_id=user_id,
        tenant_id="zhiguang",
        role="USER",
        display_name="smoke",
        token="smoke-user-jwt",
    )
    accepted = await send_message(
        conversation_id,
        MessageCreate(content=content, client_timezone="Asia/Shanghai"),
        principal,  # type: ignore[arg-type]
        rt,  # type: ignore[arg-type]
        idempotency_key,
    )
    run_id = accepted.run_id
    async with rt.database.sessions() as session, session.begin():
        run = await session.get(Run, run_id)
        assert run is not None
        message = await session.scalar(
            select(Message)
            .where(Message.run_id == run_id, Message.role == "user")
            .limit(1)
        )
        assert message is not None
        message.created_at = MSG_CREATED
        run.created_at = MSG_CREATED
        checkpoint = dict(run.checkpoint or {})
        checkpoint["message_id"] = message.id
        run.checkpoint = checkpoint
    return run_id


async def _schedule_run_at(database: Database, action_id: str) -> datetime:
    async with database.sessions() as session:
        action = await session.get(ScheduledAction, action_id)
        assert action is not None
        value = action.run_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


async def _count_goals(database: Database, conversation_id: str) -> int:
    async with database.sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ConversationGoalRecord)
                .where(ConversationGoalRecord.conversation_id == conversation_id)
            )
            or 0
        )


async def _count_schedules(database: Database, user_id: str) -> int:
    async with database.sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ScheduledAction)
                .where(ScheduledAction.user_id == user_id)
            )
            or 0
        )



@pytest.mark.asyncio
async def test_phase38_happy_path_delay_java_ten_minutes(
    java_stub: JavaCapabilityStub,
) -> None:
    user_id = f"smoke-user-{uuid.uuid4().hex[:8]}"
    conversation_id = f"smoke-conv-{uuid.uuid4().hex[:8]}"
    async with smoke_runtime(java_stub) as rt:
        await _seed_dual_tasks(
            rt.database, user_id=user_id, conversation_id=conversation_id
        )
        before_goals = await _count_goals(rt.database, conversation_id)
        before_schedules = await _count_schedules(rt.database, user_id)

        run_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="把 Java 学习路线那篇延迟十分钟",
            idempotency_key=f"smoke-happy-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(rt.worker, expected_run_id=run_id)
        run = await _wait_until_completed(rt.worker, run_id)

        assert run.status == "COMPLETED"
        assert run.goal_id == "goal-java"
        assert EXPECTED_USER_TIME in (run.final_response or "")
        assert "00:10:00Z" not in (run.final_response or "")

        async with rt.database.sessions() as session:
            effects = list(
                (
                    await session.scalars(
                        select(SideEffect).where(
                            SideEffect.run_id == run_id,
                            SideEffect.tool_name == "publication.update_schedule",
                        )
                    )
                ).all()
            )
            assert len(effects) == 1
            assert effects[0].status == "COMPLETED"
            assert effects[0].attempts >= 1
            assert effects[0].operation_key

        java_at = await _schedule_run_at(rt.database, "sched-java")
        fit_at = await _schedule_run_at(rt.database, "sched-fit")
        assert java_at.astimezone(SH) == JAVA_RUN_AT + timedelta(minutes=10)
        assert fit_at.astimezone(SH) == FIT_RUN_AT
        assert await _count_goals(rt.database, conversation_id) == before_goals
        assert await _count_schedules(rt.database, user_id) == before_schedules
        assert len(java_stub.issue_calls) >= 1


@pytest.mark.asyncio
async def test_phase38_retry_keeps_solidified_run_at(
    java_stub: JavaCapabilityStub,
) -> None:
    user_id = f"smoke-user-{uuid.uuid4().hex[:8]}"
    conversation_id = f"smoke-conv-{uuid.uuid4().hex[:8]}"
    java_stub.fail_next_issue = 1
    async with smoke_runtime(java_stub) as rt:
        await _seed_dual_tasks(
            rt.database, user_id=user_id, conversation_id=conversation_id
        )
        run_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="把 Java 学习路线那篇延迟十分钟",
            idempotency_key=f"smoke-retry-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(rt.worker, expected_run_id=run_id)
        run = await _wait_until_completed(rt.worker, run_id)

        assert run.status == "COMPLETED"
        plan = dict(run.plan or {})
        update_args = None
        for step in list(plan.get("steps") or []):
            if dict(step).get("tool") == "publication.update_schedule":
                update_args = dict(dict(step).get("arguments") or {})
        assert update_args is not None
        assert update_args.get("run_at") == EXPECTED_TOOL_RUN_AT
        assert "delay_seconds" not in update_args

        async with rt.database.sessions() as session:
            effects = list(
                (
                    await session.scalars(
                        select(SideEffect).where(
                            SideEffect.run_id == run_id,
                            SideEffect.tool_name == "publication.update_schedule",
                        )
                    )
                ).all()
            )
            assert len(effects) == 1
            assert effects[0].status == "COMPLETED"
            assert effects[0].attempts >= 1

        java_at = await _schedule_run_at(rt.database, "sched-java")
        assert java_at.astimezone(SH) == JAVA_RUN_AT + timedelta(minutes=10)
        assert java_stub.fail_next_issue == 0
        assert len(java_stub.issue_calls) >= 2


@pytest.mark.asyncio
async def test_phase38_temporal_clarification_then_resume(
    java_stub: JavaCapabilityStub,
) -> None:
    user_id = f"smoke-user-{uuid.uuid4().hex[:8]}"
    conversation_id = f"smoke-conv-{uuid.uuid4().hex[:8]}"
    async with smoke_runtime(java_stub) as rt:
        await _seed_dual_tasks(
            rt.database, user_id=user_id, conversation_id=conversation_id
        )
        run_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="调整一下 Java 那篇的发布时间",
            idempotency_key=f"smoke-clarify-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(rt.worker, expected_run_id=run_id)

        async with rt.database.sessions() as session:
            run = await session.get(Run, run_id)
            goal = await session.get(ConversationGoalRecord, "goal-java")
            assert run is not None and goal is not None
            assert run.status == "WAITING_CLARIFICATION"
            assert goal.status == "WAITING_CLARIFICATION"
            assert (goal.pending_clarification or {}).get("kind") == "TEMPORAL_SCHEDULE"
            effects = list(
                (
                    await session.scalars(
                        select(SideEffect).where(SideEffect.run_id == run_id)
                    )
                ).all()
            )
            assert effects == []

        assert (
            await _schedule_run_at(rt.database, "sched-java")
        ).astimezone(SH) == JAVA_RUN_AT

        resume_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="延迟十分钟",
            idempotency_key=f"smoke-clarify-resume-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(rt.worker, expected_run_id=resume_id)
        resume = await _wait_until_completed(rt.worker, resume_id)

        assert resume.status == "COMPLETED"
        assert resume.goal_id == "goal-java"
        assert EXPECTED_USER_TIME in (resume.final_response or "")

        async with rt.database.sessions() as session:
            effects = list(
                (
                    await session.scalars(
                        select(SideEffect).where(
                            SideEffect.run_id == resume_id,
                            SideEffect.tool_name == "publication.update_schedule",
                        )
                    )
                ).all()
            )
            assert len(effects) == 1
            assert effects[0].status == "COMPLETED"
            goal_count = await session.scalar(
                select(func.count())
                .select_from(ConversationGoalRecord)
                .where(ConversationGoalRecord.conversation_id == conversation_id)
            )
            assert int(goal_count or 0) == 2

        java_at = await _schedule_run_at(rt.database, "sched-java")
        fit_at = await _schedule_run_at(rt.database, "sched-fit")
        assert java_at.astimezone(SH) == JAVA_RUN_AT + timedelta(minutes=10)
        assert fit_at.astimezone(SH) == FIT_RUN_AT


@pytest.mark.asyncio
async def test_phase38_worker_restart_recovers_temporal_clarification(
    java_stub: JavaCapabilityStub,
) -> None:
    user_id = f"smoke-user-{uuid.uuid4().hex[:8]}"
    conversation_id = f"smoke-conv-{uuid.uuid4().hex[:8]}"
    async with smoke_runtime(java_stub) as rt:
        await _seed_dual_tasks(
            rt.database, user_id=user_id, conversation_id=conversation_id
        )
        run_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="调整一下 Java 那篇的发布时间",
            idempotency_key=f"smoke-restart-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(rt.worker, expected_run_id=run_id)
        async with rt.database.sessions() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            assert run.status == "WAITING_CLARIFICATION"

        restarted = AgentWorker(
            settings=rt.settings,
            database=rt.database,
            llm=rt.llm,
            community=rt.community,
            creator=rt.creator,
            mcp=rt.mcp,
            registry=tool_registry,
            rate_limiter=rt.rate_limiter,
            memory=rt.memory,
        )
        resume_id = await _post_message(
            rt=rt,
            conversation_id=conversation_id,
            user_id=user_id,
            content="延迟十分钟",
            idempotency_key=f"smoke-restart-resume-{uuid.uuid4().hex}",
        )
        await _claim_and_execute(restarted, expected_run_id=resume_id)
        resume = await _wait_until_completed(restarted, resume_id)

        assert resume.status == "COMPLETED"
        assert resume.goal_id == "goal-java"
        assert await _count_goals(rt.database, conversation_id) == 2
        java_at = await _schedule_run_at(rt.database, "sched-java")
        assert java_at.astimezone(SH) == JAVA_RUN_AT + timedelta(minutes=10)
