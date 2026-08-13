from __future__ import annotations

import json
import logging
from typing import Any

from app.creator.agents.schemas import (
    ContentAnalysisDocument,
    ContentOutlineDocument,
    CreatorProfileDocument,
    CritiqueDocument,
    CritiqueVerdict,
    DataAvailability,
    DraftDocument,
    EvaluationDocument,
    EvaluationMetricDocument,
    EvidenceItem,
    EvidencePackDocument,
    TopicOptionsDocument,
    TopicRecommendation,
    UsedAngleDocument,
)
from app.creator.evaluation.ports import CreatorRuntimeEvaluator
from app.creator.evaluation.runtime import CreatorRuntimeContextEvaluator
from app.creator.memory.angles import (
    angles_conflict,
    extract_used_angles,
    normalize_angle_key,
    used_angles_as_dicts,
)
from app.creator.memory.models import (
    CreatorMemoryBundle,
    CreatorMemoryQuery,
    MemorySourceStatus,
)
from app.creator.memory.ports import CreatorMemoryReader
from app.creator.privacy import CreatorPrivacySanitizer
from app.creator.providers.models import (
    CommunityAccessScope,
    CommunityCommentSort,
    CommunitySearchRequest,
    CommunitySearchResult,
)
from app.creator.providers.ports import CreatorCommunityProvider
from app.creator.retrieval.models import (
    CreatorRetrievalRequest,
    CreatorRetrievalResult,
    RetrievalAvailability,
)
from app.creator.retrieval.ports import CreatorRetrievalReader
from app.creator.retrieval.scoring import query_sha256
from app.creator.runtime.models import (
    AgentCapability,
    AgentDescriptor,
    AgentExecutionContext,
    AgentResult,
    AgentUsage,
    ArtifactKind,
    ArtifactPayload,
    CreatorArtifact,
    FactDraft,
)
from app.creator.runtime.ports import (
    CreatorModelGateway,
    CreatorModelRequest,
    OutputModelT,
)

logger = logging.getLogger(__name__)


class SpecialistAgentError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _ModelSpecialist:
    descriptor: AgentDescriptor

    def __init__(self, model: CreatorModelGateway):
        self._model = model

    async def _complete(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: dict[str, Any],
        output_type: type[OutputModelT],
    ) -> tuple[OutputModelT, AgentUsage]:
        system_prompt = _with_language_instruction(system_prompt, payload)
        request = CreatorModelRequest(
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, default=str),
        )
        try:
            document, input_tokens, output_tokens = (
                await self._model.complete_structured(request, output_type)
            )
        except Exception as exc:
            raise SpecialistAgentError(
                "MODEL_CALL_FAILED",
                f"{self.descriptor.name} model call failed",
                retryable=True,
            ) from exc
        return document, AgentUsage(
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class MemoryAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="MemoryAgent",
        capabilities=frozenset({AgentCapability.LOAD_CREATOR_MEMORY}),
        description="Builds a creator profile from authorized memory sources.",
    )

    def __init__(
        self,
        model: CreatorModelGateway,
        memory: CreatorMemoryReader | None = None,
        community: CreatorCommunityProvider | None = None,
    ) -> None:
        super().__init__(model)
        self._memory = memory
        self._community = community

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        memory_context = await _load_memory(
            self._memory,
            context,
            include_task_state=True,
            include_profile=True,
            include_semantic=True,
        )
        community_profile = None
        community_profile_error = False
        if self._community is not None and _creator_profile_enabled(
            context.goal.source_scope
        ):
            try:
                community_profile = await self._community.get_creator_profile(
                    _community_scope(context)
                )
            except Exception as exc:
                community_profile_error = True
                logger.warning(
                    "Creator community profile unavailable task_id=%s "
                    "backend=%s error=%s",
                    context.identity.task_id,
                    self._community.backend_name,
                    type(exc).__name__,
                )
        document, usage = await self._complete(
            operation="memory.profile",
            system_prompt=(
                "Build a creator profile only from supplied data. Mark unavailable "
                "sources explicitly and never invent historical preferences."
            ),
            payload={
                "creator_id": context.identity.creator_id,
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "source_scope": context.goal.source_scope,
                "memory_context": (
                    memory_context.model_dump(mode="json")
                    if memory_context is not None
                    else None
                ),
                "community_profile": (
                    community_profile.model_dump(mode="json")
                    if community_profile is not None
                    else None
                ),
            },
            output_type=CreatorProfileDocument,
        )
        availability = (
            DataAvailability.AVAILABLE
            if community_profile is not None
            else _profile_availability(memory_context)
        )
        chinese = _uses_chinese(context.goal.constraints)
        limitations = list(_memory_limitations(memory_context, chinese=chinese))
        if community_profile_error:
            limitations.append(
                    "已配置的社区创作者画像当前不可用。"
                    if chinese
                    else "The configured community creator profile was unavailable."
            )
        updates: dict[str, Any] = {
            "creator_id": context.identity.creator_id,
            "data_availability": availability,
            "limitations": _merge_strings(
                document.limitations,
                tuple(limitations),
            ),
        }
        if community_profile is not None:
            updates.update(
                display_name=community_profile.display_name,
                bio=community_profile.bio,
                expertise_tags=community_profile.expertise_tags,
            )
        if availability == DataAvailability.NOT_CONNECTED:
            updates.update(
                style_traits=(),
                audience_hypotheses=(),
                preferred_formats=(),
            )
        used_angles = tuple(
            UsedAngleDocument(
                angle_key=item.angle_key,
                title=item.title,
                angle=item.angle,
                task_id=item.task_id,
                artifact_id=item.artifact_id,
                used_at=item.used_at,
            )
            for item in extract_used_angles(
                memory_context.profile if memory_context is not None else None
            )
        )
        updates["used_angles"] = used_angles
        document = document.model_copy(update=updates)
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.CREATOR_PROFILE,
                    content=document.model_dump(mode="json"),
                    metadata={
                        "data_availability": document.data_availability.value,
                        "used_angle_count": len(used_angles),
                        "memory_sources": _memory_source_metadata(memory_context),
                        "community_profile_backend": (
                            self._community.backend_name
                            if community_profile is not None
                            and self._community is not None
                            else None
                        ),
                    },
                    confidence=(
                        0.35
                        if document.data_availability.value == "NOT_CONNECTED"
                        else 0.8
                    ),
                ),
            ),
            facts=(
                FactDraft(
                    key="memory.data_availability",
                    value=document.data_availability.value,
                ),
            ),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese="已生成创作者画像，并保留数据来源限制说明。",
                english="Creator profile artifact produced with source limitations.",
            ),
        )


class ContentAnalyzerAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="ContentAnalyzerAgent",
        capabilities=frozenset({AgentCapability.ANALYZE_CONTENT}),
        description="Analyzes creator history and engagement patterns.",
    )

    def __init__(
        self,
        model: CreatorModelGateway,
        memory: CreatorMemoryReader | None = None,
    ) -> None:
        super().__init__(model)
        self._memory = memory

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        memory_context = await _load_memory(
            self._memory,
            context,
            include_task_state=False,
            include_profile=False,
            include_semantic=True,
        )
        document, usage = await self._complete(
            operation="content.analyze",
            system_prompt=(
                "Analyze only supplied historical content and metrics. Separate "
                "observations from hypotheses and expose missing data."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "source_scope": context.goal.source_scope,
                "memory_context": (
                    memory_context.model_dump(mode="json")
                    if memory_context is not None
                    else None
                ),
            },
            output_type=ContentAnalysisDocument,
        )
        availability = _history_availability(memory_context)
        chinese = _uses_chinese(context.goal.constraints)
        limitations = _memory_limitations(memory_context, chinese=chinese)
        updates = {
            "data_availability": availability,
            "limitations": _merge_strings(document.limitations, limitations),
        }
        if availability == DataAvailability.NOT_CONNECTED:
            updates.update(
                strengths=(),
                reusable_patterns=(),
                improvement_areas=(
                    (
                        "当前没有可用的已授权历史内容。"
                        if chinese
                        else "Authorized historical content is unavailable."
                    ),
                ),
            )
        document = document.model_copy(update=updates)
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.CONTENT_ANALYSIS,
                    content=document.model_dump(mode="json"),
                    metadata={
                        "data_availability": document.data_availability.value,
                        "memory_sources": _memory_source_metadata(memory_context),
                    },
                    confidence=(
                        0.35
                        if document.data_availability.value == "NOT_CONNECTED"
                        else 0.8
                    ),
                ),
            ),
            facts=(
                FactDraft(
                    key="analysis.data_availability",
                    value=document.data_availability.value,
                ),
            ),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese="已生成历史内容分析产物。",
                english="Historical content analysis artifact produced.",
            ),
        )


class ResearchAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="ResearchAgent",
        capabilities=frozenset({AgentCapability.RESEARCH_TOPIC}),
        description="Builds an evidence pack and records unresolved search gaps.",
    )

    def __init__(
        self,
        model: CreatorModelGateway,
        retrieval: CreatorRetrievalReader | None = None,
        community: CreatorCommunityProvider | None = None,
    ) -> None:
        super().__init__(model)
        self._retrieval = retrieval
        self._community = community

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        if self._retrieval is not None or self._community is not None:
            return await self._retrieve(context)
        document, usage = await self._complete(
            operation="research.collect",
            system_prompt=(
                "Build an evidence pack from supplied sources. Do not fabricate "
                "URLs, citations, studies, metrics, or current facts."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "source_scope": context.goal.source_scope,
                "retrieval_backend": "not_connected_in_phase_3",
            },
            output_type=EvidencePackDocument,
        )
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.EVIDENCE_PACK,
                    content=document.model_dump(mode="json"),
                    metadata={
                        "data_availability": document.data_availability.value,
                        "evidence_count": len(document.evidence),
                        "search_gap_count": len(document.search_gaps),
                    },
                    confidence=(
                        0.25
                        if document.data_availability.value == "NOT_CONNECTED"
                        else 0.8
                    ),
                ),
            ),
            facts=(
                FactDraft(
                    key="research.evidence_count",
                    value=len(document.evidence),
                ),
                FactDraft(
                    key="research.search_gap_count",
                    value=len(document.search_gaps),
                ),
            ),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese="已生成证据包，并明确记录检索缺口。",
                english="Evidence pack produced with explicit retrieval gaps.",
            ),
        )

    async def _retrieve(
        self,
        context: AgentExecutionContext,
    ) -> AgentResult:
        result: CreatorRetrievalResult | None = None
        community_result: CommunitySearchResult | None = None
        failures: list[Exception] = []
        if self._retrieval is not None:
            try:
                result = await self._retrieval.retrieve(
                    CreatorRetrievalRequest(
                        tenant_id=context.identity.tenant_id,
                        creator_id=context.identity.creator_id,
                        task_id=context.identity.task_id,
                        run_id=context.identity.run_id,
                        task_kind=context.identity.task_kind,
                        goal=context.goal.text,
                        constraints=context.goal.constraints,
                        source_scope=context.goal.source_scope,
                    )
                )
            except Exception as exc:
                failures.append(exc)
                logger.warning(
                    "Creator local retrieval failed task_id=%s error=%s",
                    context.identity.task_id,
                    type(exc).__name__,
                )
        if self._community is not None and _community_search_enabled(
            context.goal.source_scope
        ):
            try:
                community_result = await self._community.search_posts(
                    _community_scope(context),
                    _community_search_request(context),
                )
            except Exception as exc:
                failures.append(exc)
                logger.warning(
                    "Creator community retrieval failed task_id=%s backend=%s "
                    "error=%s",
                    context.identity.task_id,
                    self._community.backend_name,
                    type(exc).__name__,
                )
        if result is None and community_result is None and failures:
            raise SpecialistAgentError(
                "RETRIEVAL_FAILED",
                "ResearchAgent retrieval failed",
                retryable=True,
            ) from failures[0]

        evidence_items = [
            EvidenceItem(
                id=item.evidence_id,
                title=item.title,
                summary=item.excerpt,
                source=(item.source_url or f"{item.source_system}:{item.document_id}"),
                source_type="community_post",
                requires_verification=not item.authority_verified,
                document_id=item.document_id,
                source_url=item.source_url,
                retrieval_channels=tuple(channel.value for channel in item.channels),
                score=item.score.final,
                score_breakdown=item.score.model_dump(mode="json"),
                published_at=item.published_at,
                authority_verified=item.authority_verified,
            )
            for item in (result.evidence if result is not None else ())
        ]
        if community_result is not None:
            evidence_items.extend(
                _community_evidence(
                    community_result,
                    tenant_id=context.identity.tenant_id,
                )
            )
            if self._community is not None:
                evidence_items.extend(
                    await _community_comment_evidence(
                        self._community,
                        context,
                        community_result,
                    )
                )
        evidence = _deduplicate_evidence(evidence_items)

        grade = result.rounds[-1].grade if result and result.rounds else None
        gaps = [
            *(grade.missing_topics if grade is not None else ()),
            *(result.limitations if result is not None else ()),
            *((grade.reason,) if grade is not None and not grade.sufficient else ()),
            *(
                f"Community search degraded: {service}"
                for service in (
                    community_result.degraded_services
                    if community_result is not None
                    else ()
                )
            ),
        ]
        if failures:
            gaps.append(
                "One configured retrieval source was unavailable during this run."
            )
        search_gaps = tuple(dict.fromkeys(gaps))
        availability = _combined_retrieval_availability(
            result,
            community_result,
            failures=bool(failures),
            has_evidence=bool(evidence),
        )
        document = EvidencePackDocument(
            research_question=context.goal.text,
            evidence=evidence,
            search_gaps=search_gaps,
            data_availability=availability,
        )
        confidence = (
            max((item.score for item in evidence), default=0.25)
            if availability != DataAvailability.NOT_CONNECTED
            else 0.25
        )
        metadata: dict[str, Any] = {
            "data_availability": document.data_availability.value,
            "evidence_count": len(document.evidence),
            "search_gap_count": len(document.search_gaps),
        }
        if result is not None:
            metadata["retrieval_audit"] = _retrieval_audit_metadata(result)
        if community_result is not None and self._community is not None:
            metadata["community_audit"] = {
                "backend": self._community.backend_name,
                "candidate_count": len(community_result.candidates),
                "degraded_services": list(community_result.degraded_services),
            }
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.EVIDENCE_PACK,
                    content=document.model_dump(mode="json"),
                    metadata=metadata,
                    confidence=max(0.0, min(1.0, confidence)),
                ),
            ),
            facts=(
                FactDraft(
                    key="research.evidence_count",
                    value=len(document.evidence),
                ),
                FactDraft(
                    key="research.search_gap_count",
                    value=len(document.search_gaps),
                ),
                FactDraft(
                    key="research.retrieval_rounds",
                    value=len(result.rounds) if result is not None else 0,
                ),
            ),
            usage=AgentUsage(
                tool_calls=(
                    (result.tool_calls if result is not None else 0)
                    + (1 if community_result is not None else 0)
                )
            ),
            summary=_localized_summary(
                context,
                chinese="已根据授权检索结果生成证据包。",
                english="Evidence pack produced from authorized retrieval results.",
            ),
        )


class StrategyAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="StrategyAgent",
        capabilities=frozenset(
            {
                AgentCapability.PLAN_TOPICS,
                AgentCapability.BUILD_OUTLINE,
            }
        ),
        description="Creates topic choices and develops the selected outline.",
    )

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        if context.step.capability == AgentCapability.PLAN_TOPICS:
            return await self._plan_topics(context)
        if context.step.capability == AgentCapability.BUILD_OUTLINE:
            return await self._build_outline(context)
        raise SpecialistAgentError(
            "UNSUPPORTED_CAPABILITY",
            f"StrategyAgent cannot execute {context.step.capability.value}",
            retryable=False,
        )

    async def _plan_topics(self, context: AgentExecutionContext) -> AgentResult:
        inputs = _artifact_payloads(context.artifacts)
        used_angles = _used_angles_from_artifacts(context.artifacts)
        document, usage = await self._complete(
            operation="strategy.topics",
            system_prompt=(
                "Create three to five editorial topic options for a knowledge "
                "creator. Each option must include recommendation "
                "(WRITE_NOW, WRITE_LATER, or SKIP), why_now, reader_question, "
                "and differentiation. Ground WRITE_NOW options in supplied "
                "evidence or comment IDs. Include at least two distinct "
                "recommendation labels. Prefer answering unanswered reader "
                "questions over inventing witty titles. Never recommend SKIP "
                "as recommended_option_id. Do not propose WRITE_NOW angles that "
                "repeat used_content_angles from the creator profile."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "inputs": inputs,
                "used_content_angles": used_angles_as_dicts(used_angles),
            },
            output_type=TopicOptionsDocument,
        )
        _validate_topic_grounding(document, inputs)
        _validate_topic_angle_novelty(document, used_angles)
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.TOPIC_OPTIONS,
                    content=document.model_dump(mode="json"),
                    parent_ids=tuple(artifact.id for artifact in context.artifacts),
                    metadata={
                        "option_count": len(document.options),
                        "option_ids": [option.id for option in document.options],
                        "recommended_option_id": document.recommended_option_id,
                    },
                    confidence=0.8,
                ),
            ),
            facts=(
                FactDraft(
                    key="strategy.topic_option_count",
                    value=len(document.options),
                ),
                FactDraft(
                    key="strategy.recommended_option_id",
                    value=document.recommended_option_id,
                ),
            ),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese=f"已生成 {len(document.options)} 个选题方向。",
                english=f"Produced {len(document.options)} topic options.",
            ),
        )

    async def _build_outline(self, context: AgentExecutionContext) -> AgentResult:
        topics_artifact = _latest_required(
            context.artifacts,
            ArtifactKind.TOPIC_OPTIONS,
        )
        topics = TopicOptionsDocument.model_validate(topics_artifact.content)
        selected_id = str(
            context.goal.constraints.get("selected_topic_id")
            or topics.recommended_option_id
        )
        selected = next(
            (option for option in topics.options if option.id == selected_id),
            None,
        )
        if selected is None:
            raise SpecialistAgentError(
                "INVALID_TOPIC_SELECTION",
                f"Selected topic {selected_id!r} does not exist",
                retryable=False,
            )
        document, usage = await self._complete(
            operation="strategy.outline",
            system_prompt=(
                "Turn the selected topic into a coherent knowledge-post outline. "
                "Keep unsupported evidence out and preserve traceable evidence IDs."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "selected_topic_id": selected.id,
                "selected_title": selected.title,
                "selected_angle": selected.angle,
                "inputs": _artifact_payloads(context.artifacts),
            },
            output_type=ContentOutlineDocument,
        )
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.CONTENT_OUTLINE,
                    content=document.model_dump(mode="json"),
                    parent_ids=tuple(artifact.id for artifact in context.artifacts),
                    metadata={"selected_topic_id": selected.id},
                    confidence=0.82,
                ),
            ),
            facts=(FactDraft(key="strategy.selected_topic_id", value=selected.id),),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese="已根据选定主题生成内容大纲。",
                english="Content outline produced for the selected topic.",
            ),
        )


class WriterAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="WriterAgent",
        capabilities=frozenset(
            {
                AgentCapability.WRITE_DRAFT,
                AgentCapability.REVISE_DRAFT,
            }
        ),
        description="Writes and revises evidence-aware content drafts.",
    )

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        if context.step.capability == AgentCapability.WRITE_DRAFT:
            return await self._write(context)
        if context.step.capability == AgentCapability.REVISE_DRAFT:
            return await self._revise(context)
        raise SpecialistAgentError(
            "UNSUPPORTED_CAPABILITY",
            f"WriterAgent cannot execute {context.step.capability.value}",
            retryable=False,
        )

    async def _write(self, context: AgentExecutionContext) -> AgentResult:
        outline_artifact = _latest_required(
            context.artifacts,
            ArtifactKind.CONTENT_OUTLINE,
        )
        outline = ContentOutlineDocument.model_validate(outline_artifact.content)
        evidence_ids = _evidence_ids(context.artifacts)
        evidence_context = _evidence_context(context.artifacts)
        document, usage = await self._complete(
            operation="writer.draft",
            system_prompt=(
                "Write the complete draft from the approved outline. Attribute only "
                "supplied evidence IDs and list every unsupported factual claim. "
                "For each supported factual claim, add a citation whose claim_text "
                "is an exact substring of body_markdown and whose evidence_id is "
                "present in evidence_context."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "outline": outline.model_dump(mode="json"),
                "evidence_ids": evidence_ids,
                "evidence_context": evidence_context,
            },
            output_type=DraftDocument,
        )
        document = _ground_draft_citations(document, evidence_context)
        return _draft_result(
            document,
            usage,
            parents=tuple(artifact.id for artifact in context.artifacts),
            summary=_localized_summary(
                context,
                chinese="已生成初版正文草稿。",
                english="Initial content draft produced.",
            ),
        )

    async def _revise(self, context: AgentExecutionContext) -> AgentResult:
        draft_artifact = _latest_draft(context.artifacts)
        draft = DraftDocument.model_validate(draft_artifact.content)
        critique_artifact = _latest_optional(
            context.artifacts,
            ArtifactKind.CRITIQUE,
        )
        instructions = _draft_revision_instructions(
            draft_artifact_id=draft_artifact.id,
            critique_artifact=critique_artifact,
            constraints=context.goal.constraints,
        )
        if not instructions:
            raise SpecialistAgentError(
                "DRAFT_REVISION_INPUT_MISSING",
                "Revision requires critic instructions or human draft annotations",
                retryable=False,
            )
        outline_artifact = _latest_optional(
            context.artifacts,
            ArtifactKind.CONTENT_OUTLINE,
        )
        evidence_context = _evidence_context(context.artifacts)
        document, usage = await self._complete(
            operation="writer.revise",
            system_prompt=(
                "Revise the exact reviewed draft. Apply concrete critic and human "
                "section notes without introducing unverified claims or changing "
                "the core goal. Preserve valid citations and add citations only "
                "when claim_text appears exactly in the revised body and the "
                "evidence ID is supplied."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "title": draft.title,
                "draft": draft.model_dump(mode="json"),
                "outline": (
                    outline_artifact.content if outline_artifact is not None else {}
                ),
                "revision_instructions": instructions,
                "section_annotations": list(
                    context.goal.constraints.get("draft_annotations") or ()
                ),
                "evidence_ids": _evidence_ids(context.artifacts),
                "evidence_context": evidence_context,
            },
            output_type=DraftDocument,
        )
        revision_scope = str(
            context.goal.constraints.get("revision_scope", "FULL_REVISION")
        ).upper()
        if revision_scope == "TITLE_ONLY":
            document = document.model_copy(
                update={
                    "title": str(
                        context.goal.constraints.get("requested_title")
                        or document.title
                        or draft.title
                    ).strip(),
                    "body_markdown": draft.body_markdown,
                    "evidence_ids": draft.evidence_ids,
                    "unsupported_claims": draft.unsupported_claims,
                    "citations": draft.citations,
                    "revision_note": document.revision_note,
                }
            )
        elif revision_scope in {"CONTENT_ONLY", "STYLE_ONLY", "STRUCTURE_ONLY"}:
            document = document.model_copy(update={"title": draft.title})
        document = _ground_draft_citations(document, evidence_context)
        parents = [draft_artifact.id]
        if critique_artifact is not None:
            parents.append(critique_artifact.id)
        return _draft_result(
            document,
            usage,
            parents=tuple(parents),
            summary=_localized_summary(
                context,
                chinese="已根据质量评审和人工分段批注修订草稿。",
                english="Draft revised from critic and/or human section notes.",
            ),
            metadata={
                "revision_scope": revision_scope,
                "source_artifact_id": draft_artifact.id,
                "title_only_body_preserved": revision_scope == "TITLE_ONLY",
            },
        )


class CriticAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="CriticAgent",
        capabilities=frozenset({AgentCapability.CRITIQUE_CONTENT}),
        description="Reviews the actual draft against explicit quality dimensions.",
    )

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        draft_artifact = _latest_draft(context.artifacts)
        draft = DraftDocument.model_validate(draft_artifact.content)
        document, usage = await self._complete(
            operation="critic.review",
            system_prompt=(
                "Review the exact supplied draft for relevance, structure, evidence, "
                "and style. Return ACCEPT only when the overall score is at least "
                "0.70 and no blocking issue remains."
            ),
            payload={
                "goal": context.goal.text,
                "constraints": context.goal.constraints,
                "reviewed_artifact_id": draft_artifact.id,
                "draft": draft.model_dump(mode="json"),
                "available_evidence_ids": _evidence_ids(context.artifacts),
            },
            output_type=CritiqueDocument,
        )
        if document.reviewed_artifact_id != draft_artifact.id:
            raise SpecialistAgentError(
                "CRITIC_OUTPUT_MISMATCH",
                "Critic response references a different draft",
                retryable=True,
            )
        accepted = (
            document.verdict == CritiqueVerdict.ACCEPT
            and document.scores.overall >= 0.7
        )
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.CRITIQUE,
                    content=document.model_dump(mode="json"),
                    parent_ids=(draft_artifact.id,),
                    metadata={
                        "accepted": accepted,
                        "overall_score": document.scores.overall,
                        "reviewed_artifact_id": draft_artifact.id,
                    },
                    confidence=0.85,
                ),
            ),
            facts=(
                FactDraft(
                    key="critic.accepted",
                    value=accepted,
                ),
                FactDraft(
                    key="critic.overall_score",
                    value=document.scores.overall,
                ),
                FactDraft(
                    key="critic.reviewed_artifact_id",
                    value=draft_artifact.id,
                ),
            ),
            usage=usage,
            summary=_localized_summary(
                context,
                chinese=(
                    "质量评审已通过草稿。" if accepted else "质量评审要求修订草稿。"
                ),
                english=(
                    "Critic accepted the draft."
                    if accepted
                    else "Critic requested a draft revision."
                ),
            ),
        )


class EvaluationAgent(_ModelSpecialist):
    descriptor = AgentDescriptor(
        name="EvaluationAgent",
        capabilities=frozenset({AgentCapability.EVALUATE_RUN}),
        description="Computes versioned runtime quality signals without model self-grading.",
    )

    def __init__(
        self,
        model: CreatorModelGateway,
        evaluator: CreatorRuntimeEvaluator | None = None,
        memory: CreatorMemoryReader | None = None,
    ) -> None:
        super().__init__(model)
        self._evaluator = evaluator or CreatorRuntimeContextEvaluator()
        self._memory = memory

    async def execute(self, context: AgentExecutionContext) -> AgentResult:
        critique_artifact = _latest_required(
            context.artifacts,
            ArtifactKind.CRITIQUE,
        )
        result = await self._evaluator.evaluate(context)
        chinese = _uses_chinese(context.goal.constraints)
        document = EvaluationDocument(
            task_success=result.task_success,
            planning_observations=(
                ("规划质量将结合已持久化的计划与执行事件，" "在离线或抽样评估中计算。",)
                if chinese
                else result.planning_observations
            ),
            generation_observations=(
                tuple(
                    _evaluation_reason_zh(metric.metric.value)
                    for metric in result.metrics
                    if metric.metric.value.startswith("generation_")
                )
                if chinese
                else result.generation_observations
            ),
            quality_score=result.quality_score,
            metric_status=result.metric_status,
            dataset_id=result.dataset_id,
            dataset_version=result.dataset_version,
            evaluator_version=result.evaluator_version,
            metrics=tuple(
                EvaluationMetricDocument(
                    metric=metric.metric.value,
                    status=metric.status.value,
                    score=metric.score,
                    threshold=metric.threshold,
                    passed=metric.passed,
                    evaluator=metric.evaluator,
                    evaluator_version=metric.evaluator_version,
                    reason=(
                        _evaluation_reason_zh(metric.metric.value)
                        if chinese
                        else metric.reason
                    ),
                )
                for metric in result.metrics
            ),
            unevaluated_metrics=tuple(
                metric.value for metric in result.unevaluated_metrics
            ),
        )
        remembered = await _remember_selected_topic_angle(self._memory, context)
        metadata = {
            "quality_score": document.quality_score,
            "metric_status": document.metric_status,
            "dataset_version": document.dataset_version,
            "evaluator_version": document.evaluator_version,
        }
        if remembered is not None:
            metadata["remembered_angle_key"] = remembered.angle_key
        return AgentResult(
            artifacts=(
                ArtifactPayload(
                    kind=ArtifactKind.EVALUATION_REPORT,
                    content=document.model_dump(mode="json"),
                    parent_ids=(critique_artifact.id,),
                    metadata=metadata,
                    confidence=0.9,
                ),
            ),
            facts=(
                FactDraft(
                    key="evaluation.quality_score",
                    value=document.quality_score,
                ),
            ),
            usage=AgentUsage(),
            summary=_localized_summary(
                context,
                chinese="已生成版本化运行评估报告。",
                english="Versioned runtime evaluation produced.",
            ),
        )


def build_default_specialists(
    model: CreatorModelGateway,
    memory: CreatorMemoryReader | None = None,
    retrieval: CreatorRetrievalReader | None = None,
    evaluation: CreatorRuntimeEvaluator | None = None,
    community: CreatorCommunityProvider | None = None,
) -> tuple[
    MemoryAgent,
    ContentAnalyzerAgent,
    ResearchAgent,
    StrategyAgent,
    WriterAgent,
    CriticAgent,
    EvaluationAgent,
]:
    return (
        MemoryAgent(model, memory, community),
        ContentAnalyzerAgent(model, memory),
        ResearchAgent(model, retrieval, community),
        StrategyAgent(model),
        WriterAgent(model),
        CriticAgent(model),
        EvaluationAgent(model, evaluation, memory),
    )


def _community_search_enabled(source_scope: dict[str, Any]) -> bool:
    return _source_flag(source_scope, "include_community_posts", True)


def _creator_profile_enabled(source_scope: dict[str, Any]) -> bool:
    return _source_flag(source_scope, "include_creator_profile", True)


def _source_flag(
    source_scope: dict[str, Any],
    key: str,
    default: bool,
) -> bool:
    value = source_scope.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _community_scope(
    context: AgentExecutionContext,
) -> CommunityAccessScope:
    return CommunityAccessScope(
        tenant_id=context.identity.tenant_id,
        creator_id=context.identity.creator_id,
        actor_id=context.identity.creator_id,
        roles=frozenset({"CREATOR"}),
        trace_id=context.identity.trace_id,
    )


def _community_search_request(
    context: AgentExecutionContext,
) -> CommunitySearchRequest:
    queries: list[str] = []
    configured = context.goal.constraints.get("research_queries")
    if isinstance(configured, (list, tuple)):
        queries.extend(str(value) for value in configured)
    queries.append(context.goal.text)
    sanitizer = CreatorPrivacySanitizer()
    safe_queries = tuple(
        dict.fromkeys(
            normalized
            for value in queries
            if (normalized := sanitizer.sanitize(value).strip()[:500])
        )
    )[:3]
    source_scope = context.goal.source_scope
    return CommunitySearchRequest.model_validate(
        {
            "queries": safe_queries or (context.goal.text[:500],),
            "tags": _scope_values(source_scope.get("tags"), limit=20),
            "creator_ids": _scope_values(
                source_scope.get("creator_ids"),
                limit=20,
            ),
            "content_types": _scope_values(
                source_scope.get("content_types"),
                limit=10,
            ),
            "published_after": source_scope.get("published_after"),
            "published_before": source_scope.get("published_before"),
            "limit": 10,
        }
    )


def _scope_values(value: Any, *, limit: int) -> tuple[str, ...]:
    if isinstance(value, str):
        values: tuple[Any, ...] = tuple(value.split(","))
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(value)
    else:
        return ()
    return tuple(
        dict.fromkeys(item for raw in values if (item := str(raw).strip()[:128]))
    )[:limit]


def _community_evidence(
    result: CommunitySearchResult,
    *,
    tenant_id: str,
) -> tuple[EvidenceItem, ...]:
    items: list[EvidenceItem] = []
    for candidate in result.candidates:
        post = candidate.post
        if post.tenant_id != tenant_id or not post.is_public_and_published:
            continue
        normalized_score = (
            candidate.score / (candidate.score + 1.0) if candidate.score > 0 else 0.0
        )
        score = min(
            1.0,
            max(0.0, normalized_score * 0.8 + (1.0 / candidate.rank) * 0.2),
        )
        summary = (post.body or post.description).strip()[:4_000]
        items.append(
            EvidenceItem(
                id=f"community:{post.post_id}",
                title=post.title,
                summary=summary,
                source=(post.source_url or f"{post.source_system}:{post.post_id}"),
                source_type="community_post",
                requires_verification=False,
                document_id=post.post_id,
                source_url=post.source_url,
                retrieval_channels=(f"COMMUNITY:{candidate.channel}",),
                score=score,
                score_breakdown={
                    "community": score,
                    "raw": max(0.0, candidate.score),
                },
                published_at=post.published_at,
                authority_verified=True,
            )
        )
    return tuple(items)


async def _community_comment_evidence(
    community: CreatorCommunityProvider,
    context: AgentExecutionContext,
    result: CommunitySearchResult,
) -> tuple[EvidenceItem, ...]:
    scope = _community_scope(context)
    items: list[EvidenceItem] = []
    for candidate in result.candidates[:4]:
        post = candidate.post
        if post.tenant_id != scope.tenant_id or not post.is_public_and_published:
            continue
        try:
            page = await community.get_comments(
                scope,
                post_id=post.post_id,
                cursor=None,
                limit=3,
                parent_id=None,
                sort=CommunityCommentSort.HOT,
            )
        except Exception as exc:
            logger.warning(
                "Creator community comments unavailable post_id=%s error=%s",
                post.post_id,
                type(exc).__name__,
            )
            continue
        for index, comment in enumerate(page.items, start=1):
            score = min(
                1.0, max(0.35, 0.72 - (index * 0.05) + (candidate.score * 0.01))
            )
            items.append(
                EvidenceItem(
                    id=f"community:comment:{comment.comment_id}",
                    title=f"Reader question on {post.title}",
                    summary=comment.content.strip()[:2_000],
                    source=(
                        post.source_url
                        or f"{post.source_system}:{post.post_id}#{comment.comment_id}"
                    ),
                    source_type="community_comment",
                    requires_verification=False,
                    document_id=comment.comment_id,
                    source_url=post.source_url,
                    retrieval_channels=("COMMUNITY:COMMENTS",),
                    score=score,
                    score_breakdown={
                        "comment_rank": float(index),
                        "like_count": float(comment.like_count),
                    },
                    published_at=comment.created_at,
                    authority_verified=True,
                )
            )
    return tuple(items)


def _deduplicate_evidence(
    evidence: list[EvidenceItem],
) -> tuple[EvidenceItem, ...]:
    best: dict[str, EvidenceItem] = {}
    for item in evidence:
        key = item.id or item.document_id
        existing = best.get(key)
        if existing is None or item.score > existing.score:
            best[key] = item
    comments = [item for item in best.values() if "comment" in item.source_type.lower()]
    posts = [
        item for item in best.values() if "comment" not in item.source_type.lower()
    ]
    comments.sort(key=lambda item: item.score, reverse=True)
    posts.sort(key=lambda item: item.score, reverse=True)
    return tuple((comments[:6] + posts[:8])[:14])


def _used_angles_from_artifacts(
    artifacts: tuple[CreatorArtifact, ...],
):
    from app.creator.memory.angles import UsedContentAngle

    profile_artifact = _latest_optional(artifacts, ArtifactKind.CREATOR_PROFILE)
    if profile_artifact is None:
        return ()
    raw = profile_artifact.content.get("used_angles") or ()
    angles: list[UsedContentAngle] = []
    if not isinstance(raw, (list, tuple)):
        return ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            angles.append(UsedContentAngle.model_validate(item))
        except Exception:
            continue
    return tuple(angles)


def _validate_topic_angle_novelty(
    document: TopicOptionsDocument,
    used_angles,
) -> None:
    if not used_angles:
        return
    for option in document.options:
        if option.recommendation != TopicRecommendation.WRITE_NOW:
            continue
        key = normalize_angle_key(option.title, option.angle)
        if angles_conflict(key, used_angles):
            raise SpecialistAgentError(
                "TOPIC_ANGLE_REUSED",
                (
                    f"WRITE_NOW topic {option.id!r} repeats a previously used "
                    "content angle; choose a differentiated thesis"
                ),
                retryable=True,
            )


async def _remember_selected_topic_angle(
    memory: CreatorMemoryReader | None,
    context: AgentExecutionContext,
):
    remember = getattr(memory, "remember_used_content_angle", None)
    if memory is None or remember is None:
        return None
    selected_id = str(context.goal.constraints.get("selected_topic_id") or "").strip()
    if not selected_id:
        return None
    topics_artifact = _latest_optional(context.artifacts, ArtifactKind.TOPIC_OPTIONS)
    if topics_artifact is None:
        return None
    try:
        topics = TopicOptionsDocument.model_validate(topics_artifact.content)
    except Exception:
        return None
    selected = next(
        (option for option in topics.options if option.id == selected_id),
        None,
    )
    if selected is None:
        return None
    try:
        return await remember(
            tenant_id=context.identity.tenant_id,
            creator_id=context.identity.creator_id,
            title=selected.title,
            angle=selected.angle,
            task_id=context.identity.task_id,
            artifact_id=topics_artifact.id,
        )
    except Exception as exc:
        logger.warning(
            "Creator angle memory write skipped task_id=%s error=%s",
            context.identity.task_id,
            type(exc).__name__,
        )
        return None


def _validate_topic_grounding(
    document: TopicOptionsDocument,
    inputs: list[dict[str, Any]],
) -> None:
    available_ids = {
        str(evidence.get("id") or "")
        for item in inputs
        for evidence in ((item.get("content") or {}).get("evidence") or ())
        if isinstance(item, dict)
        and isinstance(item.get("content"), dict)
        and isinstance(evidence, dict)
        and evidence.get("id")
    }
    if not available_ids:
        return
    for option in document.options:
        if option.recommendation != TopicRecommendation.WRITE_NOW:
            continue
        cited = set(option.evidence_ids) | set(option.comment_ids)
        if not cited.intersection(available_ids):
            raise SpecialistAgentError(
                "TOPIC_EVIDENCE_REQUIRED",
                (
                    f"WRITE_NOW topic {option.id!r} must cite at least one "
                    "authorized evidence or comment id from the evidence pack"
                ),
                retryable=True,
            )
        if not (option.reader_question.strip() and option.why_now.strip()):
            raise SpecialistAgentError(
                "TOPIC_DECISION_INCOMPLETE",
                (
                    f"WRITE_NOW topic {option.id!r} must include reader_question "
                    "and why_now"
                ),
                retryable=True,
            )


def _combined_retrieval_availability(
    result: CreatorRetrievalResult | None,
    community: CommunitySearchResult | None,
    *,
    failures: bool,
    has_evidence: bool,
) -> DataAvailability:
    degraded = failures or bool(community is not None and community.degraded_services)
    if result is not None and result.availability != RetrievalAvailability.AVAILABLE:
        degraded = True
    if has_evidence:
        return DataAvailability.PARTIAL if degraded else DataAvailability.AVAILABLE
    if result is not None or community is not None:
        return DataAvailability.PARTIAL
    return DataAvailability.NOT_CONNECTED


def _retrieval_audit_metadata(
    result: CreatorRetrievalResult,
) -> dict[str, Any]:
    return {
        "availability": result.availability.value,
        "tool_calls": result.tool_calls,
        "limitations": list(result.limitations),
        "rounds": [
            {
                "retrieval_round": audit.retrieval_round,
                "intent": audit.plan.intent.value,
                "query_hashes": [query_sha256(query) for query in audit.plan.queries],
                "channels": [channel.value for channel in audit.plan.channels],
                "source_reports": [
                    report.model_dump(mode="json") for report in audit.source_reports
                ],
                "reranker": audit.rerank_report.model_dump(mode="json"),
                "candidate_count": audit.candidate_count,
                "hydrated_count": audit.hydrated_count,
                "evidence_count": audit.evidence_count,
                "grade": audit.grade.model_dump(mode="json"),
            }
            for audit in result.rounds
        ],
    }


async def _load_memory(
    memory: CreatorMemoryReader | None,
    context: AgentExecutionContext,
    *,
    include_task_state: bool,
    include_profile: bool,
    include_semantic: bool,
) -> CreatorMemoryBundle | None:
    if memory is None:
        return None
    try:
        return await memory.load(
            CreatorMemoryQuery(
                tenant_id=context.identity.tenant_id,
                creator_id=context.identity.creator_id,
                task_id=context.identity.task_id,
                run_id=context.identity.run_id,
                query=context.goal.text,
                source_scope=context.goal.source_scope,
                include_task_state=include_task_state,
                include_profile=include_profile,
                include_semantic=include_semantic,
            )
        )
    except Exception as exc:
        logger.warning(
            "Creator Service memory load failed agent_task_id=%s run_id=%s error=%s",
            context.identity.task_id,
            context.identity.run_id,
            type(exc).__name__,
        )
        return None


def _profile_availability(
    memory: CreatorMemoryBundle | None,
) -> DataAvailability:
    if memory is None:
        return DataAvailability.NOT_CONNECTED
    return DataAvailability(memory.overall_availability.value)


def _history_availability(
    memory: CreatorMemoryBundle | None,
) -> DataAvailability:
    if memory is None:
        return DataAvailability.NOT_CONNECTED
    return DataAvailability(memory.history_availability.value)


def _memory_limitations(
    memory: CreatorMemoryBundle | None,
    *,
    chinese: bool = False,
) -> tuple[str, ...]:
    if memory is None:
        return (
            (
                "尚未配置创作者记忆服务。"
                if chinese
                else "Creator memory service is not configured."
            ),
        )
    reports = tuple(
        (
            f"{report.tier.value} 层记忆当前状态为 {report.status.value}。"
            if chinese
            else f"{report.tier.value} memory is {report.status.value.lower()}."
        )
        for report in memory.source_reports
        if report.status
        in {
            MemorySourceStatus.DEGRADED,
            MemorySourceStatus.DISABLED,
        }
    )
    return _merge_strings(memory.limitations, reports)


def _with_language_instruction(
    system_prompt: str,
    payload: dict[str, Any],
) -> str:
    constraints = payload.get("constraints")
    if not isinstance(constraints, dict):
        return system_prompt
    language = str(constraints.get("language") or "").strip()
    if not language:
        return system_prompt
    if language.lower().startswith("zh"):
        instruction = (
            "所有面向用户的标题、摘要、解释和正文必须使用简体中文。"
            "constraints 中的 audience、reader_takeaway、tone、key_points 和 "
            "reference_notes 共同构成创作者简报，必须作为生成内容的硬约束。"
            "优先帮助目标读者完成具体判断或行动，不要暴露内部 Agent 编排术语，"
            "除非它们就是文章主题。"
            "JSON 键、枚举值、ID、代码标识符和证据引用保持原样。"
        )
    else:
        instruction = (
            f"Use {language} for all user-visible titles, summaries, explanations, "
            "and generated content. Treat audience, reader_takeaway, tone, key_points, "
            "and reference_notes in constraints as the creator's hard requirements. "
            "Optimize for a concrete reader decision or action. Keep JSON keys, enum "
            "values, IDs, code identifiers, and evidence citations unchanged."
        )
    return f"{system_prompt}\n{instruction}"


def _uses_chinese(constraints: dict[str, Any]) -> bool:
    return str(constraints.get("language") or "").strip().lower().startswith("zh")


def _localized_summary(
    context: AgentExecutionContext,
    *,
    chinese: str,
    english: str,
) -> str:
    return chinese if _uses_chinese(context.goal.constraints) else english


def _evaluation_reason_zh(metric: str) -> str:
    reasons = {
        "retrieval_recall_at_k": "根据标注相关证据在前 K 条结果中的覆盖率计算。",
        "retrieval_precision_at_k": "根据前 K 条结果中相关证据的占比计算。",
        "retrieval_mrr": "根据首条相关证据的排序位置计算。",
        "retrieval_ndcg_at_k": "根据前 K 条结果的相关性与排序质量计算。",
        "retrieval_acl_safety": "根据检索结果是否满足租户访问控制要求计算。",
        "agent_task_success_rate": "根据任务最终状态和质量门禁结果计算。",
        "agent_tool_calling_accuracy": "根据工具调用与预期能力契约的一致性计算。",
        "agent_planning_quality": "根据计划步骤、依赖关系和执行结果计算。",
        "generation_faithfulness": "根据草稿主张与授权证据之间的对应关系计算。",
        "generation_relevance": "根据草稿对必需概念和目标问题的覆盖情况计算。",
        "generation_style_consistency": "根据明确的格式与风格规则计算。",
    }
    return reasons.get(metric, "已根据当前运行记录计算该指标。")


def _memory_source_metadata(
    memory: CreatorMemoryBundle | None,
) -> list[dict[str, Any]]:
    if memory is None:
        return []
    return [report.model_dump(mode="json") for report in memory.source_reports]


def _merge_strings(
    first: tuple[str, ...],
    second: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _draft_result(
    document: DraftDocument,
    usage: AgentUsage,
    *,
    parents: tuple[str, ...],
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> AgentResult:
    word_count = len(document.body_markdown.split())
    return AgentResult(
        artifacts=(
            ArtifactPayload(
                kind=ArtifactKind.DRAFT,
                content=document.model_dump(mode="json"),
                parent_ids=parents,
                metadata={
                    "word_count": word_count,
                    "unsupported_claim_count": len(document.unsupported_claims),
                    "citation_count": len(document.citations),
                    **(metadata or {}),
                },
                confidence=0.78,
            ),
        ),
        facts=(
            FactDraft(key="writer.word_count", value=word_count),
            FactDraft(
                key="writer.unsupported_claim_count",
                value=len(document.unsupported_claims),
            ),
            FactDraft(
                key="writer.citation_count",
                value=len(document.citations),
            ),
        ),
        usage=usage,
        summary=summary,
    )


def _artifact_payloads(
    artifacts: tuple[CreatorArtifact, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": artifact.id,
            "kind": artifact.kind.value,
            "content": artifact.content,
            "confidence": artifact.confidence,
        }
        for artifact in artifacts
    ]


def _draft_revision_instructions(
    *,
    draft_artifact_id: str,
    critique_artifact: CreatorArtifact | None,
    constraints: dict[str, Any],
) -> tuple[str, ...]:
    instructions: list[str] = []
    revision_instruction = str(constraints.get("revision_instruction") or "").strip()
    if revision_instruction:
        instructions.append(revision_instruction)
    if critique_artifact is not None:
        critique = CritiqueDocument.model_validate(critique_artifact.content)
        if critique.reviewed_artifact_id == draft_artifact_id:
            instructions.extend(str(item) for item in critique.revision_instructions)
        elif not constraints.get("draft_annotations") and not constraints.get(
            "draft_feedback"
        ):
            raise SpecialistAgentError(
                "CRITIQUE_DRAFT_MISMATCH",
                "Revision input critique does not review the selected draft",
                retryable=False,
            )
    if constraints.get("draft_revision_requested_from") == draft_artifact_id:
        for raw in constraints.get("draft_annotations") or ():
            if not isinstance(raw, dict):
                continue
            section = raw.get("section")
            note = str(raw.get("note") or "").strip()
            if section is not None and note:
                instructions.append(f"Section {section}: {note}")
        feedback = str(constraints.get("draft_feedback") or "").strip()
        if feedback:
            instructions.append(feedback)
    return tuple(instructions)


def _latest_required(
    artifacts: tuple[CreatorArtifact, ...],
    kind: ArtifactKind,
) -> CreatorArtifact:
    artifact = _latest_optional(artifacts, kind)
    if artifact is None:
        raise SpecialistAgentError(
            "AGENT_INPUT_MISSING",
            f"Required artifact {kind.value} is missing",
            retryable=False,
        )
    return artifact


def _latest_optional(
    artifacts: tuple[CreatorArtifact, ...],
    kind: ArtifactKind,
) -> CreatorArtifact | None:
    matching = [artifact for artifact in artifacts if artifact.kind == kind]
    if not matching:
        return None
    return max(matching, key=lambda item: (item.revision, item.created_at))


def _latest_draft(
    artifacts: tuple[CreatorArtifact, ...],
) -> CreatorArtifact:
    drafts = [
        artifact
        for artifact in artifacts
        if artifact.kind in {ArtifactKind.DRAFT, ArtifactKind.SOURCE_DRAFT}
    ]
    if not drafts:
        raise SpecialistAgentError(
            "AGENT_INPUT_MISSING",
            "A draft artifact is required",
            retryable=False,
        )
    return max(drafts, key=lambda item: (item.revision, item.created_at))


def _evidence_ids(artifacts: tuple[CreatorArtifact, ...]) -> tuple[str, ...]:
    evidence_artifact = _latest_optional(artifacts, ArtifactKind.EVIDENCE_PACK)
    if evidence_artifact is None:
        return ()
    document = EvidencePackDocument.model_validate(evidence_artifact.content)
    return tuple(item.id for item in document.evidence)


def _evidence_context(
    artifacts: tuple[CreatorArtifact, ...],
) -> tuple[dict[str, Any], ...]:
    evidence_artifact = _latest_optional(artifacts, ArtifactKind.EVIDENCE_PACK)
    if evidence_artifact is None:
        return ()
    document = EvidencePackDocument.model_validate(evidence_artifact.content)
    return tuple(
        {
            "evidence_id": item.id,
            "title": item.title,
            "summary": item.summary,
            "source": item.source,
            "source_url": item.source_url,
        }
        for item in document.evidence
    )


def _ground_draft_citations(
    document: DraftDocument,
    evidence_context: tuple[dict[str, Any], ...],
) -> DraftDocument:
    by_id = {
        str(item["evidence_id"]): item
        for item in evidence_context
        if item.get("evidence_id")
    }
    grounded = []
    seen: set[tuple[str, str]] = set()
    for citation in document.citations:
        source = by_id.get(citation.evidence_id)
        key = (citation.claim_text, citation.evidence_id)
        if (
            source is None
            or citation.claim_text not in document.body_markdown
            or key in seen
        ):
            continue
        seen.add(key)
        grounded.append(
            citation.model_copy(
                update={
                    "source_title": str(source.get("title") or ""),
                    "source_url": source.get("source_url") or None,
                }
            )
        )
    return document.model_copy(update={"citations": tuple(grounded)})
