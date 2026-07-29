from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.creator.drafts.models import CreatorDraftWriteResult
from app.creator.drafts.service import CreatorDraftService
from app.creator.drafts.sqlalchemy import CreatorDraftRow, CreatorDraftVersionRow
from app.creator.infrastructure.sqlalchemy import (
    CreatorArtifactRow,
    CreatorTaskRow,
)
from app.creator.runtime.ports import CreatorModelGateway, CreatorModelRequest
from app.creator.studio.errors import (
    CreatorStudioConflictError,
    CreatorStudioInvalidSelectionError,
    CreatorStudioModelError,
    CreatorStudioNotFoundError,
    CreatorStudioScopeError,
    CreatorStudioSuggestionStaleError,
)
from app.creator.studio.models import (
    ChannelVariantDocument,
    CreatorBranch,
    CreatorChannelVariant,
    CreatorDeliveryChannel,
    CreatorDeliveryStatus,
    CreatorFeedback,
    CreatorFeedbackKind,
    CreatorFeedbackSummary,
    CreatorMaterial,
    CreatorMaterialKind,
    CreatorMaterialStatus,
    CreatorProject,
    CreatorProjectStatus,
    CreatorSuggestion,
    CreatorSuggestionKind,
    CreatorSuggestionStatus,
    SuggestionProposalDocument,
)
from app.creator.studio.sqlalchemy import (
    CreatorChannelVariantRow,
    CreatorDraftBranchRow,
    CreatorFeedbackRow,
    CreatorMaterialRow,
    CreatorProjectRow,
    CreatorProjectTaskRow,
    CreatorSuggestionRow,
    CreatorTaskMaterialRow,
)


class CreatorStudioService:
    """Tenant-scoped application service for the creator-facing studio."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        drafts: CreatorDraftService,
        draft_loader: Callable[..., Any],
        model: CreatorModelGateway,
        model_provider: str,
        model_name: str,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._drafts = drafts
        self._draft_loader = draft_loader
        self._model = model
        self._model_provider = model_provider
        self._model_name = model_name
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create_project(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        name: str,
        description: str,
    ) -> CreatorProject:
        now = self._clock()
        row = CreatorProjectRow(
            id=self._id_factory(),
            tenant_id=tenant_id,
            creator_id=creator_id,
            name=name.strip(),
            description=description.strip(),
            status=CreatorProjectStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError as exc:
            raise CreatorStudioConflictError(
                "同名项目已经存在",
                details={"name": name.strip()},
            ) from exc
        return _project_from_row(row)

    async def list_projects(
        self,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> tuple[CreatorProject, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorProjectRow)
                    .where(
                        CreatorProjectRow.tenant_id == tenant_id,
                        CreatorProjectRow.creator_id == creator_id,
                    )
                    .order_by(
                        CreatorProjectRow.updated_at.desc(),
                        CreatorProjectRow.id.desc(),
                    )
                )
            ).all()
            task_count_rows = (
                await session.execute(
                    select(
                        CreatorProjectTaskRow.project_id,
                        func.count(CreatorProjectTaskRow.task_id),
                    )
                    .where(
                        CreatorProjectTaskRow.tenant_id == tenant_id,
                        CreatorProjectTaskRow.creator_id == creator_id,
                    )
                    .group_by(CreatorProjectTaskRow.project_id)
                )
            ).all()
            task_counts: dict[str, int] = {
                project_id: int(count) for project_id, count in task_count_rows
            }
            material_count_rows = (
                await session.execute(
                    select(
                        CreatorMaterialRow.project_id,
                        func.count(CreatorMaterialRow.id),
                    )
                    .where(
                        CreatorMaterialRow.tenant_id == tenant_id,
                        CreatorMaterialRow.creator_id == creator_id,
                        CreatorMaterialRow.project_id.is_not(None),
                    )
                    .group_by(CreatorMaterialRow.project_id)
                )
            ).all()
            material_counts: dict[str, int] = {
                project_id: int(count)
                for project_id, count in material_count_rows
                if project_id is not None
            }
        return tuple(
            _project_from_row(
                row,
                task_count=int(task_counts.get(row.id, 0)),
                material_count=int(material_counts.get(row.id, 0)),
            )
            for row in rows
        )

    async def create_material(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        project_id: str | None,
        title: str,
        kind: CreatorMaterialKind,
        content_text: str,
        source_url: str | None,
        tags: tuple[str, ...],
    ) -> CreatorMaterial:
        normalized_content = content_text.strip()
        now = self._clock()
        async with self._sessions() as session:
            async with session.begin():
                if project_id:
                    project = await session.get(CreatorProjectRow, project_id)
                    project = _require_project_scope(
                        project,
                        tenant_id=tenant_id,
                        creator_id=creator_id,
                    )
                    project.updated_at = now
                row = CreatorMaterialRow(
                    id=self._id_factory(),
                    tenant_id=tenant_id,
                    creator_id=creator_id,
                    project_id=project_id,
                    title=title.strip(),
                    kind=kind.value,
                    source_url=source_url.strip() if source_url else None,
                    content_text=normalized_content,
                    content_sha256=_hash_text(normalized_content),
                    status=CreatorMaterialStatus.READY.value,
                    chunk_count=max(1, (len(normalized_content) + 1_199) // 1_200),
                    tags_json=list(dict.fromkeys(tag.strip() for tag in tags if tag.strip())),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
        return _material_from_row(row)

    async def list_materials(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        project_id: str | None = None,
    ) -> tuple[CreatorMaterial, ...]:
        statement = (
            select(CreatorMaterialRow)
            .where(
                CreatorMaterialRow.tenant_id == tenant_id,
                CreatorMaterialRow.creator_id == creator_id,
            )
            .order_by(
                CreatorMaterialRow.updated_at.desc(),
                CreatorMaterialRow.id.desc(),
            )
        )
        if project_id:
            statement = statement.where(CreatorMaterialRow.project_id == project_id)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_material_from_row(row) for row in rows)

    async def build_material_context(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        material_ids: tuple[str, ...],
        max_chars: int = 10_000,
    ) -> str:
        if not material_ids:
            return ""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorMaterialRow).where(
                        CreatorMaterialRow.id.in_(material_ids),
                        CreatorMaterialRow.tenant_id == tenant_id,
                        CreatorMaterialRow.creator_id == creator_id,
                        CreatorMaterialRow.status == CreatorMaterialStatus.READY.value,
                    )
                )
            ).all()
        by_id = {row.id: row for row in rows}
        missing = [material_id for material_id in material_ids if material_id not in by_id]
        if missing:
            raise CreatorStudioNotFoundError(
                "部分素材不存在或不可用",
                details={"material_ids": missing},
            )
        blocks: list[str] = []
        remaining = max(500, max_chars)
        for material_id in material_ids:
            row = by_id[material_id]
            heading = f"[素材 material:{row.id}] {row.title}"
            if row.source_url:
                heading += f"\n来源：{row.source_url}"
            allowance = max(0, remaining - len(heading) - 2)
            if allowance <= 0:
                break
            body = row.content_text[:allowance]
            blocks.append(f"{heading}\n{body}")
            remaining -= len(heading) + len(body) + 2
        return "\n\n".join(blocks)

    async def validate_task_context(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        project_id: str | None,
        material_ids: tuple[str, ...],
    ) -> None:
        async with self._sessions() as session:
            if project_id:
                project = await session.get(CreatorProjectRow, project_id)
                _require_project_scope(
                    project,
                    tenant_id=tenant_id,
                    creator_id=creator_id,
                )
            if material_ids:
                available = set(
                    (
                        await session.scalars(
                            select(CreatorMaterialRow.id).where(
                                CreatorMaterialRow.id.in_(material_ids),
                                CreatorMaterialRow.tenant_id == tenant_id,
                                CreatorMaterialRow.creator_id == creator_id,
                                CreatorMaterialRow.status
                                == CreatorMaterialStatus.READY.value,
                            )
                        )
                    ).all()
                )
                missing = [
                    material_id
                    for material_id in material_ids
                    if material_id not in available
                ]
                if missing:
                    raise CreatorStudioNotFoundError(
                        "部分素材不存在或不可用",
                        details={"material_ids": missing},
                    )

    async def attach_task_context(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        project_id: str | None,
        material_ids: tuple[str, ...],
    ) -> None:
        now = self._clock()
        async with self._sessions() as session:
            async with session.begin():
                task = await session.get(CreatorTaskRow, task_id)
                if task is None:
                    raise CreatorStudioNotFoundError(
                        "创作任务不存在",
                        details={"task_id": task_id},
                    )
                if task.tenant_id != tenant_id or task.creator_id != creator_id:
                    raise CreatorStudioScopeError("创作任务不属于当前用户")
                if project_id:
                    project = await session.get(CreatorProjectRow, project_id)
                    project = _require_project_scope(
                        project,
                        tenant_id=tenant_id,
                        creator_id=creator_id,
                    )
                    key = {"project_id": project_id, "task_id": task_id}
                    if await session.get(CreatorProjectTaskRow, key) is None:
                        session.add(
                            CreatorProjectTaskRow(
                                project_id=project_id,
                                task_id=task_id,
                                tenant_id=tenant_id,
                                creator_id=creator_id,
                                created_at=now,
                            )
                        )
                    project.updated_at = now
                if material_ids:
                    materials = (
                        await session.scalars(
                            select(CreatorMaterialRow).where(
                                CreatorMaterialRow.id.in_(material_ids),
                                CreatorMaterialRow.tenant_id == tenant_id,
                                CreatorMaterialRow.creator_id == creator_id,
                            )
                        )
                    ).all()
                    by_id = {row.id: row for row in materials}
                    missing = [
                        material_id
                        for material_id in material_ids
                        if material_id not in by_id
                    ]
                    if missing:
                        raise CreatorStudioNotFoundError(
                            "部分素材不存在",
                            details={"material_ids": missing},
                        )
                    for material_id in material_ids:
                        key = {"task_id": task_id, "material_id": material_id}
                        if await session.get(CreatorTaskMaterialRow, key) is None:
                            session.add(
                                CreatorTaskMaterialRow(
                                    task_id=task_id,
                                    material_id=material_id,
                                    tenant_id=tenant_id,
                                    creator_id=creator_id,
                                    created_at=now,
                                )
                            )

    async def create_suggestion(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
        expected_version: int,
        kind: CreatorSuggestionKind,
        instruction: str,
        original_text: str,
        prefix_context: str,
        suffix_context: str,
        idempotency_key: str,
    ) -> CreatorSuggestion:
        payload_hash = _canonical_hash(
            {
                "draft_id": draft_id,
                "expected_version": expected_version,
                "kind": kind.value,
                "instruction": instruction,
                "original_text": original_text,
                "prefix_context": prefix_context,
                "suffix_context": suffix_context,
            }
        )
        key_hash = _hash_text(idempotency_key)
        replay = await self._find_suggestion_by_key(
            tenant_id=tenant_id,
            creator_id=creator_id,
            key_hash=key_hash,
            request_hash=payload_hash,
        )
        if replay is not None:
            return replay

        current = await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        if current.draft.current_version != expected_version:
            raise CreatorStudioSuggestionStaleError(
                "正文已产生新版本，请重新选择需要修改的内容",
                details={
                    "expected_version": expected_version,
                    "actual_version": current.draft.current_version,
                },
            )
        content = current.version.content_markdown
        _locate_selection(
            content,
            original_text,
            prefix_context=prefix_context,
            suffix_context=suffix_context,
        )
        evidence_context, allowed_evidence_ids = await self._task_evidence_context(
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=current.draft.task_id,
        )
        request = CreatorModelRequest(
            operation="editor.suggest",
            system_prompt=(
                "你是创作者的协作编辑。只改写用户选中的文本，不扩展修改范围。"
                "保留原意与事实边界；有证据时引用 evidence_id，没有证据时不要编造。"
                "replacement_text 只返回可直接替换选区的正文，不包含解释或代码围栏。"
            ),
            user_prompt=json.dumps(
                {
                    "language": "zh-CN",
                    "kind": kind.value,
                    "instruction": instruction.strip(),
                    "title": current.version.title,
                    "selected_text": original_text,
                    "prefix_context": prefix_context,
                    "suffix_context": suffix_context,
                    "evidence_context": evidence_context,
                    "allowed_evidence_ids": sorted(allowed_evidence_ids),
                },
                ensure_ascii=False,
            ),
            max_output_tokens=2_000,
        )
        try:
            proposal, _, _ = await self._model.complete_structured(
                request,
                SuggestionProposalDocument,
            )
        except Exception as exc:
            raise CreatorStudioModelError(
                "AI 暂时无法生成局部建议，请稍后重试"
            ) from exc
        replacement = proposal.replacement_text.strip()
        if not replacement or replacement == original_text.strip():
            raise CreatorStudioModelError("AI 未生成有效的修改内容")
        evidence_ids = tuple(
            evidence_id
            for evidence_id in proposal.evidence_ids
            if evidence_id in allowed_evidence_ids
        )
        now = self._clock()
        row = CreatorSuggestionRow(
            id=self._id_factory(),
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=current.draft.task_id,
            draft_id=draft_id,
            base_version=expected_version,
            kind=kind.value,
            instruction=instruction.strip(),
            original_text=original_text,
            replacement_text=replacement,
            prefix_context=prefix_context[-500:],
            suffix_context=suffix_context[:500],
            rationale=proposal.rationale.strip(),
            evidence_ids_json=list(evidence_ids),
            risk_note=proposal.risk_note.strip(),
            status=CreatorSuggestionStatus.PENDING.value,
            model_provider=self._model_provider,
            model_name=self._model_name,
            idempotency_key_hash=key_hash,
            request_hash=payload_hash,
            created_at=now,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    session.add(row)
        except IntegrityError:
            replay = await self._find_suggestion_by_key(
                tenant_id=tenant_id,
                creator_id=creator_id,
                key_hash=key_hash,
                request_hash=payload_hash,
            )
            if replay is not None:
                return replay
            raise
        return _suggestion_from_row(row)

    async def list_suggestions(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> tuple[CreatorSuggestion, ...]:
        await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorSuggestionRow)
                    .where(
                        CreatorSuggestionRow.draft_id == draft_id,
                        CreatorSuggestionRow.tenant_id == tenant_id,
                        CreatorSuggestionRow.creator_id == creator_id,
                    )
                    .order_by(
                        CreatorSuggestionRow.created_at.desc(),
                        CreatorSuggestionRow.id.desc(),
                    )
                )
            ).all()
        return tuple(_suggestion_from_row(row) for row in rows)

    async def accept_suggestion(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        actor_id: str,
        suggestion_id: str,
        idempotency_key: str,
    ) -> tuple[CreatorSuggestion, CreatorDraftWriteResult]:
        suggestion = await self._load_suggestion(
            tenant_id=tenant_id,
            creator_id=creator_id,
            suggestion_id=suggestion_id,
        )
        if suggestion.status == CreatorSuggestionStatus.ACCEPTED:
            current = await self._load_draft(
                tenant_id=tenant_id,
                creator_id=creator_id,
                draft_id=suggestion.draft_id,
            )
            return suggestion, current
        if suggestion.status != CreatorSuggestionStatus.PENDING:
            raise CreatorStudioConflictError(
                "该建议已处理，不能重复接受",
                details={"status": suggestion.status.value},
            )
        current = await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=suggestion.draft_id,
        )
        if current.draft.current_version != suggestion.base_version:
            await self._mark_suggestion_stale(suggestion_id)
            raise CreatorStudioSuggestionStaleError(
                "正文已发生变化，这条建议已过期",
                details={
                    "base_version": suggestion.base_version,
                    "current_version": current.draft.current_version,
                },
            )
        try:
            start = _locate_selection(
                current.version.content_markdown,
                suggestion.original_text,
                prefix_context=suggestion.prefix_context,
                suffix_context=suggestion.suffix_context,
            )
        except CreatorStudioInvalidSelectionError as exc:
            await self._mark_suggestion_stale(suggestion_id)
            raise CreatorStudioSuggestionStaleError(
                "无法在当前正文中定位原选区，这条建议已过期"
            ) from exc
        end = start + len(suggestion.original_text)
        updated_content = (
            current.version.content_markdown[:start]
            + suggestion.replacement_text
            + current.version.content_markdown[end:]
        )
        result = await self._drafts.update_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=suggestion.draft_id,
            expected_version=suggestion.base_version,
            title=None,
            content_markdown=updated_content,
            source_artifact_id=current.version.source_artifact_id,
            editor_type="AI_ASSISTED",
            actor_id=actor_id,
            idempotency_key=f"suggestion:{suggestion_id}:{idempotency_key}",
        )
        now = self._clock()
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    CreatorSuggestionRow,
                    suggestion_id,
                    with_for_update=True,
                )
                if row is None:
                    raise CreatorStudioNotFoundError("AI 建议不存在")
                row.status = CreatorSuggestionStatus.ACCEPTED.value
                row.applied_version = result.draft.current_version
                row.resolved_at = now
                session.add(
                    CreatorFeedbackRow(
                        id=self._id_factory(),
                        tenant_id=tenant_id,
                        creator_id=creator_id,
                        task_id=row.task_id,
                        draft_id=row.draft_id,
                        suggestion_id=row.id,
                        kind=CreatorFeedbackKind.SUGGESTION_ACCEPTED.value,
                        score=1.0,
                        reason="",
                        metadata_json={
                            "kind": row.kind,
                            "base_version": row.base_version,
                            "applied_version": result.draft.current_version,
                        },
                        created_at=now,
                    )
                )
        return (
            (await self._load_suggestion(
                tenant_id=tenant_id,
                creator_id=creator_id,
                suggestion_id=suggestion_id,
            )),
            result,
        )

    async def reject_suggestion(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        suggestion_id: str,
        reason: str,
    ) -> CreatorSuggestion:
        now = self._clock()
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    CreatorSuggestionRow,
                    suggestion_id,
                    with_for_update=True,
                )
                row = _require_suggestion_scope(
                    row,
                    tenant_id=tenant_id,
                    creator_id=creator_id,
                )
                if row.status == CreatorSuggestionStatus.REJECTED.value:
                    return _suggestion_from_row(row)
                if row.status != CreatorSuggestionStatus.PENDING.value:
                    raise CreatorStudioConflictError(
                        "该建议已处理，不能再次拒绝",
                        details={"status": row.status},
                    )
                row.status = CreatorSuggestionStatus.REJECTED.value
                row.resolved_at = now
                session.add(
                    CreatorFeedbackRow(
                        id=self._id_factory(),
                        tenant_id=tenant_id,
                        creator_id=creator_id,
                        task_id=row.task_id,
                        draft_id=row.draft_id,
                        suggestion_id=row.id,
                        kind=CreatorFeedbackKind.SUGGESTION_REJECTED.value,
                        score=0.0,
                        reason=reason.strip(),
                        metadata_json={"kind": row.kind},
                        created_at=now,
                    )
                )
        return _suggestion_from_row(row)

    async def record_manual_edit(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        draft_id: str,
        from_version: int,
        to_version: int,
        changed_chars: int,
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    CreatorFeedbackRow(
                        id=self._id_factory(),
                        tenant_id=tenant_id,
                        creator_id=creator_id,
                        task_id=task_id,
                        draft_id=draft_id,
                        kind=CreatorFeedbackKind.MANUAL_EDIT.value,
                        reason="",
                        metadata_json={
                            "from_version": from_version,
                            "to_version": to_version,
                            "changed_chars": changed_chars,
                        },
                        created_at=self._clock(),
                    )
                )

    async def create_branch(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        actor_id: str,
        draft_id: str,
        source_version: int,
        name: str,
        idempotency_key: str,
    ) -> tuple[CreatorBranch, CreatorDraftWriteResult]:
        async with self._sessions() as session:
            draft_row = await session.get(CreatorDraftRow, draft_id)
            draft_row = _require_draft_scope(
                draft_row,
                tenant_id=tenant_id,
                creator_id=creator_id,
            )
            version_row = await session.get(
                CreatorDraftVersionRow,
                {"draft_id": draft_id, "version": source_version},
            )
        if version_row is None:
            raise CreatorStudioNotFoundError(
                "指定的草稿版本不存在",
                details={"draft_id": draft_id, "version": source_version},
            )
        result = await self._drafts.save_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=draft_row.task_id,
            title=f"{version_row.title} · {name.strip()}",
            content_markdown=version_row.content_markdown,
            source_artifact_id=version_row.source_artifact_id,
            editor_type="BRANCH",
            actor_id=actor_id,
            idempotency_key=f"branch:{draft_id}:{source_version}:{idempotency_key}",
        )
        async with self._sessions() as session:
            existing = await session.scalar(
                select(CreatorDraftBranchRow).where(
                    CreatorDraftBranchRow.draft_id == result.draft.id,
                    CreatorDraftBranchRow.tenant_id == tenant_id,
                    CreatorDraftBranchRow.creator_id == creator_id,
                )
            )
        if existing is None:
            now = self._clock()
            row = CreatorDraftBranchRow(
                id=self._id_factory(),
                tenant_id=tenant_id,
                creator_id=creator_id,
                source_draft_id=draft_id,
                source_version=source_version,
                draft_id=result.draft.id,
                name=name.strip(),
                created_at=now,
            )
            feedback = CreatorFeedbackRow(
                id=self._id_factory(),
                tenant_id=tenant_id,
                creator_id=creator_id,
                task_id=draft_row.task_id,
                draft_id=result.draft.id,
                kind=CreatorFeedbackKind.BRANCH_CREATED.value,
                reason="",
                metadata_json={
                    "source_draft_id": draft_id,
                    "source_version": source_version,
                },
                created_at=now,
            )
            async with self._sessions() as session:
                async with session.begin():
                    session.add_all((row, feedback))
        else:
            row = existing
        return _branch_from_row(row), result

    async def list_branches(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> tuple[CreatorBranch, ...]:
        await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorDraftBranchRow)
                    .where(
                        CreatorDraftBranchRow.source_draft_id == draft_id,
                        CreatorDraftBranchRow.tenant_id == tenant_id,
                        CreatorDraftBranchRow.creator_id == creator_id,
                    )
                    .order_by(CreatorDraftBranchRow.created_at.desc())
                )
            ).all()
        return tuple(_branch_from_row(row) for row in rows)

    async def create_channel_variant(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
        expected_version: int,
        channel: CreatorDeliveryChannel,
        instruction: str,
        idempotency_key: str,
    ) -> CreatorChannelVariant:
        request_hash = _canonical_hash(
            {
                "draft_id": draft_id,
                "expected_version": expected_version,
                "channel": channel.value,
                "instruction": instruction,
            }
        )
        key_hash = _hash_text(idempotency_key)
        replay = await self._find_channel_variant_by_key(
            tenant_id=tenant_id,
            creator_id=creator_id,
            key_hash=key_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        current = await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        if current.draft.current_version != expected_version:
            raise CreatorStudioConflictError(
                "正文已产生新版本，请基于最新版本重新生成渠道稿",
                details={
                    "expected_version": expected_version,
                    "actual_version": current.draft.current_version,
                },
            )
        request = CreatorModelRequest(
            operation="editor.channel",
            system_prompt=(
                "你是内容发行编辑。将原稿适配到指定渠道，保持核心观点和事实不变，"
                "按渠道阅读习惯调整标题、篇幅、段落和行动引导。不要新增未经原稿支持的事实。"
            ),
            user_prompt=json.dumps(
                {
                    "language": "zh-CN",
                    "channel": channel.value,
                    "instruction": instruction.strip(),
                    "title": current.version.title,
                    "content_markdown": current.version.content_markdown,
                },
                ensure_ascii=False,
            ),
            max_output_tokens=4_000,
        )
        try:
            document, _, _ = await self._model.complete_structured(
                request,
                ChannelVariantDocument,
            )
        except Exception as exc:
            raise CreatorStudioModelError("AI 暂时无法生成渠道版本") from exc
        now = self._clock()
        row = CreatorChannelVariantRow(
            id=self._id_factory(),
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=current.draft.task_id,
            draft_id=draft_id,
            draft_version=expected_version,
            channel=channel.value,
            title=document.title.strip(),
            content_markdown=document.content_markdown.strip(),
            adaptation_note=document.adaptation_note.strip(),
            status=CreatorDeliveryStatus.READY.value,
            model_provider=self._model_provider,
            model_name=self._model_name,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    session.add(row)
                    session.add(
                        CreatorFeedbackRow(
                            id=self._id_factory(),
                            tenant_id=tenant_id,
                            creator_id=creator_id,
                            task_id=current.draft.task_id,
                            draft_id=draft_id,
                            kind=CreatorFeedbackKind.CHANNEL_VARIANT_CREATED.value,
                            reason="",
                            metadata_json={
                                "channel": channel.value,
                                "draft_version": expected_version,
                            },
                            created_at=now,
                        )
                    )
        except IntegrityError:
            replay = await self._find_channel_variant_by_key(
                tenant_id=tenant_id,
                creator_id=creator_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            raise
        return _channel_variant_from_row(row)

    async def list_channel_variants(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> tuple[CreatorChannelVariant, ...]:
        await self._load_draft(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorChannelVariantRow)
                    .where(
                        CreatorChannelVariantRow.draft_id == draft_id,
                        CreatorChannelVariantRow.tenant_id == tenant_id,
                        CreatorChannelVariantRow.creator_id == creator_id,
                    )
                    .order_by(CreatorChannelVariantRow.created_at.desc())
                )
            ).all()
        return tuple(_channel_variant_from_row(row) for row in rows)

    async def record_rating(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
        draft_id: str | None,
        score: float,
        reason: str,
    ) -> CreatorFeedback:
        await self._require_task_scope(
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=task_id,
        )
        if draft_id:
            await self._load_draft(
                tenant_id=tenant_id,
                creator_id=creator_id,
                draft_id=draft_id,
            )
        row = CreatorFeedbackRow(
            id=self._id_factory(),
            tenant_id=tenant_id,
            creator_id=creator_id,
            task_id=task_id,
            draft_id=draft_id,
            kind=CreatorFeedbackKind.RATING.value,
            score=score,
            reason=reason.strip(),
            metadata_json={},
            created_at=self._clock(),
        )
        async with self._sessions() as session:
            async with session.begin():
                session.add(row)
        return _feedback_from_row(row)

    async def feedback_summary(
        self,
        *,
        tenant_id: str,
        creator_id: str,
    ) -> CreatorFeedbackSummary:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CreatorFeedbackRow).where(
                        CreatorFeedbackRow.tenant_id == tenant_id,
                        CreatorFeedbackRow.creator_id == creator_id,
                    )
                )
            ).all()
        kinds = [row.kind for row in rows]
        accepted = kinds.count(CreatorFeedbackKind.SUGGESTION_ACCEPTED.value)
        rejected = kinds.count(CreatorFeedbackKind.SUGGESTION_REJECTED.value)
        ratings = [
            float(row.score)
            for row in rows
            if row.kind == CreatorFeedbackKind.RATING.value and row.score is not None
        ]
        decided = accepted + rejected
        return CreatorFeedbackSummary(
            accepted_suggestions=accepted,
            rejected_suggestions=rejected,
            manual_edits=kinds.count(CreatorFeedbackKind.MANUAL_EDIT.value),
            acceptance_rate=(accepted / decided if decided else None),
            average_rating=(sum(ratings) / len(ratings) if ratings else None),
            total_events=len(rows),
        )

    async def _load_draft(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        draft_id: str,
    ) -> CreatorDraftWriteResult:
        result = await self._draft_loader(
            tenant_id=tenant_id,
            creator_id=creator_id,
            draft_id=draft_id,
        )
        if result is None:
            raise CreatorStudioNotFoundError(
                "草稿不存在",
                details={"draft_id": draft_id},
            )
        return result

    async def _load_suggestion(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        suggestion_id: str,
    ) -> CreatorSuggestion:
        async with self._sessions() as session:
            row = await session.get(CreatorSuggestionRow, suggestion_id)
        row = _require_suggestion_scope(
            row,
            tenant_id=tenant_id,
            creator_id=creator_id,
        )
        return _suggestion_from_row(row)

    async def _find_suggestion_by_key(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        key_hash: str,
        request_hash: str,
    ) -> CreatorSuggestion | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CreatorSuggestionRow).where(
                    CreatorSuggestionRow.tenant_id == tenant_id,
                    CreatorSuggestionRow.creator_id == creator_id,
                    CreatorSuggestionRow.idempotency_key_hash == key_hash,
                )
            )
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise CreatorStudioConflictError(
                "幂等键已被另一条 AI 建议使用"
            )
        return _suggestion_from_row(row)

    async def _find_channel_variant_by_key(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        key_hash: str,
        request_hash: str,
    ) -> CreatorChannelVariant | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CreatorChannelVariantRow).where(
                    CreatorChannelVariantRow.tenant_id == tenant_id,
                    CreatorChannelVariantRow.creator_id == creator_id,
                    CreatorChannelVariantRow.idempotency_key_hash == key_hash,
                )
            )
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise CreatorStudioConflictError("幂等键已被另一条渠道稿请求使用")
        return _channel_variant_from_row(row)

    async def _mark_suggestion_stale(self, suggestion_id: str) -> None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    CreatorSuggestionRow,
                    suggestion_id,
                    with_for_update=True,
                )
                if row is not None and row.status == CreatorSuggestionStatus.PENDING.value:
                    row.status = CreatorSuggestionStatus.STALE.value
                    row.resolved_at = self._clock()

    async def _task_evidence_context(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> tuple[list[dict[str, str]], set[str]]:
        async with self._sessions() as session:
            artifacts = (
                await session.scalars(
                    select(CreatorArtifactRow).where(
                        CreatorArtifactRow.task_id == task_id,
                        CreatorArtifactRow.tenant_id == tenant_id,
                        CreatorArtifactRow.creator_id == creator_id,
                        CreatorArtifactRow.kind == "EVIDENCE_PACK",
                    )
                )
            ).all()
            materials = (
                await session.scalars(
                    select(CreatorMaterialRow)
                    .join(
                        CreatorTaskMaterialRow,
                        CreatorTaskMaterialRow.material_id == CreatorMaterialRow.id,
                    )
                    .where(
                        CreatorTaskMaterialRow.task_id == task_id,
                        CreatorTaskMaterialRow.tenant_id == tenant_id,
                        CreatorTaskMaterialRow.creator_id == creator_id,
                    )
                )
            ).all()
        evidence: list[dict[str, str]] = []
        allowed: set[str] = set()
        for artifact in artifacts:
            for item in artifact.content_json.get("evidence") or ():
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                evidence_id = str(item["id"])
                allowed.add(evidence_id)
                evidence.append(
                    {
                        "evidence_id": evidence_id,
                        "title": str(item.get("title") or ""),
                        "summary": str(item.get("summary") or "")[:1_000],
                        "source_url": str(item.get("source_url") or ""),
                    }
                )
        for material in materials:
            evidence_id = f"material:{material.id}"
            allowed.add(evidence_id)
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "title": material.title,
                    "summary": material.content_text[:1_000],
                    "source_url": material.source_url or "",
                }
            )
        return evidence[:12], allowed

    async def _require_task_scope(
        self,
        *,
        tenant_id: str,
        creator_id: str,
        task_id: str,
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(CreatorTaskRow, task_id)
        if row is None:
            raise CreatorStudioNotFoundError("创作任务不存在")
        if row.tenant_id != tenant_id or row.creator_id != creator_id:
            raise CreatorStudioScopeError("创作任务不属于当前用户")


def _project_from_row(
    row: CreatorProjectRow,
    *,
    task_count: int = 0,
    material_count: int = 0,
) -> CreatorProject:
    return CreatorProject(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        name=row.name,
        description=row.description,
        status=CreatorProjectStatus(row.status),
        task_count=task_count,
        material_count=material_count,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _material_from_row(row: CreatorMaterialRow) -> CreatorMaterial:
    return CreatorMaterial(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        project_id=row.project_id,
        title=row.title,
        kind=CreatorMaterialKind(row.kind),
        source_url=row.source_url,
        content_text=row.content_text,
        content_sha256=row.content_sha256,
        status=CreatorMaterialStatus(row.status),
        chunk_count=row.chunk_count,
        tags=tuple(row.tags_json or ()),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _suggestion_from_row(row: CreatorSuggestionRow) -> CreatorSuggestion:
    return CreatorSuggestion(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        draft_id=row.draft_id,
        base_version=row.base_version,
        kind=CreatorSuggestionKind(row.kind),
        instruction=row.instruction,
        original_text=row.original_text,
        replacement_text=row.replacement_text,
        prefix_context=row.prefix_context,
        suffix_context=row.suffix_context,
        rationale=row.rationale,
        evidence_ids=tuple(row.evidence_ids_json or ()),
        risk_note=row.risk_note,
        status=CreatorSuggestionStatus(row.status),
        model_provider=row.model_provider,
        model_name=row.model_name,
        applied_version=row.applied_version,
        created_at=_as_utc(row.created_at),
        resolved_at=_as_utc(row.resolved_at) if row.resolved_at else None,
    )


def _branch_from_row(row: CreatorDraftBranchRow) -> CreatorBranch:
    return CreatorBranch(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        source_draft_id=row.source_draft_id,
        source_version=row.source_version,
        draft_id=row.draft_id,
        name=row.name,
        created_at=_as_utc(row.created_at),
    )


def _channel_variant_from_row(
    row: CreatorChannelVariantRow,
) -> CreatorChannelVariant:
    return CreatorChannelVariant(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        draft_id=row.draft_id,
        draft_version=row.draft_version,
        channel=CreatorDeliveryChannel(row.channel),
        title=row.title,
        content_markdown=row.content_markdown,
        adaptation_note=row.adaptation_note,
        status=CreatorDeliveryStatus(row.status),
        model_provider=row.model_provider,
        model_name=row.model_name,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _feedback_from_row(row: CreatorFeedbackRow) -> CreatorFeedback:
    return CreatorFeedback(
        id=row.id,
        tenant_id=row.tenant_id,
        creator_id=row.creator_id,
        task_id=row.task_id,
        draft_id=row.draft_id,
        suggestion_id=row.suggestion_id,
        kind=CreatorFeedbackKind(row.kind),
        score=row.score,
        reason=row.reason,
        metadata=dict(row.metadata_json or {}),
        created_at=_as_utc(row.created_at),
    )


def _locate_selection(
    content: str,
    original_text: str,
    *,
    prefix_context: str,
    suffix_context: str,
) -> int:
    if not original_text:
        raise CreatorStudioInvalidSelectionError("请先选择需要修改的正文")
    positions: list[int] = []
    start = 0
    while True:
        index = content.find(original_text, start)
        if index < 0:
            break
        positions.append(index)
        start = index + max(1, len(original_text))
    if not positions:
        raise CreatorStudioInvalidSelectionError("选中的内容不在当前正文中")
    if len(positions) == 1:
        return positions[0]
    normalized_prefix = prefix_context[-500:]
    normalized_suffix = suffix_context[:500]
    matches = [
        index
        for index in positions
        if (not normalized_prefix or content[:index].endswith(normalized_prefix))
        and (
            not normalized_suffix
            or content[index + len(original_text) :].startswith(normalized_suffix)
        )
    ]
    if len(matches) == 1:
        return matches[0]
    raise CreatorStudioInvalidSelectionError(
        "正文中存在多个相同选区，请扩大选择范围后重试",
        details={"occurrences": len(positions)},
    )


def _require_project_scope(
    row: CreatorProjectRow | None,
    *,
    tenant_id: str,
    creator_id: str,
) -> CreatorProjectRow:
    if row is None:
        raise CreatorStudioNotFoundError("项目不存在")
    if row.tenant_id != tenant_id or row.creator_id != creator_id:
        raise CreatorStudioScopeError("项目不属于当前用户")
    return row


def _require_draft_scope(
    row: CreatorDraftRow | None,
    *,
    tenant_id: str,
    creator_id: str,
) -> CreatorDraftRow:
    if row is None:
        raise CreatorStudioNotFoundError("草稿不存在")
    if row.tenant_id != tenant_id or row.creator_id != creator_id:
        raise CreatorStudioScopeError("草稿不属于当前用户")
    return row


def _require_suggestion_scope(
    row: CreatorSuggestionRow | None,
    *,
    tenant_id: str,
    creator_id: str,
) -> CreatorSuggestionRow:
    if row is None:
        raise CreatorStudioNotFoundError("AI 建议不存在")
    if row.tenant_id != tenant_id or row.creator_id != creator_id:
        raise CreatorStudioScopeError("AI 建议不属于当前用户")
    return row


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
