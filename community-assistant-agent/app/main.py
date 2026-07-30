from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.clients import CommunityClient, CreatorClient, ModerationClient
from app.config import Settings, get_settings
from app.database import (
    AgentEvent,
    Approval,
    Conversation,
    Database,
    IdempotencyRecord,
    Message,
    Run,
    RunStep,
    Artifact,
    PolicyAudit,
    ToolJob,
    ScheduledAction,
    ScheduledActionAttempt,
    SideEffect,
    UserMemory,
    EpisodicMemory,
    MemoryProfile,
    append_event,
    utc_now,
)
from app.domain import (
    AgentPlan,
    ArtifactView,
    ConversationCreate,
    ConversationView,
    MessageCreate,
    MessageView,
    ApprovalDecision,
    ApprovalView,
    MemoryCreate,
    MemoryView,
    MemoryProfileUpdate,
    MemoryProfileView,
    EpisodicMemoryView,
    Principal,
    PolicyAuditView,
    RunAccepted,
    RunListItemView,
    RunListStepView,
    RunView,
    ScheduledActionAttemptView,
    ScheduledActionView,
    StepView,
    ToolJobView,
)
from app.agent_registry import agent_registry
from app.artifacts import blackboard_snapshot
from app.graph_runtime import graph_descriptor
from app.llm import DeepSeekClient
from app.mcp_gateway import McpGateway
from app.memory import AssistantMemory
from app.policy import community_policy
from app.rate_limit import DistributedRateLimiter
from app.security import current_principal
from app.token_vault import DelegatedTokenVault
from app.worker import AgentWorker
from app.tools import tool_registry
from app.skill_registry import skill_registry


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url)
        self.llm = DeepSeekClient(settings, tool_registry)
        self.community = CommunityClient(settings)
        self.creator = CreatorClient(settings)
        self.moderation = ModerationClient(settings)
        self.mcp = McpGateway(settings)
        self.memory = AssistantMemory(settings, self.database)
        self.token_vault = DelegatedTokenVault(settings.service_shared_secret)
        self.rate_limiter = DistributedRateLimiter(
            redis_url=settings.redis_url,
            enabled=settings.distributed_limits_enabled,
            required=settings.distributed_limits_required,
            global_requests_per_minute=settings.model_requests_per_minute,
            user_requests_per_minute=settings.user_model_requests_per_minute,
        )
        self.worker = AgentWorker(
            settings=settings,
            database=self.database,
            llm=self.llm,
            community=self.community,
            creator=self.creator,
            moderation=self.moderation,
            mcp=self.mcp,
            registry=tool_registry,
            rate_limiter=self.rate_limiter,
            memory=self.memory,
        )
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.mcp.discover(tool_registry)
        await self.database.initialize()
        await self.memory.start()
        await self.rate_limiter.start()
        if self.settings.process_role == "all":
            self.worker_task = asyncio.create_task(self.worker.run_forever())
        elif self.settings.process_role == "run-worker":
            self.worker_task = asyncio.create_task(
                self.worker.run_and_tool_jobs_forever()
            )
        elif self.settings.process_role == "scheduler-worker":
            self.worker_task = asyncio.create_task(
                self.worker.schedule_jobs_forever()
            )
        elif self.settings.process_role == "tool-worker":
            self.worker_task = asyncio.create_task(
                self.worker.tool_jobs_forever()
            )

    async def close(self) -> None:
        self.worker.stop()
        if self.worker_task:
            await self.worker_task
        await self.llm.close()
        await self.community.close()
        await self.creator.close()
        await self.moderation.close()
        await self.rate_limiter.close()
        await self.memory.close()
        await self.database.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = Runtime(get_settings())
    await runtime.start()
    app.state.runtime = runtime
    yield
    await runtime.close()


app = FastAPI(
    title="GreenBook Community Assistant Agent",
    version="2.0.0",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)


def runtime(request: Request) -> Runtime:
    value = getattr(request.app.state, "runtime", None)
    if not isinstance(value, Runtime):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "助手服务尚未就绪")
    return value


RuntimeDep = Annotated[Runtime, Depends(runtime)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@app.get("/actuator/health")
async def health(rt: RuntimeDep) -> dict:
    try:
        await rt.database.ping()
        memory = rt.memory.health()
        return {
            "status": "UP",
            "checks": {
                "database": "UP",
                "model": rt.settings.deepseek_model,
                "mcp_tools": len(rt.mcp.bindings),
                "skills": len(skill_registry.public_catalog()),
                "policy_version": community_policy.version,
                "episodic_memory": memory.episodic,
                "semantic_memory": memory.semantic,
                "memory_backend": memory.backend,
                "memory_embedding": memory.embedding,
            },
        }
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@app.post(
    "/api/v1/assistant/conversations",
    response_model=ConversationView,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreate, principal: PrincipalDep, rt: RuntimeDep
) -> ConversationView:
    conversation = Conversation(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        title=body.title or "新的对话",
        context_post_id=body.context_post_id,
        surface=body.surface,
    )
    async with rt.database.sessions() as session, session.begin():
        session.add(conversation)
        await session.flush()
    return _conversation_view(conversation)


@app.get(
    "/api/v1/assistant/conversations", response_model=list[ConversationView]
)
async def list_conversations(
    principal: PrincipalDep,
    rt: RuntimeDep,
    context_post_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[ConversationView]:
    query = (
        select(Conversation)
        .where(Conversation.user_id == principal.user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    if context_post_id:
        query = query.where(Conversation.context_post_id == context_post_id)
    async with rt.database.sessions() as session:
        rows = (await session.scalars(query)).all()
    return [_conversation_view(item) for item in rows]


@app.get(
    "/api/v1/assistant/conversations/{conversation_id}/messages",
    response_model=list[MessageView],
)
async def list_messages(
    conversation_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> list[MessageView]:
    await _owned_conversation(rt, conversation_id, principal.user_id)
    async with rt.database.sessions() as session:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        ).all()
    return [
        MessageView(
            message_id=item.id,
            role=item.role,
            content=item.content,
            parts=list(item.parts or []),
            run_id=item.run_id,
            created_at=item.created_at,
        )
        for item in rows
    ]


@app.post(
    "/api/v1/assistant/conversations/{conversation_id}/messages",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: str, # 会话ID
    body: MessageCreate, # 消息内容
    principal: PrincipalDep, # 当前用户
    rt: RuntimeDep, # 运行时
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],# 幂等性键
) -> RunAccepted: # 返回任务接受结果
    # 验证当前用户是否拥有该会话，如果会话不存在或不属于该用户，会抛出异常。
    conversation = await _owned_conversation(rt, conversation_id, principal.user_id)
    # 计算请求的哈希值，用于后续幂等性验证。
    request_hash = hashlib.sha256(
        f"{conversation_id}\0{body.model_dump_json()}".encode()
    ).hexdigest()
    # 在数据库中查找幂等性记录，确保同一幂等性键只能用于一次请求。
    async with rt.database.sessions() as session, session.begin():
        existing = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == principal.user_id,
                IdempotencyRecord.key == idempotency_key,
            )
        )
        # 如果幂等性记录存在，则需要验证请求的哈希值是否与记录中的哈希值一致。
        if existing:
            if existing.request_hash != request_hash:
                # 如果请求的哈希值与记录中的哈希值不一致，则抛出冲突异常。
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "同一 Idempotency-Key 不能用于不同请求",
                )
            # 如果请求的哈希值与记录中的哈希值一致，则返回已有的任务ID。
            return RunAccepted(
                run_id=existing.run_id,
                conversation_id=conversation_id,
                status="QUEUED",
                events_url=f"/api/v1/assistant/runs/{existing.run_id}/events/stream",
                replayed=True,
            )
        # 如果幂等性记录不存在，则创建新的任务。
        run = Run(
            conversation_id=conversation_id,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            principal_role=principal.role,
            prompt=body.content.strip(),
            context_post_id=body.context_post_id or conversation.context_post_id,
            context_comment_id=body.context_comment_id,
            client_timezone=body.client_timezone,
            delegated_token=rt.token_vault.encrypt(principal.token),
            status="QUEUED",
            max_model_calls=rt.settings.max_model_calls,
            max_tool_calls=rt.settings.max_tool_calls,
            max_replans=rt.settings.max_replans,
            max_attempts=rt.settings.max_run_attempts,
            runtime_identity=rt.llm.runtime_identity(),
            deadline_at=utc_now()
            + timedelta(seconds=rt.settings.run_timeout_seconds),
        )
        # 将任务添加到数据库。
        session.add(run)
        await session.flush()
        # 创建用户消息记录。
        session.add(
            Message(
                conversation_id=conversation_id,
                role="user",
                content=body.content.strip(),
                parts=[],
                run_id=run.id,
            )
        )
        # 创建幂等性记录。
        session.add(
            IdempotencyRecord(
                user_id=principal.user_id,
                key=idempotency_key,
                request_hash=request_hash,
                run_id=run.id,
            )
        )
        # 更新会话标题。
        conversation.title = (
            body.content.strip()[:32]
            if conversation.title == "新的对话"
            else conversation.title
        )
        # 更新会话更新时间。
        conversation.updated_at = utc_now()
        # 追加任务排队事件。
        await append_event(session, run.id, "RUN_QUEUED", {"status": "QUEUED"})
    # 返回任务接受结果。
    return RunAccepted(
        # 任务ID。
        run_id=run.id,
        # 会话ID。
        conversation_id=conversation_id,
        # 任务状态。
        status="QUEUED",
        # 事件流URL。
        events_url=f"/api/v1/assistant/runs/{run.id}/events/stream",
    )


@app.get("/api/v1/assistant/runs", response_model=list[RunListItemView])
async def list_runs(
    principal: PrincipalDep,
    rt: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    offset: Annotated[int, Query(ge=0, le=500)] = 0,
) -> list[RunListItemView]:
    async with rt.database.sessions() as session:
        runs = list(
            (
                await session.scalars(
                    select(Run)
                    .options(selectinload(Run.steps), selectinload(Run.approvals))
                    .where(Run.user_id == principal.user_id)
                    .order_by(Run.updated_at.desc(), Run.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
    return [_run_list_item_view(run) for run in runs]


@app.get("/api/v1/assistant/runs/{run_id}", response_model=RunView)
async def get_run(run_id: str, principal: PrincipalDep, rt: RuntimeDep) -> RunView:
    async with rt.database.sessions() as session:
        run = await session.scalar(
            select(Run)
            .options(selectinload(Run.steps), selectinload(Run.approvals))
            .where(Run.id == run_id, Run.user_id == principal.user_id)
        )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return _run_view(run)


@app.get("/api/v1/assistant/agents")
async def list_agents(principal: PrincipalDep) -> dict:
    del principal
    return {
        "agents": agent_registry.public_catalog(),
        "registry_signature": agent_registry.signature(),
    }


@app.get("/api/v1/assistant/skills")
async def list_skills(principal: PrincipalDep) -> dict:
    del principal
    return {
        "skills": skill_registry.public_catalog(),
        "registry_signature": skill_registry.signature(),
    }


@app.get("/api/v1/assistant/policy")
async def get_policy(principal: PrincipalDep) -> dict:
    del principal
    return {
        **community_policy.public_summary(),
        "policy_signature": community_policy.signature(),
    }


@app.get("/api/v1/assistant/mcp/tools")
async def list_mcp_tools(principal: PrincipalDep, rt: RuntimeDep) -> dict:
    del principal
    return {"tools": rt.mcp.public_catalog()}


@app.get(
    "/api/v1/assistant/runs/{run_id}/artifacts",
    response_model=list[ArtifactView],
)
async def list_run_artifacts(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> list[ArtifactView]:
    await _owned_run(rt, run_id, principal.user_id)
    async with rt.database.sessions() as session:
        artifacts = list(
            (
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.run_id == run_id)
                    .order_by(Artifact.created_at, Artifact.id)
                )
            ).all()
        )
    return [
        ArtifactView(
            artifact_id=item.id,
            run_id=item.run_id,
            step_id=item.step_id,
            task_id=item.task_key,
            agent=item.agent_name,
            artifact_type=item.artifact_type,
            parent_artifact_ids=list(item.parent_artifact_ids or []),
            version=item.version,
            content=dict(item.content or {}),
            content_hash=item.content_hash,
            created_at=item.created_at,
        )
        for item in artifacts
    ]


@app.get("/api/v1/assistant/runs/{run_id}/blackboard")
async def get_run_blackboard(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> dict:
    await _owned_run(rt, run_id, principal.user_id)
    async with rt.database.sessions() as session:
        return await blackboard_snapshot(session, run_id=run_id)


@app.get(
    "/api/v1/assistant/runs/{run_id}/tool-jobs",
    response_model=list[ToolJobView],
)
async def list_run_tool_jobs(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> list[ToolJobView]:
    await _owned_run(rt, run_id, principal.user_id)
    async with rt.database.sessions() as session:
        jobs = list(
            (
                await session.scalars(
                    select(ToolJob)
                    .where(ToolJob.run_id == run_id)
                    .order_by(ToolJob.created_at)
                )
            ).all()
        )
    return [_tool_job_view(item) for item in jobs]


@app.post(
    "/api/v1/assistant/tool-jobs/{job_id}/retry",
    response_model=ToolJobView,
)
async def retry_tool_job(
    job_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> ToolJobView:
    async with rt.database.sessions() as session, session.begin():
        job = await session.scalar(
            select(ToolJob).where(ToolJob.id == job_id).with_for_update()
        )
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "工具任务不存在")
        run = await session.scalar(
            select(Run)
            .where(
                Run.id == job.run_id,
                Run.user_id == principal.user_id,
            )
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "工具任务不存在")
        if job.status != "DEAD_LETTER":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "只有 Dead Letter 工具任务可以人工重试",
            )
        now = utc_now()
        job.status = "PENDING"
        job.attempts = 0
        job.error = None
        job.dead_lettered_at = None
        job.next_attempt_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        if run.status == "FAILED":
            run.status = "QUEUED"
            run.error = None
            run.completed_at = None
            run.delegated_token = rt.token_vault.encrypt(principal.token)
            run.deadline_at = now + timedelta(
                seconds=rt.settings.run_timeout_seconds
            )
            run.retry_after = now
            run.version += 1
            run.updated_at = now
        await append_event(
            session,
            run.id,
            "TOOL_JOB_MANUAL_RETRY",
            {"job_id": job.id, "tool": job.tool_name},
        )
    return _tool_job_view(job)


@app.get(
    "/api/v1/assistant/runs/{run_id}/policy-audits",
    response_model=list[PolicyAuditView],
)
async def list_run_policy_audits(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> list[PolicyAuditView]:
    await _owned_run(rt, run_id, principal.user_id)
    async with rt.database.sessions() as session:
        audits = list(
            (
                await session.scalars(
                    select(PolicyAudit)
                    .where(PolicyAudit.run_id == run_id)
                    .order_by(PolicyAudit.created_at)
                )
            ).all()
        )
    return [
        PolicyAuditView(
            audit_id=item.id,
            run_id=item.run_id,
            action=item.action,
            resource=dict(item.resource or {}),
            decision=item.decision,
            reason=item.reason,
            policy_version=item.policy_version,
            created_at=item.created_at,
        )
        for item in audits
    ]


@app.get("/api/v1/assistant/runs/{run_id}/graph")
async def get_run_graph(
    run_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> dict:
    async with rt.database.sessions() as session:
        run = await session.scalar(
            select(Run).where(
                Run.id == run_id,
                Run.user_id == principal.user_id,
            )
        )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if not run.plan:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务计划尚未生成")
    return {
        "run_id": run.id,
        "task_ledger": dict(run.task_ledger or {}),
        "progress_ledger": dict(run.progress_ledger or {}),
        **graph_descriptor(AgentPlan.model_validate(run.plan)),
    }


@app.post("/api/v1/assistant/runs/{run_id}/interrupt", response_model=RunView)
async def interrupt_run(
    run_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> RunView:
    allowed = {
        "QUEUED",
        "RUNNING",
        "RETRYING",
        "WAITING_DEPENDENCY",
        "WAITING_LANE",
    }
    async with rt.database.sessions() as session, session.begin():
        run = await session.scalar(
            select(Run)
            .where(Run.id == run_id, Run.user_id == principal.user_id)
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        if run.status not in allowed:
            raise HTTPException(status.HTTP_409_CONFLICT, "当前状态不能暂停")
        now = utc_now()
        run.status = "PAUSED"
        run.interrupted_at = now
        run.retry_after = None
        run.lease_owner = None
        run.lease_expires_at = None
        run.version += 1
        run.updated_at = now
        await append_event(
            session,
            run.id,
            "RUN_INTERRUPTED",
            {"status": "PAUSED", "version": run.version},
        )
    return await _reload_run(rt, run_id)


@app.post("/api/v1/assistant/runs/{run_id}/resume", response_model=RunView)
async def resume_run(
    run_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> RunView:
    async with rt.database.sessions() as session, session.begin():
        run = await session.scalar(
            select(Run)
            .where(Run.id == run_id, Run.user_id == principal.user_id)
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        if run.status != "PAUSED":
            raise HTTPException(status.HTTP_409_CONFLICT, "只有暂停任务可以恢复")
        run.status = "QUEUED"
        run.interrupted_at = None
        run.deadline_at = utc_now() + timedelta(
            seconds=rt.settings.run_timeout_seconds
        )
        run.version += 1
        run.updated_at = utc_now()
        await append_event(
            session,
            run.id,
            "RUN_RESUMED",
            {"status": "QUEUED", "version": run.version},
        )
    return await _reload_run(rt, run_id)


@app.post("/api/v1/assistant/runs/{run_id}/retry", response_model=RunView)
async def retry_run(
    run_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> RunView:
    async with rt.database.sessions() as session, session.begin():
        run = await session.scalar(
            select(Run)
            .where(Run.id == run_id, Run.user_id == principal.user_id)
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        if run.status != "FAILED":
            raise HTTPException(status.HTTP_409_CONFLICT, "只有失败任务可以重试")
        run.status = "QUEUED"
        run.error = None
        run.final_response = None
        run.completed_at = None
        run.retry_after = None
        run.attempts = 0
        run.delegated_token = rt.token_vault.encrypt(principal.token)
        run.deadline_at = utc_now() + timedelta(
            seconds=rt.settings.run_timeout_seconds
        )
        run.version += 1
        run.updated_at = utc_now()
        await append_event(
            session,
            run.id,
            "RUN_MANUAL_RETRY",
            {"status": "QUEUED", "version": run.version},
        )
    return await _reload_run(rt, run_id)


@app.post("/api/v1/assistant/runs/{run_id}/cancel", response_model=RunView)
async def cancel_run(
    run_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> RunView:
    propagation_error: str | None = None
    async with rt.database.sessions() as session:
        run_snapshot = await session.scalar(
            select(Run).where(
                Run.id == run_id,
                Run.user_id == principal.user_id,
            )
        )
        creator_effect = await session.scalar(
            select(SideEffect)
            .where(
                SideEffect.run_id == run_id,
                SideEffect.tool_name == "creator.create_draft",
                SideEffect.remote_operation_id.is_not(None),
            )
            .order_by(SideEffect.created_at.desc())
        )
    if run_snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if run_snapshot.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务已经结束，无法取消")

    if (
        creator_effect is not None
        and creator_effect.remote_operation_id
        and run_snapshot.delegated_token
    ):
        try:
            await rt.creator.cancel_task(
                creator_effect.remote_operation_id,
                access_token=rt.token_vault.decrypt(run_snapshot.delegated_token),
                trace_id=run_snapshot.trace_id,
            )
        except Exception as exc:
            propagation_error = str(exc)[:1_000]

    async with rt.database.sessions() as session, session.begin():
        run = await session.scalar(
            select(Run)
            .where(Run.id == run_id, Run.user_id == principal.user_id)
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "任务已经结束，无法取消")
        now = utc_now()
        if run.dependency_wait_started_at is not None:
            run.dependency_wait_ms += max(
                0,
                int((now - run.dependency_wait_started_at).total_seconds() * 1000),
            )
            run.dependency_wait_started_at = None
        run.status = "CANCELLED"
        run.final_response = "本次任务已取消。"
        run.error = propagation_error
        run.completed_at = now
        run.delegated_token = None
        run.retry_after = None
        run.lease_owner = None
        run.lease_expires_at = None
        run.version += 1
        run.updated_at = now

        approvals = (
            await session.scalars(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.status == "PENDING",
                )
            )
        ).all()
        for approval in approvals:
            approval.status = "REJECTED"
            approval.decided_at = now

        steps = (
            await session.scalars(
                select(RunStep).where(
                    RunStep.run_id == run_id,
                    RunStep.status.in_(
                        ["PENDING", "RUNNING", "WAITING_DEPENDENCY"]
                    ),
                )
            )
        ).all()
        for step in steps:
            step.status = "CANCELLED"
            step.completed_at = now

        effects = (
            await session.scalars(
                select(SideEffect).where(
                    SideEffect.run_id == run_id,
                    SideEffect.status.not_in(["COMPLETED", "FAILED", "CANCELLED"]),
                )
            )
        ).all()
        for effect in effects:
            effect.status = "CANCELLED"
            effect.error = propagation_error
            effect.updated_at = now

        tool_jobs = (
            await session.scalars(
                select(ToolJob).where(
                    ToolJob.run_id == run_id,
                    ToolJob.status.not_in(
                        ["COMPLETED", "DEAD_LETTER", "CANCELLED"]
                    ),
                )
            )
        ).all()
        for job in tool_jobs:
            job.status = "CANCELLED"
            job.error = "所属任务已取消"
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now

        await append_event(
            session,
            run_id,
            "RUN_CANCELLED",
            {
                "status": "CANCELLED",
                "creator_cancel_propagated": propagation_error is None,
                "propagation_error": propagation_error,
            },
        )

    async with rt.database.sessions() as session:
        refreshed = await session.scalar(
            select(Run)
            .options(selectinload(Run.steps), selectinload(Run.approvals))
            .where(Run.id == run_id)
        )
    assert refreshed is not None
    return _run_view(refreshed)


@app.get("/api/v1/assistant/runs/{run_id}/events/stream")
async def stream_events(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    async with rt.database.sessions() as session:
        owned = await session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == principal.user_id)
        )
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")

    async def generate():
        sequence = after
        idle_ticks = 0
        while True:
            async with rt.database.sessions() as session:
                events = (
                    await session.scalars(
                        select(AgentEvent)
                        .where(
                            AgentEvent.run_id == run_id,
                            AgentEvent.sequence > sequence,
                        )
                        .order_by(AgentEvent.sequence)
                    )
                ).all()
                run_status = (
                    None
                    if events
                    else await session.scalar(
                        select(Run.status).where(Run.id == run_id)
                    )
                )
            for item in events:
                sequence = item.sequence
                payload = {
                    "sequence": item.sequence,
                    "type": item.type,
                    "payload": item.payload,
                    "createdAt": item.created_at.isoformat(),
                }
                yield (
                    f"id: {item.sequence}\nevent: {item.type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            if run_status in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "PAUSED",
                "WAITING_APPROVAL",
            } and not events:
                break
            idle_ticks += 1
            if idle_ticks % 15 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(rt.settings.event_stream_poll_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post(
    "/api/v1/assistant/runs/{run_id}/approvals/{approval_id}",
    response_model=RunView,
)
async def decide_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalDecision,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> RunView:
    async with rt.database.sessions() as session, session.begin():
        run = await session.scalar(
            select(Run)
            .where(Run.id == run_id, Run.user_id == principal.user_id)
            .with_for_update()
        )
        approval = await session.scalar(
            select(Approval)
            .where(Approval.id == approval_id, Approval.run_id == run_id)
            .with_for_update()
        )
        if run is None or approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "审批不存在")
        if approval.status != "PENDING":
            raise HTTPException(status.HTTP_409_CONFLICT, "该审批已处理")
        if (
            body.expected_run_version != run.version
            or approval.expected_run_version != run.version
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "任务版本已变化，请刷新后再确认"
            )
        if approval.expires_at <= utc_now():
            approval.status = "EXPIRED"
            raise HTTPException(status.HTTP_409_CONFLICT, "审批已过期")
        approval.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
        approval.decided_at = utc_now()
        run.version += 1
        if body.decision == "APPROVE":
            run.status = "QUEUED"
            run.deadline_at = utc_now() + timedelta(
                seconds=rt.settings.run_timeout_seconds
            )
            await append_event(
                session,
                run.id,
                "APPROVAL_APPROVED",
                {"approval_id": approval.id, "action": approval.action},
            )
        else:
            run.status = "CANCELLED"
            run.final_response = (
                "已按你的选择取消删除帖子。"
                if "delete" in approval.action
                else "已按你的选择取消本次发布。"
            )
            run.completed_at = utc_now()
            run.delegated_token = None
            run.lease_owner = None
            run.lease_expires_at = None
            session.add(
                Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=run.final_response,
                    parts=[],
                    run_id=run.id,
                )
            )
            await append_event(
                session,
                run.id,
                "APPROVAL_REJECTED",
                {"approval_id": approval.id, "action": approval.action},
            )
    async with rt.database.sessions() as session:
        refreshed = await session.scalar(
            select(Run)
            .options(selectinload(Run.steps), selectinload(Run.approvals))
            .where(Run.id == run_id)
        )
    assert refreshed is not None
    return _run_view(refreshed)


@app.get("/api/v1/assistant/runs/{run_id}/events")
async def get_events(
    run_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    async with rt.database.sessions() as session:
        owned = await session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == principal.user_id)
        )
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        events = (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id, AgentEvent.sequence > after)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    return {
        "events": [
            {
                "sequence": item.sequence,
                "type": item.type,
                "payload": item.payload,
                "createdAt": item.created_at,
            }
            for item in events
        ]
    }


@app.get("/api/v1/assistant/memories", response_model=list[MemoryView])
async def list_memories(
    principal: PrincipalDep, rt: RuntimeDep
) -> list[MemoryView]:
    async with rt.database.sessions() as session:
        rows = (
            await session.scalars(
                select(UserMemory)
                .where(UserMemory.user_id == principal.user_id)
                .order_by(UserMemory.updated_at.desc())
            )
        ).all()
    return [_memory_view(item) for item in rows]


@app.get("/api/v1/assistant/operations/metrics")
async def operation_metrics(
    principal: PrincipalDep, rt: RuntimeDep
) -> dict:
    async with rt.database.sessions() as session:
        status_rows = (
            await session.execute(
                select(Run.status, func.count(Run.id))
                .where(Run.user_id == principal.user_id)
                .group_by(Run.status)
            )
        ).all()
        path_rows = (
            await session.execute(
                select(
                    Run.execution_path,
                    func.count(Run.id),
                    func.coalesce(func.avg(Run.model_calls), 0),
                    func.coalesce(func.avg(Run.model_duration_ms), 0),
                )
                .where(Run.user_id == principal.user_id)
                .group_by(Run.execution_path)
            )
        ).all()
        lane_rows = (
            await session.execute(
                select(Run.workload_lane, func.count(Run.id))
                .where(Run.user_id == principal.user_id)
                .group_by(Run.workload_lane)
            )
        ).all()
        totals = (
            await session.execute(
                select(
                    func.count(Run.id),
                    func.coalesce(func.avg(Run.model_calls), 0),
                    func.coalesce(func.avg(Run.tool_calls), 0),
                    func.coalesce(func.avg(Run.replan_count), 0),
                    func.coalesce(func.avg(Run.model_duration_ms), 0),
                    func.coalesce(func.avg(Run.tool_duration_ms), 0),
                    func.coalesce(func.avg(Run.dependency_wait_ms), 0),
                ).where(Run.user_id == principal.user_id)
            )
        ).one()
    return {
        "runs": int(totals[0]),
        "status_counts": {str(name): int(count) for name, count in status_rows},
        "execution_paths": {
            str(name): {
                "runs": int(count),
                "average_model_calls": round(float(model_calls), 2),
                "average_model_duration_ms": round(float(model_ms), 2),
            }
            for name, count, model_calls, model_ms in path_rows
        },
        "workload_lane_counts": {
            str(name): int(count) for name, count in lane_rows
        },
        "average_model_calls": round(float(totals[1]), 2),
        "average_tool_calls": round(float(totals[2]), 2),
        "average_replans": round(float(totals[3]), 2),
        "average_model_duration_ms": round(float(totals[4]), 2),
        "average_tool_duration_ms": round(float(totals[5]), 2),
        "average_dependency_wait_ms": round(float(totals[6]), 2),
    }


@app.post("/api/v1/assistant/memories", response_model=MemoryView)
async def save_memory(
    body: MemoryCreate, principal: PrincipalDep, rt: RuntimeDep
) -> MemoryView:
    async with rt.database.sessions() as session, session.begin():
        memory = await session.scalar(
            select(UserMemory)
            .where(
                UserMemory.user_id == principal.user_id,
                UserMemory.key == body.key.strip(),
            )
            .with_for_update()
        )
        if memory is None:
            memory = UserMemory(
                user_id=principal.user_id,
                key=body.key.strip(),
                value=body.value.strip(),
            )
            session.add(memory)
        else:
            memory.value = body.value.strip()
            memory.updated_at = utc_now()
        await session.flush()
    return _memory_view(memory)


@app.delete(
    "/api/v1/assistant/memories/{memory_id}",
    status_code=204,
    response_class=Response,
)
async def delete_memory(
    memory_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> Response:
    async with rt.database.sessions() as session, session.begin():
        memory = await session.scalar(
            select(UserMemory).where(
                UserMemory.id == memory_id,
                UserMemory.user_id == principal.user_id,
            )
        )
        if memory is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        await session.delete(memory)
    return Response(status_code=204)


@app.get(
    "/api/v1/assistant/memory/settings",
    response_model=MemoryProfileView,
)
async def get_memory_settings(
    principal: PrincipalDep, rt: RuntimeDep
) -> MemoryProfileView:
    async with rt.database.sessions() as session, session.begin():
        profile = await session.get(MemoryProfile, principal.user_id)
        if profile is None:
            profile = MemoryProfile(user_id=principal.user_id)
            session.add(profile)
    return _memory_profile_view(profile, rt)


@app.put(
    "/api/v1/assistant/memory/settings",
    response_model=MemoryProfileView,
)
async def update_memory_settings(
    body: MemoryProfileUpdate,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> MemoryProfileView:
    semantic_enabled = body.semantic_enabled and body.episodic_enabled
    async with rt.database.sessions() as session, session.begin():
        profile = await session.get(
            MemoryProfile,
            principal.user_id,
            with_for_update=True,
        )
        if profile is None:
            profile = MemoryProfile(user_id=principal.user_id)
            session.add(profile)
        profile.episodic_enabled = body.episodic_enabled
        profile.semantic_enabled = semantic_enabled
        profile.updated_at = utc_now()
    await rt.memory.sync_semantic_setting(
        user_id=principal.user_id,
        enabled=semantic_enabled,
    )
    return _memory_profile_view(profile, rt)


@app.get(
    "/api/v1/assistant/memory/episodes",
    response_model=list[EpisodicMemoryView],
)
async def list_episodic_memories(
    principal: PrincipalDep,
    rt: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[EpisodicMemoryView]:
    async with rt.database.sessions() as session:
        rows = (
            await session.scalars(
                select(EpisodicMemory)
                .where(EpisodicMemory.user_id == principal.user_id)
                .order_by(EpisodicMemory.occurred_at.desc())
                .limit(limit)
            )
        ).all()
    return [_episodic_memory_view(item) for item in rows]


@app.delete(
    "/api/v1/assistant/memory/episodes/{episode_id}",
    status_code=204,
    response_class=Response,
)
async def delete_episodic_memory(
    episode_id: str,
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> Response:
    deleted = await rt.memory.delete_episode(
        user_id=principal.user_id,
        episode_id=episode_id,
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务记忆不存在")
    return Response(status_code=204)


@app.delete("/api/v1/assistant/memory/episodes")
async def clear_episodic_memories(
    principal: PrincipalDep,
    rt: RuntimeDep,
) -> dict:
    deleted = await rt.memory.clear_episodes(user_id=principal.user_id)
    return {"deleted": deleted}


@app.get(
    "/api/v1/assistant/scheduled-actions",
    response_model=list[ScheduledActionView],
)
async def list_scheduled_actions(
    principal: PrincipalDep, rt: RuntimeDep
) -> list[ScheduledActionView]:
    async with rt.database.sessions() as session:
        rows = (
            await session.scalars(
                select(ScheduledAction)
                .where(ScheduledAction.user_id == principal.user_id)
                .order_by(ScheduledAction.run_at.desc())
                .limit(50)
            )
        ).all()
    return [_scheduled_view(item) for item in rows]


@app.get(
    "/api/v1/assistant/scheduled-actions/{action_id}/attempts",
    response_model=list[ScheduledActionAttemptView],
)
async def list_scheduled_action_attempts(
    action_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> list[ScheduledActionAttemptView]:
    async with rt.database.sessions() as session:
        action = await session.scalar(
            select(ScheduledAction).where(
                ScheduledAction.id == action_id,
                ScheduledAction.user_id == principal.user_id,
            )
        )
        if action is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
        attempts = (
            await session.scalars(
                select(ScheduledActionAttempt)
                .where(ScheduledActionAttempt.action_id == action_id)
                .order_by(ScheduledActionAttempt.attempt)
            )
        ).all()
    return [
        ScheduledActionAttemptView(
            attempt=item.attempt,
            status=item.status,
            worker_id=item.worker_id,
            result=item.result,
            error=item.error,
            started_at=item.started_at,
            completed_at=item.completed_at,
        )
        for item in attempts
    ]


@app.delete(
    "/api/v1/assistant/scheduled-actions/{action_id}",
    response_model=ScheduledActionView,
)
async def cancel_scheduled_action(
    action_id: str, principal: PrincipalDep, rt: RuntimeDep
) -> ScheduledActionView:
    async with rt.database.sessions() as session, session.begin():
        action = await session.scalar(
            select(ScheduledAction)
            .where(
                ScheduledAction.id == action_id,
                ScheduledAction.user_id == principal.user_id,
            )
            .with_for_update()
        )
        if action is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
        if action.status not in {"SCHEDULED", "RETRYING"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "当前状态不能取消该定时任务"
            )
        if action.capability_id:
            try:
                await rt.community.revoke_capability(
                    access_token=principal.token,
                    capability_id=action.capability_id,
                )
            except Exception as exc:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "Java 能力撤销失败，定时任务尚未取消，请重试",
                ) from exc
        action.status = "CANCELLED"
        action.capability_token = None
        action.lease_owner = None
        action.lease_expires_at = None
    return _scheduled_view(action)


async def _owned_conversation(
    rt: Runtime, conversation_id: str, user_id: str
) -> Conversation:
    async with rt.database.sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "对话不存在")
    return conversation


async def _owned_run(rt: Runtime, run_id: str, user_id: str) -> Run:
    async with rt.database.sessions() as session:
        run = await session.scalar(
            select(Run).where(
                Run.id == run_id,
                Run.user_id == user_id,
            )
        )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return run


def _conversation_view(item: Conversation) -> ConversationView:
    return ConversationView(
        conversation_id=item.id,
        title=item.title,
        context_post_id=item.context_post_id,
        surface=item.surface,
        updated_at=item.updated_at,
    )


def _run_view(run: Run) -> RunView:
    steps = sorted(run.steps, key=lambda item: item.ordinal)
    approval = next(
        (
            item
            for item in sorted(run.approvals, key=lambda value: value.created_at)
            if item.status == "PENDING"
        ),
        None,
    )
    return RunView(
        run_id=run.id,
        conversation_id=run.conversation_id,
        goal=run.prompt,
        status=run.status,
        execution_path=run.execution_path,
        workload_lane=run.workload_lane,
        intent=run.intent,
        summary=run.summary,
        final_response=run.final_response,
        error=run.error,
        trace_id=run.trace_id,
        budget={
            "model_calls": run.model_calls,
            "max_model_calls": run.max_model_calls,
            "tool_calls": run.tool_calls,
            "max_tool_calls": run.max_tool_calls,
            "replan_count": run.replan_count,
            "max_replans": run.max_replans,
        },
        timing={
            "queue_ms": _elapsed_ms(run.created_at, run.started_at),
            "model_ms": run.model_duration_ms,
            "tool_ms": run.tool_duration_ms,
            "dependency_wait_ms": run.dependency_wait_ms,
            "total_ms": _elapsed_ms(run.created_at, run.completed_at),
        },
        intent_detail=dict(run.intent_detail) if run.intent_detail else None,
        task_ledger=dict(run.task_ledger or {}),
        progress_ledger=dict(run.progress_ledger or {}),
        approval=(
            ApprovalView(
                approval_id=approval.id,
                action=approval.action,
                status=approval.status,
                description=approval.description,
                preview=dict(approval.preview or {}),
                expires_at=approval.expires_at,
                expected_run_version=approval.expected_run_version,
            )
            if approval
            else None
        ),
        steps=[
            StepView(
                step_id=item.id,
                ordinal=item.ordinal,
                kind=item.kind,
                tool_name=item.tool_name,
                label=item.label,
                status=item.status,
                output=item.output,
                error=item.error,
                started_at=item.started_at,
                completed_at=item.completed_at,
                task_key=item.task_key,
                agent_name=item.agent_name,
                capabilities=list(item.capabilities or []),
                depends_on=list(item.depends_on or []),
                attempts=item.attempts,
                max_attempts=item.max_attempts,
            )
            for item in steps
        ],
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_list_item_view(run: Run) -> RunListItemView:
    steps = sorted(run.steps, key=lambda item: item.ordinal)
    approval = next(
        (
            item
            for item in sorted(run.approvals, key=lambda value: value.created_at)
            if item.status == "PENDING"
        ),
        None,
    )
    creator_task_ids = {
        str(item.output["creator_task_id"])
        for item in steps
        if isinstance(item.output, dict)
        and item.output.get("creator_task_id")
    }
    return RunListItemView(
        run_id=run.id,
        conversation_id=run.conversation_id,
        goal=run.prompt,
        status=run.status,
        intent=run.intent,
        summary=run.summary,
        error=run.error,
        trace_id=run.trace_id,
        approval=(
            ApprovalView(
                approval_id=approval.id,
                action=approval.action,
                status=approval.status,
                description=approval.description,
                preview=dict(approval.preview or {}),
                expires_at=approval.expires_at,
                expected_run_version=approval.expected_run_version,
            )
            if approval
            else None
        ),
        steps=[
            RunListStepView(
                step_id=item.id,
                label=item.label,
                status=item.status,
            )
            for item in steps
        ],
        creator_task_ids=sorted(creator_task_ids),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _reload_run(rt: Runtime, run_id: str) -> RunView:
    async with rt.database.sessions() as session:
        refreshed = await session.scalar(
            select(Run)
            .options(selectinload(Run.steps), selectinload(Run.approvals))
            .where(Run.id == run_id)
        )
    assert refreshed is not None
    return _run_view(refreshed)


def _elapsed_ms(started_at, completed_at) -> int | None:
    if started_at is None or completed_at is None:
        return None
    return max(0, int((completed_at - started_at).total_seconds() * 1000))


def _scheduled_view(item: ScheduledAction) -> ScheduledActionView:
    return ScheduledActionView(
        action_id=item.id,
        run_id=item.run_id,
        draft_id=item.draft_id,
        instruction=item.instruction,
        run_at=item.run_at,
        status=item.status,
        attempts=item.attempts,
        result=item.result,
        error=item.error,
    )


def _tool_job_view(item: ToolJob) -> ToolJobView:
    return ToolJobView(
        job_id=item.id,
        run_id=item.run_id,
        step_ordinal=item.step_ordinal,
        tool_name=item.tool_name,
        status=item.status,
        attempts=item.attempts,
        max_attempts=item.max_attempts,
        next_attempt_at=item.next_attempt_at,
        result=dict(item.result) if item.result else None,
        error=item.error,
        dead_lettered_at=item.dead_lettered_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _memory_view(item: UserMemory) -> MemoryView:
    return MemoryView(
        memory_id=item.id,
        key=item.key,
        value=item.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _memory_profile_view(item: MemoryProfile, rt: Runtime) -> MemoryProfileView:
    health = rt.memory.health()
    return MemoryProfileView(
        episodic_enabled=item.episodic_enabled,
        semantic_enabled=item.semantic_enabled,
        retention_days=rt.settings.episodic_memory_retention_days,
        semantic_backend=health.backend,
        embedding_provider=health.embedding,
    )


def _episodic_memory_view(item: EpisodicMemory) -> EpisodicMemoryView:
    return EpisodicMemoryView(
        episode_id=item.id,
        run_id=item.run_id,
        intent=item.intent,
        goal=item.goal,
        summary=item.summary,
        outcome=item.outcome,
        tool_names=list(item.tool_names or []),
        artifact_refs=list(item.artifact_refs or []),
        importance=item.importance,
        occurred_at=item.occurred_at,
        expires_at=item.expires_at,
        recall_count=item.recall_count,
    )
