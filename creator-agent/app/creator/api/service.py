from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.creator.api.dispatcher import CreatorRunDispatcher
from app.creator.api.models import (
    CreatorApiPrincipal,
    CreatorDecisionResponseRequest,
    CreatorDraftCreateRequest,
    CreatorDraftUpdateRequest,
    CreatorDraftVersionView,
    CreatorDraftView,
    CreatorPublicationHandoffRequest,
    CreatorPublicationHandoffView,
    CreatorTaskAcceptedResponse,
    CreatorTaskCreateRequest,
    CreatorTaskMutationResponse,
    CreatorTaskVersionRequest,
)
from app.creator.api.query import SqlAlchemyCreatorWorkspaceQuery
from app.creator.application.harness import CreatorAgentHarness
from app.creator.domain.models import (
    CancelCreatorTaskCommand,
    CreateCreatorTaskCommand,
    RetryCreatorTaskCommand,
    SubmitCreatorDecisionCommand,
)
from app.creator.drafts.models import CreatorDraftWriteResult
from app.creator.drafts.service import CreatorDraftService
from app.creator.infrastructure.sqlalchemy import CreatorTaskRow, _task_from_row
from app.creator.publication.errors import CreatorPublicationNotReadyError
from app.creator.publication.service import CreatorPublicationHandoffService
from app.creator.runtime.ports import CreatorArtifactStore
from app.creator.studio.service import CreatorStudioService


class CreatorWorkspaceService:
    """Command-side facade used by HTTP routes and kept free of FastAPI types."""

    def __init__(
        self,
        *,
        harness: CreatorAgentHarness,
        query: SqlAlchemyCreatorWorkspaceQuery,
        drafts: CreatorDraftService,
        publication: CreatorPublicationHandoffService,
        artifact_store: CreatorArtifactStore,
        sessions: async_sessionmaker[AsyncSession],
        dispatcher: CreatorRunDispatcher,
        studio: CreatorStudioService | None = None,
        worker_prefix: str = "creator-api",
    ) -> None:
        self._harness = harness
        self._query = query
        self._drafts = drafts
        self._publication = publication
        self._artifact_store = artifact_store
        self._sessions = sessions
        self._dispatcher = dispatcher
        self._studio = studio
        self._worker_prefix = worker_prefix
        self._outbox_execution = dispatcher.execution_mode == "outbox-worker"

    async def create_task(
        self,
        principal: CreatorApiPrincipal,
        request: CreatorTaskCreateRequest,
        *,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> CreatorTaskAcceptedResponse:
        constraints = request.constraints.runtime_values()
        if self._studio is not None:
            await self._studio.validate_task_context(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                project_id=request.project_id,
                material_ids=request.material_ids,
            )
        if self._studio is not None and request.material_ids:
            material_context = await self._studio.build_material_context(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                material_ids=request.material_ids,
            )
            existing_notes = str(constraints.get("reference_notes") or "").strip()
            constraints["reference_notes"] = (
                "\n\n".join(
                    item
                    for item in (
                        existing_notes,
                        "以下为本次任务明确选用的创作素材：\n" + material_context,
                    )
                    if item
                )
            )[:12_000]
        result = await self._harness.create_task(
            CreateCreatorTaskCommand(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                kind=request.kind,
                goal=request.goal,
                session_id=request.session_id,
                constraints=constraints,
                source_scope=request.source_scope.runtime_values(),
                idempotency_key=idempotency_key,
                trace_id=trace_id,
            )
        )
        if self._studio is not None:
            await self._studio.attach_task_context(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                task_id=result.task_id,
                project_id=request.project_id,
                material_ids=request.material_ids,
            )
        self._dispatcher.schedule_run(result.run_id)
        return CreatorTaskAcceptedResponse(
            task_id=result.task_id,
            run_id=result.run_id,
            status=result.status,
            version=result.version,
            events_url=f"/api/v1/creator/tasks/{result.task_id}/events",
            trace_id=result.trace_id,
            replayed=result.replayed,
        )

    async def submit_decision(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
        decision_id: str,
        request: CreatorDecisionResponseRequest,
        idempotency_key: str,
    ) -> CreatorTaskMutationResponse:
        command = SubmitCreatorDecisionCommand(
            tenant_id=principal.tenant_id,
            creator_id=principal.creator_id,
            task_id=task_id,
            decision_id=decision_id,
            action=request.action,
            actor_id=principal.creator_id,
            selected_option_id=request.selected_option_id,
            feedback=request.feedback,
            edited_payload=request.edited_payload,
            expected_version=request.expected_task_version,
            idempotency_key=idempotency_key,
        )
        if self._outbox_execution:
            result = await self._harness.enqueue_decision(command)
        else:
            result = await self._harness.submit_decision(
                command,
                worker_id=f"{self._worker_prefix}:decision:{uuid.uuid4().hex}",
            )
        return CreatorTaskMutationResponse(
            task_id=result.task_id,
            run_id=result.run_id,
            task_status=result.task_status,
            run_status=result.run_status,
            task_version=result.task_version,
            final_artifact_id=result.final_artifact_id,
            pending_decision_id=result.pending_decision_id,
            applied_decision_id=result.applied_decision_id,
            replayed=result.replayed,
        )

    async def cancel_task(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
        request: CreatorTaskVersionRequest,
    ) -> CreatorTaskMutationResponse:
        await self._harness.request_cancel(
            CancelCreatorTaskCommand(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                task_id=task_id,
                expected_version=request.expected_task_version,
            )
        )
        snapshot = await self._query.get_task_snapshot(principal, task_id)
        return CreatorTaskMutationResponse(
            task_id=snapshot.task_id,
            run_id=snapshot.run_id,
            task_status=snapshot.status,
            run_status=snapshot.run.status,
            task_version=snapshot.version,
            final_artifact_id=snapshot.final_artifact_id,
            pending_decision_id=(
                snapshot.pending_decision.decision_id
                if snapshot.pending_decision
                else None
            ),
        )

    async def retry_task(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
        request: CreatorTaskVersionRequest,
        idempotency_key: str,
    ) -> CreatorTaskMutationResponse:
        result = await self._harness.retry_task(
            RetryCreatorTaskCommand(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                task_id=task_id,
                expected_version=request.expected_task_version,
                idempotency_key=idempotency_key,
            )
        )
        self._dispatcher.schedule_run(result.run_id)
        return CreatorTaskMutationResponse(
            task_id=result.task_id,
            run_id=result.run_id,
            task_status=result.status,
            task_version=result.version,
            pending_decision_id=result.pending_decision_id,
            replayed=result.replayed,
        )

    async def create_draft(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
        request: CreatorDraftCreateRequest,
        idempotency_key: str,
    ) -> CreatorDraftView:
        await self._query.get_task_snapshot(principal, task_id)
        if request.source_artifact_id:
            await self._query.get_artifact(
                principal,
                task_id,
                request.source_artifact_id,
            )
        result = await self._drafts.save_draft(
            tenant_id=principal.tenant_id,
            creator_id=principal.creator_id,
            task_id=task_id,
            title=request.title,
            content_markdown=request.content_markdown,
            source_artifact_id=request.source_artifact_id,
            editor_type="HUMAN",
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
        )
        return _draft_result_view(result)

    async def update_draft(
        self,
        principal: CreatorApiPrincipal,
        *,
        draft_id: str,
        request: CreatorDraftUpdateRequest,
        idempotency_key: str,
    ) -> CreatorDraftView:
        current = await self._query.get_draft(principal, draft_id)
        if request.source_artifact_id:
            await self._query.get_artifact(
                principal,
                current.task_id,
                request.source_artifact_id,
            )
        result = await self._drafts.update_draft(
            tenant_id=principal.tenant_id,
            creator_id=principal.creator_id,
            draft_id=draft_id,
            expected_version=request.expected_version,
            title=request.title,
            content_markdown=request.content_markdown,
            source_artifact_id=request.source_artifact_id,
            editor_type="HUMAN",
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
        )
        if self._studio is not None and not result.replayed:
            await self._studio.record_manual_edit(
                tenant_id=principal.tenant_id,
                creator_id=principal.creator_id,
                task_id=current.task_id,
                draft_id=draft_id,
                from_version=current.current_version,
                to_version=result.draft.current_version,
                changed_chars=_changed_char_count(
                    current.version.content_markdown,
                    result.version.content_markdown,
                ),
            )
        return _draft_result_view(result)

    async def create_publication_handoff(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
        request: CreatorPublicationHandoffRequest,
        idempotency_key: str,
    ) -> CreatorPublicationHandoffView:
        await self._query.get_task_snapshot(principal, task_id)
        task = await self._load_task(task_id)
        if task is None:
            raise CreatorPublicationNotReadyError(
                f"Task {task_id} was not found",
                details={"task_id": task_id},
            )
        artifact_id = request.source_artifact_id or task.final_artifact_id
        if not artifact_id:
            raise CreatorPublicationNotReadyError(
                "COMPLETED task is missing FINAL_CONTENT artifact",
                details={"task_id": task_id},
            )
        await self._query.get_artifact(principal, task_id, artifact_id)
        artifact = await self._artifact_store.get(artifact_id)
        if artifact is None:
            raise CreatorPublicationNotReadyError(
                f"Artifact {artifact_id} was not found",
                details={"artifact_id": artifact_id},
            )
        result = await self._publication.handoff(
            task=task,
            artifact=artifact,
            actor_id=principal.actor_id,
            idempotency_key=idempotency_key,
        )
        handoff = result.handoff
        return CreatorPublicationHandoffView(
            handoff_id=handoff.id,
            task_id=handoff.task_id,
            draft_id=handoff.draft_id,
            content_origin=handoff.content_origin.value,
            source_artifact_id=handoff.source_artifact_id,
            source_artifact_revision=handoff.source_artifact_revision,
            source_content_sha256=handoff.source_content_sha256,
            external_draft_id=handoff.external_draft_id,
            title=handoff.title,
            status=handoff.status.value,
            replayed=result.replayed,
            created_at=handoff.created_at,
        )

    async def list_publication_handoffs(
        self,
        principal: CreatorApiPrincipal,
        *,
        task_id: str,
    ) -> tuple[CreatorPublicationHandoffView, ...]:
        await self._query.get_task_snapshot(principal, task_id)
        handoffs = await self._publication.list_for_task(
            tenant_id=principal.tenant_id,
            creator_id=principal.creator_id,
            task_id=task_id,
        )
        return tuple(
            CreatorPublicationHandoffView(
                handoff_id=item.id,
                task_id=item.task_id,
                draft_id=item.draft_id,
                content_origin=item.content_origin.value,
                source_artifact_id=item.source_artifact_id,
                source_artifact_revision=item.source_artifact_revision,
                source_content_sha256=item.source_content_sha256,
                external_draft_id=item.external_draft_id,
                title=item.title,
                status=item.status.value,
                replayed=False,
                created_at=item.created_at,
            )
            for item in handoffs
        )

    async def _load_task(self, task_id: str):
        async with self._sessions() as session:
            row = await session.get(CreatorTaskRow, task_id)
            return _task_from_row(row) if row is not None else None


def _draft_result_view(result: CreatorDraftWriteResult) -> CreatorDraftView:
    draft = result.draft
    version = result.version
    return CreatorDraftView(
        draft_id=draft.id,
        task_id=draft.task_id,
        title=draft.title,
        current_version=draft.current_version,
        status=draft.status.value,
        version=CreatorDraftVersionView(
            draft_id=version.draft_id,
            version=version.version,
            title=version.title,
            content_markdown=version.content_markdown,
            content_sha256=version.content_sha256,
            source_artifact_id=version.source_artifact_id,
            editor_type=version.editor_type,
            actor_id=version.actor_id,
            created_at=version.created_at,
        ),
        replayed=result.replayed,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _changed_char_count(before: str, after: str) -> int:
    shared = sum(1 for left, right in zip(before, after) if left == right)
    return max(len(before), len(after)) - shared
