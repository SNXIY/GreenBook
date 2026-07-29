import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from core import settings
from database import DatabaseManager
from database.base import utc_now
from moderation.models import ModerationReviewCase, ModerationTask
from moderation.repositories import (
    ModerationActionLogRepository,
    ModerationCallbackOutboxRepository,
    ModerationReviewCaseRepository,
    ModerationSignalRepository,
    ModerationTaskRepository,
    TaskStateConflictError,
)
from moderation.schemas import (
    ActionLogEvent,
    AdversarialReviewAudit,
    AgentDecision,
    AgenticPolicyRAGAudit,
    DecisionSource,
    EvidenceReviewerAudit,
    EvidenceReviewerHistoryEntry,
    HumanDecision,
    HumanReviewSubmit,
    ModerationAction,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    ModerationTaskAccepted,
    ModerationTaskCreate,
    ModerationTaskDetail,
    ModerationTaskStatus,
    ModerationTaskSummary,
    RiskType,
    evidence_reviewer_audit_from_state,
)
from moderation.services.mappers import log_to_read, task_to_detail, task_to_summary
from moderation.services.ports import (
    KnowledgeIndex,
    NoopKnowledgeIndex,
    NoopReviewQueueIndex,
    ReviewQueueIndex,
)

logger = logging.getLogger(__name__)


class ModerationGraph(Protocol):
    async def ainvoke(
        self,
        input: dict[str, Any] | Command,
        config: RunnableConfig,
        **kwargs: Any,
    ) -> dict[str, Any]: ...
# self.graph.ainvoke(state, config) 实际调的是 LangGraph 编译图的 ainvoke，
# LangGraph 内部按图结构自动跑 START → preprocess → ... → END。
class ModerationWorkflowService:
    def __init__(
        self,
        *,
        database: DatabaseManager,
        graph: ModerationGraph,
        queue_index: ReviewQueueIndex | None = None,
        knowledge_index: KnowledgeIndex | None = None,
    ) -> None:
        self.database = database
        self.graph = graph
        self.queue_index = queue_index or NoopReviewQueueIndex()
        self.knowledge_index = knowledge_index or NoopKnowledgeIndex()
        self.tasks = ModerationTaskRepository()
        self.logs = ModerationActionLogRepository()
        self.callbacks = ModerationCallbackOutboxRepository()
        self.cases = ModerationReviewCaseRepository()
        self.signals = ModerationSignalRepository()

# create_task(request)
    # │
    # ├─ 1. 幂等检查
    # │     idempotency_key = "community:post:{id}:initial"
    # │     → 查到了 → 直接返回已有审核结果（不重复审）
    # │
    # ├─ 2. 建任务入库 PENDING
    # │     INSERT INTO moderation_task
    # │     INSERT INTO action_log (TASK_CREATED)
    # │     → commit
    # │
    # ├─ 3. 异步：立即返回；同步：process_task 跑图
    # │
    # └─ 4. 返回 ModerationTaskAccepted
    async def create_task(self, request: ModerationTaskCreate) -> ModerationTaskAccepted:
        if request.idempotency_key:
            async with self.database.session() as session:
                existing = await self.tasks.find_by_idempotency_key(
                    session,
                    request.idempotency_key,
                )
                if existing is not None:
                    detail = await self._detail_with_signals(session, existing)
                    return ModerationTaskAccepted(
                        task=detail,
                        requires_human_review=(
                            existing.status == ModerationTaskStatus.WAITING_REVIEW
                        ),
                    )
        task_id = uuid4()
        thread_id = str(uuid4())
        async with self.database.session() as session:
            task = await self.tasks.create(
                session,
                task_id=task_id,
                thread_id=thread_id,
                request=request,
            )
            await self.logs.add(
                session,
                task_id=task.id,
                event=ActionLogEvent.TASK_CREATED,
                source=DecisionSource.SYSTEM,
                actor_id=request.creator_id,
                details={"platform": request.platform, "content_id": request.content_id},
            )
            await session.commit()
            detail = await self._detail_with_signals(session, task)

        if settings.MODERATION_ASYNC_ENABLED:
            return ModerationTaskAccepted(
                task=detail,
                requires_human_review=False,
            )

        return await self.process_task(task_id)

    async def process_task(self, task_id: UUID) -> ModerationTaskAccepted:
        """Run the moderation graph for an already-persisted task."""
        async with self.database.session() as session:
            task = await self.tasks.get(session, task_id, for_update=True)
            if task.status not in {
                ModerationTaskStatus.PENDING,
                ModerationTaskStatus.RUNNING,
            }:
                detail = await self._detail_with_signals(session, task)
                return ModerationTaskAccepted(
                    task=detail,
                    requires_human_review=(
                        task.status == ModerationTaskStatus.WAITING_REVIEW
                    ),
                )
            if task.status == ModerationTaskStatus.PENDING:
                task.status = ModerationTaskStatus.RUNNING
                task.locked_at = utc_now()
                task.locked_by = task.locked_by or "process_task"
                task.attempt_count = int(task.attempt_count or 0) + 1
                task.version += 1
                await session.commit()

            thread_id = task.thread_id
            creator_id = task.creator_id
            claim_owner = task.locked_by
            claim_attempt = int(task.attempt_count or 0)
            state = {
                "task_id": str(task.id),
                "thread_id": thread_id,
                "content": task.content,
                "content_type": task.content_type.value,
                "content_id": task.content_id,
                "platform": task.platform,
                "creator_id": creator_id,
                "metadata": task.task_metadata or {},
            }

        config = self._config(task_id, thread_id, creator_id)
        try:
            result = await self.graph.ainvoke(state, config)
            waiting_for_review = bool(result.get("__interrupt__"))
            async with self.database.session() as session:
                task = await self.tasks.get(session, task_id, for_update=True)
                self._require_current_claim(
                    task,
                    expected_owner=claim_owner,
                    expected_attempt=claim_attempt,
                )
                await self.tasks.apply_agent_state(
                    session,
                    task=task,
                    state=result,
                    waiting_for_review=waiting_for_review,
                )
                agent_decision = AgentDecision.model_validate(task.agent_decision)
                signals = [
                    ModerationSignalEvidence.model_validate(signal)
                    for signal in result.get("signals", [])
                ]
                if signals:
                    await self.signals.add_many(
                        session,
                        task_id=task_id,
                        signals=signals,
                    )
                    await self.logs.add(
                        session,
                        task_id=task_id,
                        event=ActionLogEvent.SIGNALS_CAPTURED,
                        source=DecisionSource.SYSTEM,
                        details={"signal_types": [signal.signal_type.value for signal in signals]},
                    )
                context_value = result.get("context_evidence")
                if context_value:
                    context = ModerationContextEvidence.model_validate(context_value)
                    if context.errors:
                        await self.logs.add(
                            session,
                            task_id=task_id,
                            event=ActionLogEvent.CONTEXT_RETRIEVAL_FAILED,
                            source=DecisionSource.SYSTEM,
                            details={"errors": context.errors},
                        )
                evidence_review = evidence_reviewer_audit_from_state(
                    result,
                    entered_human_review=waiting_for_review,
                )
                if evidence_review:
                    for entry in evidence_review.history:
                        await self.logs.add(
                            session,
                            task_id=task_id,
                            event=ActionLogEvent.EVIDENCE_REVIEWED,
                            source=DecisionSource.AGENT,
                            details=entry.model_dump(mode="json"),
                        )
                await self.logs.add(
                    session,
                    task_id=task_id,
                    event=ActionLogEvent.AGENT_DECIDED,
                    source=DecisionSource.AGENT,
                    action=agent_decision.recommended_action,
                    details=_agent_log_details(
                        agent_decision,
                        task.adversarial_review,
                        task.policy_rag,
                        task.evidence_review,
                    ),
                )
                if waiting_for_review:
                    await self.logs.add(
                        session,
                        task_id=task_id,
                        event=ActionLogEvent.REVIEW_REQUESTED,
                        source=DecisionSource.SYSTEM,
                        action=ModerationAction.HUMAN_REVIEW,
                        details={"thread_id": thread_id},
                    )
                else:
                    await self.logs.add(
                        session,
                        task_id=task_id,
                        event=ActionLogEvent.TASK_COMPLETED,
                        source=DecisionSource.SYSTEM,
                        action=task.final_action,
                    )
                await self.callbacks.enqueue(
                    session,
                    task=task,
                    max_attempts=settings.MODERATION_CALLBACK_MAX_ATTEMPTS,
                )
                await session.commit()
                detail = await self._detail_with_signals(session, task)
            if waiting_for_review:
                await self.queue_index.enqueue(task_id, detail.created_at)
            return ModerationTaskAccepted(
                task=detail,
                requires_human_review=waiting_for_review,
            )
        except Exception as exc:
            await self._mark_failed(
                task_id,
                exc,
                expected_owner=claim_owner,
                expected_attempt=claim_attempt,
            )
            raise

    async def claim_next_task(self, *, worker_id: str) -> UUID | None:
        async with self.database.session() as session:
            await self.tasks.reclaim_stale(
                session,
                lease_seconds=settings.MODERATION_WORKER_LEASE_SECONDS,
            )
            claimed = await self.tasks.claim_next(
                session,
                worker_id=worker_id,
                lease_seconds=settings.MODERATION_WORKER_LEASE_SECONDS,
            )
            if claimed is None:
                await session.commit()
                return None
            await session.commit()
            return claimed.id

    async def submit_review(
        self,
        task_id: UUID,
        submission: HumanReviewSubmit,
    ) -> tuple[ModerationTaskDetail, bool]:
        review_case: ModerationReviewCase | None = None
        async with self.database.session() as session:
            current = await self.tasks.get(session, task_id, for_update=True)
            if (
                current.status == ModerationTaskStatus.COMPLETED
                and submission.idempotency_key
                and current.review_idempotency_key == submission.idempotency_key
            ):
                existing_case = await self.cases.get_by_task(session, task_id)
                return await self._detail_with_signals(session, current), existing_case is not None
            if current.status != ModerationTaskStatus.WAITING_REVIEW:
                raise TaskStateConflictError(
                    f"Task {task_id} is {current.status}, not WAITING_REVIEW"
                )
            if (
                submission.expected_version is not None
                and current.version != submission.expected_version
            ):
                raise TaskStateConflictError(
                    f"Task {task_id} version changed from {submission.expected_version} to {current.version}"
                )

            resume_value = submission.model_dump(
                exclude={"expected_version", "idempotency_key"},
                mode="json",
            )
            result = await self.graph.ainvoke(
                Command(resume=resume_value),
                self._config(task_id, current.thread_id, current.creator_id),
            )
            human_decision = HumanDecision.model_validate(result["human_decision"])
            task = await self.tasks.complete_human_review(
                session,
                task_id=task_id,
                expected_version=submission.expected_version,
                decision=human_decision,
                review_idempotency_key=submission.idempotency_key,
            )
            await self.logs.add(
                session,
                task_id=task_id,
                event=ActionLogEvent.HUMAN_DECIDED,
                source=DecisionSource.HUMAN,
                actor_id=human_decision.reviewer_id,
                action=human_decision.action,
                details={
                    "comment": human_decision.comment,
                    "risk_type": (
                        human_decision.risk_type.value
                        if human_decision.risk_type
                        else task.risk_type.value
                        if task.risk_type
                        else None
                    ),
                    "idempotency_key": submission.idempotency_key,
                },
            )
            action_override = (
                task.agent_action is not None
                and task.agent_action != ModerationAction.HUMAN_REVIEW
                and task.agent_action != human_decision.action
            )
            risk_override = bool(
                human_decision.risk_type is not None
                and task.risk_type is not None
                and human_decision.risk_type != task.risk_type
            )
            if action_override or risk_override:
                review_case = await self.cases.create_from_override(
                    session,
                    task=task,
                    human_decision=human_decision,
                )
                await self.logs.add(
                    session,
                    task_id=task_id,
                    event=ActionLogEvent.CASE_CREATED,
                    source=DecisionSource.SYSTEM,
                    details={"case_id": str(review_case.id)},
                )
            await self.logs.add(
                session,
                task_id=task_id,
                event=ActionLogEvent.TASK_COMPLETED,
                source=DecisionSource.SYSTEM,
                action=human_decision.action,
            )
            await self.callbacks.enqueue(
                session,
                task=task,
                max_attempts=settings.MODERATION_CALLBACK_MAX_ATTEMPTS,
            )
            await session.commit()
            detail = await self._detail_with_signals(session, task)

        await self.queue_index.remove(task_id)
        if review_case is not None:
            try:
                await self.knowledge_index.index_case(review_case)
            except Exception:
                logger.exception("Failed to index moderation review case %s", review_case.id)
        return detail, review_case is not None

    async def record_event(
        self,
        task_id: UUID,
        *,
        event: ActionLogEvent,
        details: dict[str, Any],
        actor_id: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            await self.tasks.get(session, task_id)
            await self.logs.add(
                session,
                task_id=task_id,
                event=event,
                source=DecisionSource.SYSTEM,
                actor_id=actor_id,
                details=details,
            )
            await session.commit()

    async def add_signals(
        self,
        task_id: UUID,
        signals: list[ModerationSignalEvidence],
    ) -> None:
        if not signals:
            return
        async with self.database.session() as session:
            await self.tasks.get(session, task_id)
            await self.signals.add_many(session, task_id=task_id, signals=signals)
            await self.logs.add(
                session,
                task_id=task_id,
                event=ActionLogEvent.SIGNALS_CAPTURED,
                source=DecisionSource.SYSTEM,
                details={"signal_types": [signal.signal_type.value for signal in signals]},
            )
            await session.commit()

    async def get_task(self, task_id: UUID) -> ModerationTaskDetail:
        async with self.database.session() as session:
            task = await self.tasks.get(session, task_id)
            return await self._detail_with_signals(session, task)

    async def list_tasks(
        self,
        *,
        status: ModerationTaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModerationTaskSummary]:
        async with self.database.session() as session:
            tasks = await self.tasks.list_tasks(
                session,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [task_to_summary(task) for task in tasks]

    async def get_task_outcomes(
        self,
        task_ids: list[UUID],
    ) -> dict[UUID, tuple[RiskType | None, ModerationAction | None]]:
        async with self.database.session() as session:
            return await self.tasks.outcomes_by_ids(
                session,
                list(dict.fromkeys(task_ids)),
            )

    async def list_logs(self, task_id: UUID):
        async with self.database.session() as session:
            await self.tasks.get(session, task_id)
            return [log_to_read(log) for log in await self.logs.list_for_task(session, task_id)]

    def _config(self, task_id: UUID, thread_id: str, creator_id: str | None) -> RunnableConfig:
        return RunnableConfig(
            configurable={
                "thread_id": thread_id,
                "user_id": creator_id or str(task_id),
                "moderation_task_id": str(task_id),
            },
            tags=["moderation"],
            metadata={"moderation_task_id": str(task_id)},
            run_name="content-moderation",
            recursion_limit=settings.MODERATION_GRAPH_RECURSION_LIMIT,
        )

    @staticmethod
    def _require_current_claim(
        task: ModerationTask,
        *,
        expected_owner: str | None,
        expected_attempt: int,
    ) -> None:
        if (
            task.status != ModerationTaskStatus.RUNNING
            or task.locked_by != expected_owner
            or int(task.attempt_count or 0) != expected_attempt
        ):
            raise TaskStateConflictError(
                f"Stale moderation worker result rejected for task {task.id}"
            )

    async def _mark_failed(
        self,
        task_id: UUID,
        exc: Exception,
        *,
        expected_owner: str | None,
        expected_attempt: int,
    ) -> None:
        try:
            async with self.database.session() as session:
                current = await self.tasks.get(session, task_id, for_update=True)
                try:
                    self._require_current_claim(
                        current,
                        expected_owner=expected_owner,
                        expected_attempt=expected_attempt,
                    )
                except TaskStateConflictError:
                    logger.warning(
                        "Ignored stale moderation failure for task %s "
                        "(owner=%s attempt=%s)",
                        task_id,
                        expected_owner,
                        expected_attempt,
                    )
                    return
                failed = await self.tasks.mark_failed(
                    session,
                    task_id=task_id,
                    error_message=str(exc) or type(exc).__name__,
                )
                await self.logs.add(
                    session,
                    task_id=task_id,
                    event=ActionLogEvent.TASK_FAILED,
                    source=DecisionSource.SYSTEM,
                    details={"error_type": type(exc).__name__},
                )
                await self.callbacks.enqueue(
                    session,
                    task=failed,
                    max_attempts=settings.MODERATION_CALLBACK_MAX_ATTEMPTS,
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to mark moderation task %s as failed", task_id)

    async def _detail_with_signals(
        self,
        session: AsyncSession,
        task: ModerationTask,
    ) -> ModerationTaskDetail:
        records = await self.signals.list_for_task(session, task.id)
        detail = task_to_detail(task)
        if detail.evidence_review:
            logs = await self.logs.list_for_task(
                session,
                task.id,
                event=ActionLogEvent.EVIDENCE_REVIEWED,
            )
            reviewer_history: list[EvidenceReviewerHistoryEntry] = []
            for log in logs:
                if log.event != ActionLogEvent.EVIDENCE_REVIEWED:
                    continue
                try:
                    reviewer_history.append(
                        EvidenceReviewerHistoryEntry.model_validate(log.details)
                    )
                except (TypeError, ValueError):
                    continue
            detail = detail.model_copy(
                update={
                    "evidence_review": detail.evidence_review.model_copy(
                        update={"history": reviewer_history}
                    )
                }
            )
        return detail.model_copy(
            update={
                "signals": [
                    ModerationSignalEvidence(
                        signal_type=record.signal_type,
                        source=record.source,
                        score=record.score,
                        details=record.details,
                    )
                    for record in records
                ]
            }
        )


def _agent_log_details(
    decision: AgentDecision,
    adversarial_review: dict[str, Any] | None,
    policy_rag: dict[str, Any] | None,
    evidence_review: dict[str, Any] | None,
) -> dict[str, Any]:
    details = decision.model_dump(mode="json")
    if adversarial_review:
        audit = AdversarialReviewAudit.model_validate(adversarial_review)
        metrics = {
            name: value.model_dump(mode="json")
            for name, value in (
                ("risk_investigator", audit.risk_agent_metrics),
                ("safe_advocate", audit.safe_agent_metrics),
                ("adversarial_judge", audit.judge_agent_metrics),
            )
            if value is not None
        }
        details["adversarial_review"] = {
            "initial_risk_type": audit.initial_classification.risk_type.value,
            "initial_risk_score": audit.initial_classification.risk_score,
            "policy_versions": audit.policy_versions,
            "evidence_conflict": audit.evidence_conflict,
            "agent_conflict": audit.agent_conflict,
            "judge_action": (
                audit.judge_agent_result.action.value if audit.judge_agent_result else None
            ),
            "judge_confidence": (
                audit.judge_agent_result.confidence if audit.judge_agent_result else None
            ),
            "entered_human_review": audit.entered_human_review,
            "errors": audit.adversarial_errors,
            "metrics": metrics,
        }
    if policy_rag:
        rag_audit = AgenticPolicyRAGAudit.model_validate(policy_rag)
        summary = rag_audit.evidence_summary
        details["policy_rag"] = {
            "risk_hypotheses": (
                [item.value for item in rag_audit.query_plan.risk_hypotheses]
                if rag_audit.query_plan
                else []
            ),
            "retrieval_mode": (
                rag_audit.query_plan.retrieval_mode.value if rag_audit.query_plan else None
            ),
            "query_count": sum(len(item.queries) for item in rag_audit.query_history),
            "retrieval_rounds": summary.retrieval_rounds if summary else 0,
            "rewrite_count": rag_audit.rewrite_count,
            "applicable_policy_ids": (
                [str(item.policy_id) for item in summary.applicable_policies] if summary else []
            ),
            "partial_policy_ids": (
                [str(item.policy_id) for item in summary.partial_policies] if summary else []
            ),
            "rejected_policy_count": len(rag_audit.rejected_policies),
            "fallback_used": rag_audit.fallback_used,
            "budget_exceeded": rag_audit.budget_exceeded,
            "sufficient": summary.sufficient if summary else False,
            "entered_human_review": rag_audit.entered_human_review,
            "errors": rag_audit.errors,
        }
    if evidence_review:
        reviewer_audit = EvidenceReviewerAudit.model_validate(evidence_review)
        details["evidence_review"] = {
            "passed": reviewer_audit.passed,
            "final_route": reviewer_audit.final_route.value,
            "final_confidence": reviewer_audit.final_confidence,
            "iteration_count": reviewer_audit.iteration_count,
            "revision_count": reviewer_audit.revision_count,
            "tool_revision_count": reviewer_audit.tool_revision_count,
            "policy_revision_count": reviewer_audit.policy_revision_count,
            "judgment_revision_count": reviewer_audit.judgment_revision_count,
            "budget_exceeded": reviewer_audit.budget_exceeded,
            "no_progress": reviewer_audit.no_progress,
            "entered_human_review": reviewer_audit.entered_human_review,
            "errors": reviewer_audit.errors,
        }
    return details
