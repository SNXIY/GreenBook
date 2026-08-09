from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.creator.api.errors import (
    CreatorArtifactNotFoundError,
    CreatorDraftListNotFoundError,
    CreatorEventCursorError,
    CreatorTaskCursorError,
)
from app.creator.api.models import (
    CreatorApiPrincipal,
    CreatorArtifactDetail,
    CreatorArtifactSummary,
    CreatorDecisionOptionView,
    CreatorDecisionView,
    CreatorDraftSummary,
    CreatorDraftVersionView,
    CreatorDraftView,
    CreatorEventEnvelope,
    CreatorRunView,
    CreatorTaskListItem,
    CreatorTaskPage,
    CreatorTaskSnapshot,
)
from app.creator.domain.errors import (
    CreatorDecisionNotFoundError,
    CreatorTaskNotFoundError,
)
from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorDecisionStatus,
    CreatorRunStatus,
    CreatorTaskKind,
    CreatorTaskStatus,
)
from app.creator.infrastructure.sqlalchemy import (
    CreatorArtifactRow,
    CreatorHumanDecisionRow,
    CreatorRunEventRow,
    CreatorRunRow,
    CreatorTaskRow,
)
from app.creator.drafts.sqlalchemy import CreatorDraftRow, CreatorDraftVersionRow
from app.creator.runtime.models import ArtifactKind
from app.creator.studio.sqlalchemy import CreatorProjectTaskRow


_PRIVATE_EVENT_KEYS = frozenset(
    {
        "authorization",
        "checkpoint_id",
        "cookie",
        "interrupt_id",
        "messages",
        "raw_prompt",
        "secret",
        "system_prompt",
        "thread_id",
        "token",
    }
)


class SqlAlchemyCreatorWorkspaceQuery:
    """Tenant-scoped read model for the Creator HTTP and SSE boundary."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        default_page_size: int = 20,
    ) -> None:
        self._sessions = sessions
        self._default_page_size = max(1, min(default_page_size, 50))

    async def list_tasks(
        self,
        principal: CreatorApiPrincipal,
        *,
        status: CreatorTaskStatus | None = None,
        kind: CreatorTaskKind | None = None,
        project_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CreatorTaskPage:
        page_size = max(1, min(limit or self._default_page_size, 50))
        statement = (
            select(CreatorTaskRow)
            .where(
                CreatorTaskRow.tenant_id == principal.tenant_id,
                CreatorTaskRow.creator_id == principal.creator_id,
            )
            .order_by(CreatorTaskRow.updated_at.desc(), CreatorTaskRow.id.desc())
            .limit(page_size + 1)
        )
        if status is not None:
            statement = statement.where(CreatorTaskRow.status == status.value)
        if kind is not None:
            statement = statement.where(CreatorTaskRow.kind == kind.value)
        if project_id is not None:
            statement = statement.join(
                CreatorProjectTaskRow,
                CreatorProjectTaskRow.task_id == CreatorTaskRow.id,
            ).where(
                CreatorProjectTaskRow.project_id == project_id,
                CreatorProjectTaskRow.tenant_id == principal.tenant_id,
                CreatorProjectTaskRow.creator_id == principal.creator_id,
            )
        if cursor is not None:
            updated_at, task_id = _decode_task_cursor(cursor)
            statement = statement.where(
                or_(
                    CreatorTaskRow.updated_at < updated_at,
                    and_(
                        CreatorTaskRow.updated_at == updated_at,
                        CreatorTaskRow.id < task_id,
                    ),
                )
            )
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
        has_more = len(rows) > page_size
        visible = rows[:page_size]
        next_cursor = (
            _encode_task_cursor(visible[-1].updated_at, visible[-1].id)
            if has_more and visible
            else None
        )
        return CreatorTaskPage(
            items=tuple(_task_list_item(row) for row in visible),
            next_cursor=next_cursor,
        )

    async def get_task_snapshot(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
    ) -> CreatorTaskSnapshot:
        async with self._sessions() as session:
            task = await self._require_task(session, principal, task_id)
            run = await session.scalar(
                select(CreatorRunRow).where(
                    CreatorRunRow.id == task.active_run_id,
                    CreatorRunRow.task_id == task.id,
                )
            )
            if run is None:
                raise CreatorTaskNotFoundError(
                    "Creator task active run was not found",
                    details={"task_id": task_id},
                )
            artifact_rows = (
                await session.scalars(
                    select(CreatorArtifactRow)
                    .where(
                        CreatorArtifactRow.task_id == task.id,
                        CreatorArtifactRow.tenant_id == principal.tenant_id,
                        CreatorArtifactRow.creator_id == principal.creator_id,
                    )
                    .order_by(
                        CreatorArtifactRow.created_at,
                        CreatorArtifactRow.id,
                    )
                )
            ).all()
            decision = None
            if task.pending_decision_id:
                decision_row = await session.scalar(
                    select(CreatorHumanDecisionRow).where(
                        CreatorHumanDecisionRow.id == task.pending_decision_id,
                        CreatorHumanDecisionRow.task_id == task.id,
                    )
                )
                if decision_row is not None:
                    source = next(
                        (
                            artifact
                            for artifact in artifact_rows
                            if artifact.id == decision_row.source_artifact_id
                        ),
                        None,
                    )
                    decision = _decision_view(decision_row, source)
        return CreatorTaskSnapshot(
            task_id=task.id,
            run_id=task.active_run_id,
            kind=CreatorTaskKind(task.kind),
            goal=str(task.goal_json.get("text") or ""),
            constraints=dict(task.goal_json.get("constraints") or {}),
            source_scope=dict(task.goal_json.get("source_scope") or {}),
            status=CreatorTaskStatus(task.status),
            version=task.version,
            trace_id=task.trace_id,
            pending_decision=decision,
            final_artifact_id=task.final_artifact_id,
            error_code=task.error_code,
            error_message=task.error_message,
            run=_run_view(run),
            artifacts=tuple(_artifact_summary(row) for row in artifact_rows),
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    async def list_decisions(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
    ) -> tuple[CreatorDecisionView, ...]:
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            decisions = (
                await session.scalars(
                    select(CreatorHumanDecisionRow)
                    .where(CreatorHumanDecisionRow.task_id == task_id)
                    .order_by(
                        CreatorHumanDecisionRow.created_at,
                        CreatorHumanDecisionRow.id,
                    )
                )
            ).all()
            source_ids = {decision.source_artifact_id for decision in decisions}
            sources = (
                (
                    await session.scalars(
                        select(CreatorArtifactRow).where(
                            CreatorArtifactRow.id.in_(source_ids),
                            CreatorArtifactRow.task_id == task_id,
                            CreatorArtifactRow.tenant_id == principal.tenant_id,
                            CreatorArtifactRow.creator_id == principal.creator_id,
                        )
                    )
                ).all()
                if source_ids
                else ()
            )
        by_id = {source.id: source for source in sources}
        return tuple(
            _decision_view(decision, by_id.get(decision.source_artifact_id))
            for decision in decisions
        )

    async def get_decision(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
        decision_id: str,
    ) -> CreatorDecisionView:
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            decision = await session.scalar(
                select(CreatorHumanDecisionRow).where(
                    CreatorHumanDecisionRow.id == decision_id,
                    CreatorHumanDecisionRow.task_id == task_id,
                )
            )
            if decision is None:
                raise CreatorDecisionNotFoundError(
                    "Creator decision was not found",
                    details={"task_id": task_id, "decision_id": decision_id},
                )
            source = await session.scalar(
                select(CreatorArtifactRow).where(
                    CreatorArtifactRow.id == decision.source_artifact_id,
                    CreatorArtifactRow.task_id == task_id,
                    CreatorArtifactRow.tenant_id == principal.tenant_id,
                    CreatorArtifactRow.creator_id == principal.creator_id,
                )
            )
        return _decision_view(decision, source)

    async def list_artifacts(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
    ) -> tuple[CreatorArtifactSummary, ...]:
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            rows = (
                await session.scalars(
                    select(CreatorArtifactRow)
                    .where(
                        CreatorArtifactRow.task_id == task_id,
                        CreatorArtifactRow.tenant_id == principal.tenant_id,
                        CreatorArtifactRow.creator_id == principal.creator_id,
                    )
                    .order_by(
                        CreatorArtifactRow.created_at,
                        CreatorArtifactRow.id,
                    )
                )
            ).all()
        return tuple(_artifact_summary(row) for row in rows)

    async def get_artifact(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
        artifact_id: str,
    ) -> CreatorArtifactDetail:
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            row = await session.scalar(
                select(CreatorArtifactRow).where(
                    CreatorArtifactRow.id == artifact_id,
                    CreatorArtifactRow.task_id == task_id,
                    CreatorArtifactRow.tenant_id == principal.tenant_id,
                    CreatorArtifactRow.creator_id == principal.creator_id,
                )
            )
        if row is None:
            raise CreatorArtifactNotFoundError(
                "Creator artifact was not found",
                details={"task_id": task_id, "artifact_id": artifact_id},
            )
        return _artifact_detail(row)

    async def list_events(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int = 250,
    ) -> tuple[CreatorEventEnvelope, ...]:
        page_size = max(1, min(limit, 500))
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            statement = (
                select(CreatorRunEventRow)
                .join(
                    CreatorRunRow,
                    CreatorRunRow.id == CreatorRunEventRow.run_id,
                )
                .where(CreatorRunEventRow.task_id == task_id)
                .order_by(
                    CreatorRunRow.attempt,
                    CreatorRunEventRow.sequence,
                )
                .limit(page_size)
            )
            if after_event_id is not None:
                run_id, sequence = _decode_event_id(after_event_id)
                anchor_attempt = await session.scalar(
                    select(CreatorRunRow.attempt)
                    .join(
                        CreatorRunEventRow,
                        CreatorRunEventRow.run_id == CreatorRunRow.id,
                    )
                    .where(
                        CreatorRunRow.task_id == task_id,
                        CreatorRunRow.id == run_id,
                        CreatorRunEventRow.sequence == sequence,
                    )
                )
                if anchor_attempt is None:
                    raise CreatorEventCursorError(
                        "Last-Event-ID does not identify an event in this task",
                        details={"task_id": task_id},
                    )
                statement = statement.where(
                    or_(
                        CreatorRunRow.attempt > anchor_attempt,
                        and_(
                            CreatorRunRow.id == run_id,
                            CreatorRunEventRow.sequence > sequence,
                        ),
                    )
                )
            rows = (await session.scalars(statement)).all()
        return tuple(_event_envelope(row) for row in rows)

    async def list_task_drafts(
        self,
        principal: CreatorApiPrincipal,
        task_id: str,
    ) -> tuple[CreatorDraftSummary, ...]:
        async with self._sessions() as session:
            await self._require_task(session, principal, task_id)
            rows = (
                await session.scalars(
                    select(CreatorDraftRow)
                    .where(
                        CreatorDraftRow.task_id == task_id,
                        CreatorDraftRow.tenant_id == principal.tenant_id,
                        CreatorDraftRow.creator_id == principal.creator_id,
                    )
                    .order_by(
                        CreatorDraftRow.updated_at.desc(),
                        CreatorDraftRow.id.desc(),
                    )
                )
            ).all()
        return tuple(_draft_summary(row) for row in rows)

    async def get_draft(
        self,
        principal: CreatorApiPrincipal,
        draft_id: str,
    ) -> CreatorDraftView:
        async with self._sessions() as session:
            draft = await session.scalar(
                select(CreatorDraftRow).where(
                    CreatorDraftRow.id == draft_id,
                    CreatorDraftRow.tenant_id == principal.tenant_id,
                    CreatorDraftRow.creator_id == principal.creator_id,
                )
            )
            if draft is None:
                raise CreatorDraftListNotFoundError(
                    "Creator draft was not found",
                    details={"draft_id": draft_id},
                )
            version = await session.get(
                CreatorDraftVersionRow,
                {"draft_id": draft.id, "version": draft.current_version},
            )
        if version is None:
            raise CreatorDraftListNotFoundError(
                "Creator draft current version was not found",
                details={"draft_id": draft_id},
            )
        return _draft_view(draft, version)

    async def list_draft_versions(
        self,
        principal: CreatorApiPrincipal,
        draft_id: str,
    ) -> tuple[CreatorDraftVersionView, ...]:
        async with self._sessions() as session:
            draft = await session.scalar(
                select(CreatorDraftRow).where(
                    CreatorDraftRow.id == draft_id,
                    CreatorDraftRow.tenant_id == principal.tenant_id,
                    CreatorDraftRow.creator_id == principal.creator_id,
                )
            )
            if draft is None:
                raise CreatorDraftListNotFoundError(
                    "Creator draft was not found",
                    details={"draft_id": draft_id},
                )
            versions = (
                await session.scalars(
                    select(CreatorDraftVersionRow)
                    .where(
                        CreatorDraftVersionRow.draft_id == draft_id,
                        CreatorDraftVersionRow.tenant_id == principal.tenant_id,
                        CreatorDraftVersionRow.creator_id == principal.creator_id,
                    )
                    .order_by(CreatorDraftVersionRow.version.desc())
                )
            ).all()
        return tuple(_draft_version(row) for row in versions)

    async def list_runnable_run_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        now = datetime.now(timezone.utc)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorRunRow.id)
                    .where(
                        or_(
                            CreatorRunRow.status.in_(
                                (
                                    CreatorRunStatus.QUEUED.value,
                                    CreatorRunStatus.RETRYING.value,
                                )
                            ),
                            and_(
                                CreatorRunRow.status == CreatorRunStatus.RUNNING.value,
                                CreatorRunRow.lease_expires_at.is_not(None),
                                CreatorRunRow.lease_expires_at <= now,
                            ),
                        )
                    )
                    .order_by(CreatorRunRow.updated_at, CreatorRunRow.id)
                    .limit(max(1, min(limit, 500)))
                )
            ).all()
        return tuple(rows)

    @staticmethod
    async def _require_task(
        session: AsyncSession,
        principal: CreatorApiPrincipal,
        task_id: str,
    ) -> CreatorTaskRow:
        row = await session.scalar(
            select(CreatorTaskRow).where(
                CreatorTaskRow.id == task_id,
                CreatorTaskRow.tenant_id == principal.tenant_id,
                CreatorTaskRow.creator_id == principal.creator_id,
            )
        )
        if row is None:
            raise CreatorTaskNotFoundError(
                "Creator task was not found",
                details={"task_id": task_id},
            )
        return row


def _task_list_item(row: CreatorTaskRow) -> CreatorTaskListItem:
    return CreatorTaskListItem(
        task_id=row.id,
        run_id=row.active_run_id,
        kind=CreatorTaskKind(row.kind),
        goal=str(row.goal_json.get("text") or ""),
        status=CreatorTaskStatus(row.status),
        version=row.version,
        pending_decision_id=row.pending_decision_id,
        final_artifact_id=row.final_artifact_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run_view(row: CreatorRunRow) -> CreatorRunView:
    return CreatorRunView(
        run_id=row.id,
        attempt=row.attempt,
        status=CreatorRunStatus(row.status),
        execution_attempts=row.execution_attempts,
        error_code=row.error_code,
        retryable=row.retryable,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _artifact_summary(row: CreatorArtifactRow) -> CreatorArtifactSummary:
    return CreatorArtifactSummary(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        producer=row.producer,
        revision=row.revision,
        confidence=row.confidence,
        content_sha256=row.content_sha256,
        created_at=row.created_at,
    )


def _artifact_detail(row: CreatorArtifactRow) -> CreatorArtifactDetail:
    return CreatorArtifactDetail(
        **_artifact_summary(row).model_dump(),
        content=dict(row.content_json),
        parent_ids=tuple(row.parent_ids_json),
        metadata=dict(row.metadata_json),
    )


def _decision_view(
    row: CreatorHumanDecisionRow,
    source: CreatorArtifactRow | None,
) -> CreatorDecisionView:
    options: tuple[CreatorDecisionOptionView, ...] = ()
    source_view = _artifact_detail(source) if source is not None else None
    if source is not None and source.kind == ArtifactKind.TOPIC_OPTIONS.value:
        recommended = str(source.content_json.get("recommended_option_id") or "")
        raw_options = source.content_json.get("options")
        if isinstance(raw_options, list):
            options = tuple(
                CreatorDecisionOptionView(
                    option_id=str(option.get("id") or ""),
                    title=str(option.get("title") or ""),
                    angle=str(option.get("angle") or ""),
                    audience_value=str(option.get("audience_value") or ""),
                    risk_note=str(option.get("risk_note") or ""),
                    recommended=str(option.get("id") or "") == recommended,
                    recommendation=str(option.get("recommendation") or ""),
                    why_now=str(option.get("why_now") or ""),
                    reader_question=str(option.get("reader_question") or ""),
                    differentiation=str(option.get("differentiation") or ""),
                    evidence_ids=tuple(
                        str(item)
                        for item in (option.get("evidence_ids") or ())
                        if str(item).strip()
                    ),
                    comment_ids=tuple(
                        str(item)
                        for item in (option.get("comment_ids") or ())
                        if str(item).strip()
                    ),
                )
                for option in raw_options
                if isinstance(option, dict) and option.get("id") and option.get("title")
            )
    return CreatorDecisionView(
        decision_id=row.id,
        task_id=row.task_id,
        run_id=row.run_id,
        kind=CreatorDecisionKind(row.kind),
        prompt=row.prompt,
        source_artifact_id=row.source_artifact_id,
        allowed_actions=tuple(
            CreatorDecisionAction(value) for value in row.allowed_actions_json
        ),
        options=options,
        source=source_view,
        status=CreatorDecisionStatus(row.status),
        version=row.version,
        action=CreatorDecisionAction(row.action) if row.action else None,
        selected_option_id=row.selected_option_id,
        feedback=row.feedback,
        created_at=row.created_at,
        submitted_at=row.submitted_at,
        applied_at=row.applied_at,
    )


def _event_envelope(row: CreatorRunEventRow) -> CreatorEventEnvelope:
    return CreatorEventEnvelope(
        event_id=f"{row.run_id}:{row.sequence}",
        sequence=row.sequence,
        type=row.type,
        task_id=row.task_id,
        run_id=row.run_id,
        timestamp=row.created_at,
        trace_id=row.trace_id,
        payload=_public_event_value(row.payload_json),
    )


def _public_event_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public_event_value(item)
            for key, item in value.items()
            if str(key).casefold() not in _PRIVATE_EVENT_KEYS
            and not str(key).casefold().endswith("_token")
        }
    if isinstance(value, (list, tuple)):
        return [_public_event_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _draft_summary(row: CreatorDraftRow) -> CreatorDraftSummary:
    return CreatorDraftSummary(
        draft_id=row.id,
        task_id=row.task_id,
        title=row.title,
        current_version=row.current_version,
        status=row.status,
        updated_at=row.updated_at,
    )


def _draft_version(row: CreatorDraftVersionRow) -> CreatorDraftVersionView:
    return CreatorDraftVersionView(
        draft_id=row.draft_id,
        version=row.version,
        title=row.title,
        content_markdown=row.content_markdown,
        content_sha256=row.content_sha256,
        source_artifact_id=row.source_artifact_id,
        editor_type=row.editor_type,
        actor_id=row.actor_id,
        created_at=row.created_at,
    )


def _draft_view(
    draft: CreatorDraftRow,
    version: CreatorDraftVersionRow,
) -> CreatorDraftView:
    return CreatorDraftView(
        draft_id=draft.id,
        task_id=draft.task_id,
        title=draft.title,
        current_version=draft.current_version,
        status=draft.status,
        version=_draft_version(version),
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _encode_task_cursor(updated_at: datetime, task_id: str) -> str:
    payload = json.dumps(
        {"updated_at": updated_at.isoformat(), "task_id": task_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_task_cursor(value: str) -> tuple[datetime, str]:
    if not value or len(value) > 1_024:
        raise CreatorTaskCursorError("Task cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        task_id = str(payload["task_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CreatorTaskCursorError("Task cursor is invalid") from exc
    if not task_id or len(task_id) > 64:
        raise CreatorTaskCursorError("Task cursor is invalid")
    return updated_at, task_id


def _decode_event_id(value: str) -> tuple[str, int]:
    if not value or len(value) > 256:
        raise CreatorEventCursorError("Last-Event-ID is invalid")
    try:
        run_id, raw_sequence = value.rsplit(":", 1)
        sequence = int(raw_sequence)
    except (ValueError, TypeError) as exc:
        raise CreatorEventCursorError("Last-Event-ID is invalid") from exc
    if not run_id or len(run_id) > 64 or sequence < 1:
        raise CreatorEventCursorError("Last-Event-ID is invalid")
    return run_id, sequence
