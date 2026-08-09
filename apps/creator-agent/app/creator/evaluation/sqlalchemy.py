from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.creator.domain.models import CreatorTaskStatus
from app.creator.evaluation.errors import (
    CreatorEvaluationConflictError,
    CreatorEvaluationSnapshotError,
)
from app.creator.evaluation.hashing import canonical_sha256
from app.creator.evaluation.models import (
    CreatorEvaluationObservation,
    EvaluationCaseReport,
    EvaluationExecutionResult,
    EvaluationRunReport,
    EvaluationSnapshotRequest,
    GenerationObservation,
    ObservedEvidence,
    ObservedExecution,
    ObservedPlan,
    ObservedPlanStep,
    ObservedToolCall,
    utc_now,
)
from app.creator.infrastructure.sqlalchemy import (
    CreatorArtifactRow,
    CreatorBase,
    CreatorRunEventRow,
    CreatorRunRow,
    CreatorTaskRow,
)
from app.creator.runtime.models import AgentCapability, ArtifactKind, PlanStepStatus
from app.creator.tools.models import CreatorToolCallStatus
from app.creator.tools.sqlalchemy import CreatorToolCallRow


class CreatorEvaluationRunRow(CreatorBase):
    __tablename__ = "creator_evaluation_runs"
    __table_args__ = (
        Index(
            "ix_creator_evaluation_runs_dataset",
            "tenant_id",
            "dataset_id",
            "dataset_version",
        ),
        Index(
            "ix_creator_evaluation_runs_completed",
            "tenant_id",
            "completed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_name: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(512), nullable=False)
    baseline_evaluation_run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class CreatorEvaluationCaseResultRow(CreatorBase):
    __tablename__ = "creator_evaluation_case_results"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id",
            "case_id",
            name="uq_creator_evaluation_case_results_case",
        ),
        Index(
            "ix_creator_evaluation_case_results_task",
            "tenant_id",
            "creator_id",
            "task_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("creator_evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    limitations_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)


class SqlAlchemyCreatorEvaluationStore:
    backend_name = "postgresql"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(
        self,
        report: EvaluationRunReport,
    ) -> EvaluationExecutionResult:
        async with self._sessions() as session:
            existing = await session.get(CreatorEvaluationRunRow, report.id)
            if existing is not None:
                return _replayed_or_conflict(existing, report)
            session.add(_report_to_row(report))
            try:
                await session.flush()
                session.add_all(_case_to_row(report.id, case) for case in report.cases)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                existing = await session.get(CreatorEvaluationRunRow, report.id)
                if existing is not None:
                    return _replayed_or_conflict(existing, report)
                raise CreatorEvaluationConflictError(
                    "Creator evaluation report write conflicted",
                    details={"evaluation_run_id": report.id},
                ) from exc
        return EvaluationExecutionResult(report=report)

    async def get(self, evaluation_run_id: str) -> EvaluationRunReport | None:
        async with self._sessions() as session:
            row = await session.get(CreatorEvaluationRunRow, evaluation_run_id)
        return _report_from_row(row) if row is not None else None

    async def list_for_task(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[EvaluationRunReport, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorEvaluationRunRow)
                    .join(
                        CreatorEvaluationCaseResultRow,
                        CreatorEvaluationCaseResultRow.evaluation_run_id
                        == CreatorEvaluationRunRow.id,
                    )
                    .where(
                        CreatorEvaluationRunRow.tenant_id == tenant_id,
                        CreatorEvaluationCaseResultRow.tenant_id == tenant_id,
                        CreatorEvaluationCaseResultRow.creator_id == creator_id,
                        CreatorEvaluationCaseResultRow.task_id == task_id,
                    )
                    .distinct()
                    .order_by(
                        CreatorEvaluationRunRow.completed_at.desc(),
                        CreatorEvaluationRunRow.id.desc(),
                    )
                )
            ).all()
        return tuple(_report_from_row(row) for row in rows)


class SqlAlchemyCreatorEvaluationSnapshotReader:
    backend_name = "postgresql"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def capture(
        self,
        request: EvaluationSnapshotRequest,
    ) -> CreatorEvaluationObservation:
        async with self._sessions() as session:
            task = await session.scalar(
                select(CreatorTaskRow).where(
                    CreatorTaskRow.id == request.task_id,
                    CreatorTaskRow.tenant_id == request.tenant_id,
                    CreatorTaskRow.creator_id == request.creator_id,
                )
            )
            if task is None:
                raise CreatorEvaluationSnapshotError(
                    "Creator task was not found in the requested scope",
                    details={"task_id": request.task_id},
                )
            run = await session.scalar(
                select(CreatorRunRow).where(
                    CreatorRunRow.id == request.run_id,
                    CreatorRunRow.task_id == request.task_id,
                )
            )
            if run is None:
                raise CreatorEvaluationSnapshotError(
                    "Creator run was not found for the requested task",
                    details={
                        "task_id": request.task_id,
                        "run_id": request.run_id,
                    },
                )
            events = (
                await session.scalars(
                    select(CreatorRunEventRow)
                    .where(CreatorRunEventRow.run_id == request.run_id)
                    .order_by(CreatorRunEventRow.sequence)
                )
            ).all()
            artifacts = (
                await session.scalars(
                    select(CreatorArtifactRow)
                    .where(
                        CreatorArtifactRow.run_id == request.run_id,
                        CreatorArtifactRow.tenant_id == request.tenant_id,
                        CreatorArtifactRow.creator_id == request.creator_id,
                    )
                    .order_by(
                        CreatorArtifactRow.created_at,
                        CreatorArtifactRow.id,
                    )
                )
            ).all()
            tool_calls = (
                await session.scalars(
                    select(CreatorToolCallRow)
                    .where(
                        CreatorToolCallRow.run_id == request.run_id,
                        CreatorToolCallRow.tenant_id == request.tenant_id,
                        CreatorToolCallRow.creator_id == request.creator_id,
                    )
                    .order_by(
                        CreatorToolCallRow.started_at,
                        CreatorToolCallRow.call_id,
                    )
                )
            ).all()

        limitations: list[str] = []
        plans = _observed_plans(events, limitations)
        executions = _observed_executions(events, limitations)
        evidence = _observed_evidence(artifacts, limitations)
        generation = _observed_generation(artifacts, task.final_artifact_id)
        if generation is None:
            limitations.append("No draft-like generation was present in the run.")
        final_kind = _final_artifact_kind(artifacts, task.final_artifact_id)
        error_codes = {
            str(event.payload_json.get("error_code"))
            for event in events
            if event.type == "agent.failed" and event.payload_json.get("error_code")
        }
        if task.error_code:
            error_codes.add(task.error_code)
        goal = str(task.goal_json.get("text") or "").strip()
        if not goal:
            raise CreatorEvaluationSnapshotError(
                "Creator task goal is missing from persisted state",
                details={"task_id": request.task_id},
            )
        return CreatorEvaluationObservation(
            case_id=request.case_id,
            tenant_id=request.tenant_id,
            creator_id=request.creator_id,
            task_id=request.task_id,
            run_id=request.run_id,
            trace_id=task.trace_id,
            task_status=CreatorTaskStatus(task.status),
            goal=goal,
            final_artifact_kind=final_kind,
            evidence=evidence,
            plans=plans,
            executions=executions,
            tool_calls=tuple(
                ObservedToolCall(
                    call_id=row.call_id,
                    name=row.tool_name,
                    status=CreatorToolCallStatus(row.status),
                    arguments_sha256=row.arguments_sha256,
                    latency_ms=row.latency_ms,
                    error_code=row.error_code,
                )
                for row in tool_calls
            ),
            generation=generation,
            replan_count=max(0, len(plans) - 1),
            runtime_error_codes=tuple(sorted(error_codes)),
            limitations=tuple(dict.fromkeys(limitations)),
            captured_at=self._clock(),
        )


def _report_to_row(report: EvaluationRunReport) -> CreatorEvaluationRunRow:
    return CreatorEvaluationRunRow(
        id=report.id,
        tenant_id=report.tenant_id,
        actor_id=report.actor_id,
        mode=report.mode.value,
        dataset_id=report.dataset_id,
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        request_sha256=report.request_sha256,
        candidate_name=report.candidate_name,
        candidate_version=report.candidate_version,
        evaluator_version=report.evaluator_version,
        baseline_evaluation_run_id=report.baseline_evaluation_run_id,
        outcome=report.outcome.value,
        passed=report.passed,
        overall_score=report.overall_score,
        report_sha256=canonical_sha256(report),
        report_json=report.model_dump(mode="json"),
        started_at=report.started_at,
        completed_at=report.completed_at,
    )


def _case_to_row(
    evaluation_run_id: str,
    case: EvaluationCaseReport,
) -> CreatorEvaluationCaseResultRow:
    return CreatorEvaluationCaseResultRow(
        id=f"{evaluation_run_id}:{case.case_id}",
        evaluation_run_id=evaluation_run_id,
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        creator_id=case.creator_id,
        task_id=case.task_id,
        runtime_run_id=case.run_id,
        trace_id=case.trace_id,
        outcome=case.outcome.value,
        passed=case.passed,
        overall_score=case.overall_score,
        observation_sha256=case.observation_sha256,
        metrics_json=[metric.model_dump(mode="json") for metric in case.metrics],
        limitations_json=list(case.limitations),
    )


def _report_from_row(row: CreatorEvaluationRunRow) -> EvaluationRunReport:
    report = EvaluationRunReport.model_validate(row.report_json)
    if canonical_sha256(report) != row.report_sha256:
        raise CreatorEvaluationConflictError(
            f"Evaluation report {row.id} failed its integrity check",
            details={"evaluation_run_id": row.id},
        )
    return report


def _replayed_or_conflict(
    row: CreatorEvaluationRunRow,
    report: EvaluationRunReport,
) -> EvaluationExecutionResult:
    if row.request_sha256 != report.request_sha256:
        raise CreatorEvaluationConflictError(
            f"Evaluation run {report.id} already exists for another request",
            details={"evaluation_run_id": report.id},
        )
    return EvaluationExecutionResult(report=_report_from_row(row), replayed=True)


def _observed_plans(
    events: Sequence[CreatorRunEventRow],
    limitations: list[str],
) -> tuple[ObservedPlan, ...]:
    plans = []
    for event in events:
        if event.type != "supervisor.plan.created":
            continue
        payload = event.payload_json
        try:
            plans.append(
                ObservedPlan(
                    revision=int(payload["revision"]),
                    reason=str(payload.get("reason") or ""),
                    steps=tuple(
                        ObservedPlanStep(
                            step_id=str(step["step_id"]),
                            capability=AgentCapability(str(step["capability"])),
                            dependencies=tuple(
                                str(value) for value in step.get("dependencies", ())
                            ),
                        )
                        for step in payload.get("steps", ())
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            limitations.append(
                f"Plan event sequence {event.sequence} could not be parsed."
            )
    return tuple(sorted(plans, key=lambda plan: plan.revision))


def _observed_executions(
    events: Sequence[CreatorRunEventRow],
    limitations: list[str],
) -> tuple[ObservedExecution, ...]:
    executions = []
    for event in events:
        if event.type not in {"agent.completed", "agent.failed"}:
            continue
        payload = event.payload_json
        try:
            executions.append(
                ObservedExecution(
                    execution_id=str(payload["execution_id"]),
                    step_id=str(payload["step_id"]),
                    capability=AgentCapability(str(payload["capability"])),
                    agent=str(payload["agent"]),
                    status=(
                        PlanStepStatus.SUCCEEDED
                        if event.type == "agent.completed"
                        else PlanStepStatus.FAILED
                    ),
                    error_code=(
                        str(payload["error_code"])
                        if payload.get("error_code")
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            limitations.append(
                f"Execution event sequence {event.sequence} could not be parsed."
            )
    return tuple(executions)


def _observed_evidence(
    artifacts: Sequence[CreatorArtifactRow],
    limitations: list[str],
) -> tuple[ObservedEvidence, ...]:
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.kind == ArtifactKind.EVIDENCE_PACK.value
    ]
    if not candidates:
        return ()
    latest = max(
        candidates,
        key=lambda artifact: (artifact.revision, artifact.created_at, artifact.id),
    )
    evidence = []
    for rank, item in enumerate(latest.content_json.get("evidence", ()), start=1):
        try:
            evidence.append(
                ObservedEvidence(
                    evidence_id=str(item["id"]),
                    document_id=str(item.get("document_id") or item["id"]),
                    rank=rank,
                    text=str(item.get("summary") or ""),
                    source=(
                        str(item["source"]) if item.get("source") is not None else None
                    ),
                    authority_verified=bool(item.get("authority_verified", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            limitations.append(f"Evidence item at rank {rank} could not be parsed.")
    return tuple(evidence)


def _observed_generation(
    artifacts: Sequence[CreatorArtifactRow],
    final_artifact_id: str | None,
) -> GenerationObservation | None:
    selected: dict[str, Any] | None = None
    if final_artifact_id:
        final = next(
            (artifact for artifact in artifacts if artifact.id == final_artifact_id),
            None,
        )
        if final is not None:
            document = final.content_json.get("document")
            if isinstance(document, dict):
                selected = document
    if selected is None or not selected.get("body_markdown"):
        drafts = [
            artifact
            for artifact in artifacts
            if artifact.kind
            in {
                ArtifactKind.SOURCE_DRAFT.value,
                ArtifactKind.DRAFT.value,
            }
        ]
        if drafts:
            latest = max(
                drafts,
                key=lambda artifact: (
                    artifact.revision,
                    artifact.created_at,
                    artifact.id,
                ),
            )
            selected = latest.content_json
    if selected is None or not str(selected.get("body_markdown") or "").strip():
        return None
    return GenerationObservation(
        title=str(selected.get("title") or ""),
        body_markdown=str(selected["body_markdown"]),
        cited_evidence_ids=tuple(
            str(value) for value in selected.get("evidence_ids", ())
        ),
        declared_unsupported_claims=tuple(
            str(value) for value in selected.get("unsupported_claims", ())
        ),
    )


def _final_artifact_kind(
    artifacts: Sequence[CreatorArtifactRow],
    final_artifact_id: str | None,
) -> ArtifactKind | None:
    if not final_artifact_id:
        return None
    artifact = next(
        (item for item in artifacts if item.id == final_artifact_id),
        None,
    )
    return ArtifactKind(artifact.kind) if artifact is not None else None
