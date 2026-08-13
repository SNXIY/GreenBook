from __future__ import annotations

import hashlib
import logging
from typing import cast

from app.creator.drafts.service import CreatorDraftService
from app.creator.memory.ports import CreatorLongTermProfileStore
from app.creator.providers.models import (
    CommunityAccessScope,
    CommunityPost,
    CommunitySearchRequest,
)
from app.creator.providers.ports import CreatorCommunityProvider
from app.creator.retrieval.models import (
    CreatorRetrievalRequest,
    RetrievalAvailability,
)
from app.creator.retrieval.ports import CreatorRetrievalReader
from app.creator.runtime.models import CreatorTaskKind
from app.creator.tools.gateway import CreatorToolDefinition
from app.creator.tools.models import (
    CommentsToolData,
    CreatorProfileToolData,
    CreatorToolCallContext,
    CreatorToolProvenance,
    CreatorToolRisk,
    DraftToolData,
    EngagementToolData,
    GetCommentsInput,
    GetCreatorProfileInput,
    GetEngagementInput,
    GetPostDetailInput,
    GetPostMetricsInput,
    GetUserHistoryInput,
    PostDetailToolData,
    PostMetricsToolData,
    PostReference,
    SaveDraftInput,
    SearchPostItem,
    SearchPostsInput,
    SearchPostsToolData,
    ToolHandlerResult,
    UpdateDraftInput,
    UserHistoryToolData,
)
from app.creator.tools.ports import CreatorToolHandler

logger = logging.getLogger(__name__)


class CreatorToolService:
    def __init__(
        self,
        *,
        community: CreatorCommunityProvider,
        drafts: CreatorDraftService,
        retrieval: CreatorRetrievalReader | None = None,
        profile_store: CreatorLongTermProfileStore | None = None,
    ) -> None:
        self._community = community
        self._drafts = drafts
        self._retrieval = retrieval
        self._profile_store = profile_store

    def definitions(self) -> tuple[CreatorToolDefinition, ...]:
        return (
            CreatorToolDefinition(
                name="get_creator_profile",
                description=(
                    "Load the authenticated creator's community and writing profile."
                ),
                risk=CreatorToolRisk.READ,
                input_model=GetCreatorProfileInput,
                output_model=CreatorProfileToolData,
                handler=cast(CreatorToolHandler, self.get_creator_profile),
            ),
            CreatorToolDefinition(
                name="get_user_history",
                description=(
                    "List the authenticated creator's historical posts using an "
                    "opaque cursor."
                ),
                risk=CreatorToolRisk.READ,
                input_model=GetUserHistoryInput,
                output_model=UserHistoryToolData,
                handler=cast(CreatorToolHandler, self.get_user_history),
            ),
            CreatorToolDefinition(
                name="search_posts",
                description=(
                    "Search authorized published community posts with hybrid "
                    "retrieval and provenance."
                ),
                risk=CreatorToolRisk.READ,
                input_model=SearchPostsInput,
                output_model=SearchPostsToolData,
                handler=cast(CreatorToolHandler, self.search_posts),
            ),
            CreatorToolDefinition(
                name="get_post_detail",
                description="Load one authorized community post with its full body.",
                risk=CreatorToolRisk.READ,
                input_model=GetPostDetailInput,
                output_model=PostDetailToolData,
                handler=cast(CreatorToolHandler, self.get_post_detail),
            ),
            CreatorToolDefinition(
                name="get_comments",
                description=(
                    "List authorized comments for a community post using an "
                    "opaque cursor."
                ),
                risk=CreatorToolRisk.READ,
                input_model=GetCommentsInput,
                output_model=CommentsToolData,
                handler=cast(CreatorToolHandler, self.get_comments),
            ),
            CreatorToolDefinition(
                name="get_post_metrics",
                description=(
                    "Load an owned post's engagement metrics and normalized rates."
                ),
                risk=CreatorToolRisk.READ,
                input_model=GetPostMetricsInput,
                output_model=PostMetricsToolData,
                handler=cast(CreatorToolHandler, self.get_post_metrics),
            ),
            CreatorToolDefinition(
                name="get_engagement",
                description=(
                    "Load aggregate and time-series engagement for the "
                    "authenticated creator."
                ),
                risk=CreatorToolRisk.READ,
                input_model=GetEngagementInput,
                output_model=EngagementToolData,
                handler=cast(CreatorToolHandler, self.get_engagement),
            ),
            CreatorToolDefinition(
                name="save_draft",
                description=(
                    "Create the first immutable version of a task-owned draft. "
                    "An idempotency key is required."
                ),
                risk=CreatorToolRisk.DRAFT_WRITE,
                input_model=SaveDraftInput,
                output_model=DraftToolData,
                handler=cast(CreatorToolHandler, self.save_draft),
            ),
            CreatorToolDefinition(
                name="update_draft",
                description=(
                    "Append a draft version with optimistic locking and a required "
                    "idempotency key."
                ),
                risk=CreatorToolRisk.DRAFT_WRITE,
                input_model=UpdateDraftInput,
                output_model=DraftToolData,
                handler=cast(CreatorToolHandler, self.update_draft),
            ),
        )

    async def get_creator_profile(
        self,
        request: GetCreatorProfileInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        del request
        scope = _community_scope(context)
        community_profile = None
        memory_profile = None
        degraded: list[str] = []
        try:
            community_profile = await self._community.get_creator_profile(scope)
        except Exception as exc:
            logger.warning(
                "Creator community profile degraded trace_id=%s backend=%s error=%s",
                context.trace_id,
                self._community.backend_name,
                type(exc).__name__,
            )
            degraded.append(self._community.backend_name)
        if self._profile_store is not None:
            try:
                memory_profile = await self._profile_store.get(
                    tenant_id=scope.tenant_id,
                    creator_id=scope.creator_id,
                )
            except Exception as exc:
                logger.warning(
                    "Creator long profile degraded trace_id=%s backend=%s error=%s",
                    context.trace_id,
                    self._profile_store.backend_name,
                    type(exc).__name__,
                )
                degraded.append(self._profile_store.backend_name)
        if community_profile is None and memory_profile is None and degraded:
            raise RuntimeError("Creator profile sources are unavailable")

        expertise = _merge(
            memory_profile.expertise_tags if memory_profile else (),
            community_profile.expertise_tags if community_profile else (),
        )
        availability = (
            "AVAILABLE"
            if community_profile is not None and memory_profile is not None
            else (
                "PARTIAL"
                if community_profile is not None or memory_profile is not None
                else "EMPTY"
            )
        )
        data = CreatorProfileToolData(
            creator_id=scope.creator_id,
            display_name=(
                (memory_profile.display_name if memory_profile else "")
                or (
                    community_profile.display_name
                    if community_profile is not None
                    else ""
                )
            ),
            bio=(
                (memory_profile.bio if memory_profile else "")
                or (community_profile.bio if community_profile is not None else "")
            ),
            expertise_tags=expertise,
            audience_segments=(
                memory_profile.audience_segments if memory_profile else ()
            ),
            style_traits=memory_profile.style_traits if memory_profile else (),
            preferred_formats=(
                memory_profile.preferred_formats if memory_profile else ()
            ),
            language=memory_profile.language if memory_profile else "zh-CN",
            follower_count=(
                community_profile.follower_count if community_profile else 0
            ),
            following_count=(
                community_profile.following_count if community_profile else 0
            ),
            published_post_count=(
                community_profile.published_post_count if community_profile else 0
            ),
            memory_version=memory_profile.version if memory_profile else None,
            availability=availability,
        )
        provenance: list[CreatorToolProvenance] = []
        if community_profile is not None:
            provenance.append(
                CreatorToolProvenance(
                    source=community_profile.source_system,
                    reference=f"creator:{scope.creator_id}",
                    revision=community_profile.source_revision,
                )
            )
        if memory_profile is not None:
            provenance.append(
                CreatorToolProvenance(
                    source=memory_profile.source_system,
                    reference=f"creator-memory:{scope.creator_id}",
                    revision=memory_profile.source_revision,
                )
            )
        return ToolHandlerResult(
            data=data,
            provenance=tuple(provenance),
            degraded_services=tuple(dict.fromkeys(degraded)),
        )

    async def get_user_history(
        self,
        request: GetUserHistoryInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        page = await self._community.get_user_history(
            _community_scope(context),
            cursor=request.cursor,
            limit=request.limit,
            statuses=request.statuses,
        )
        return ToolHandlerResult(
            data=UserHistoryToolData(
                items=tuple(_post_reference(post) for post in page.items),
                next_cursor=page.next_cursor,
                has_more=page.has_more,
            ),
            provenance=(
                CreatorToolProvenance(
                    source=self._community.backend_name,
                    reference=f"creator:{context.principal.creator_id}:history",
                ),
            ),
        )

    async def search_posts(
        self,
        request: SearchPostsInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        degraded: list[str] = []
        if self._retrieval is not None:
            try:
                result = await self._retrieval.retrieve(
                    CreatorRetrievalRequest(
                        tenant_id=context.principal.tenant_id,
                        creator_id=context.principal.creator_id,
                        task_id=context.task_id or _tool_runtime_id("task", context),
                        run_id=context.run_id or _tool_runtime_id("run", context),
                        task_kind=CreatorTaskKind.RESEARCH_TOPIC,
                        goal=request.queries[0],
                        constraints={
                            "research_queries": list(request.queries),
                            "retrieval_intent": request.intent.value,
                        },
                        source_scope={
                            "include_community_posts": True,
                            "tags": list(request.tags),
                            "creator_ids": list(request.creator_ids),
                            "content_types": list(request.content_types),
                            "published_after": request.published_after,
                            "published_before": request.published_before,
                        },
                    )
                )
                if result.evidence or (
                    result.availability != RetrievalAvailability.NOT_CONNECTED
                ):
                    return ToolHandlerResult(
                        data=SearchPostsToolData(
                            items=tuple(
                                SearchPostItem(
                                    post_id=item.document_id,
                                    creator_id=item.creator_id,
                                    title=item.title,
                                    description=item.excerpt,
                                    excerpt=item.excerpt,
                                    tags=item.tags,
                                    source_url=item.source_url,
                                    published_at=item.published_at,
                                    score=item.score.final,
                                    channels=tuple(
                                        channel.value for channel in item.channels
                                    ),
                                    score_breakdown=item.score.model_dump(mode="json"),
                                    authority_verified=item.authority_verified,
                                )
                                for item in result.evidence[: request.limit]
                            ),
                            availability=result.availability.value,
                            limitations=result.limitations,
                        ),
                        provenance=tuple(
                            CreatorToolProvenance(
                                source=item.source_system,
                                reference=(
                                    item.source_url or f"post:{item.document_id}"
                                ),
                                authority_verified=item.authority_verified,
                            )
                            for item in result.evidence[: request.limit]
                        ),
                    )
            except Exception as exc:
                logger.warning(
                    "Creator retrieval tool degraded trace_id=%s error=%s",
                    context.trace_id,
                    type(exc).__name__,
                )
                degraded.append("creator-agentic-retrieval")

        fallback = await self._community.search_posts(
            _community_scope(context),
            CommunitySearchRequest(
                queries=request.queries,
                tags=request.tags,
                creator_ids=request.creator_ids,
                content_types=request.content_types,
                published_after=request.published_after,
                published_before=request.published_before,
                limit=request.limit,
            ),
        )
        degraded.extend(fallback.degraded_services)
        return ToolHandlerResult(
            data=SearchPostsToolData(
                items=tuple(
                    SearchPostItem(
                        **_post_reference(candidate.post).model_dump(),
                        excerpt=(candidate.post.description or candidate.post.body)[
                            :4_000
                        ],
                        score=max(0.0, min(1.0, candidate.score)),
                        channels=(candidate.channel,),
                        authority_verified=True,
                    )
                    for candidate in fallback.candidates[: request.limit]
                ),
                availability=("AVAILABLE" if fallback.candidates else "EMPTY"),
                limitations=(),
            ),
            provenance=(
                CreatorToolProvenance(
                    source=self._community.backend_name,
                    reference="community-search",
                ),
            ),
            degraded_services=tuple(dict.fromkeys(degraded)),
        )

    async def get_post_detail(
        self,
        request: GetPostDetailInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        post = await self._community.get_post_detail(
            _community_scope(context),
            post_id=request.post_id,
        )
        return ToolHandlerResult(
            data=PostDetailToolData(
                **_post_reference(post).model_dump(),
                body=post.body,
            ),
            provenance=(_post_provenance(post),),
        )

    async def get_comments(
        self,
        request: GetCommentsInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        page = await self._community.get_comments(
            _community_scope(context),
            post_id=request.post_id,
            cursor=request.cursor,
            limit=request.limit,
            parent_id=request.parent_id,
            sort=request.sort,
        )
        return ToolHandlerResult(
            data=CommentsToolData(
                items=page.items,
                next_cursor=page.next_cursor,
                has_more=page.has_more,
            ),
            provenance=(
                CreatorToolProvenance(
                    source=self._community.backend_name,
                    reference=f"post:{request.post_id}:comments",
                ),
            ),
        )

    async def get_post_metrics(
        self,
        request: GetPostMetricsInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        metrics = await self._community.get_post_metrics(
            _community_scope(context),
            post_id=request.post_id,
        )
        return ToolHandlerResult(
            data=PostMetricsToolData(
                post_id=metrics.post_id,
                metrics=metrics.metrics,
                like_rate=metrics.like_rate,
                favorite_rate=metrics.favorite_rate,
                comment_rate=metrics.comment_rate,
                collected_at=metrics.collected_at,
            ),
            provenance=(
                CreatorToolProvenance(
                    source=metrics.source_system,
                    reference=f"post:{metrics.post_id}:metrics",
                ),
            ),
        )

    async def get_engagement(
        self,
        request: GetEngagementInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        report = await self._community.get_engagement(
            _community_scope(context),
            post_ids=request.post_ids,
            start=request.start,
            end=request.end,
        )
        return ToolHandlerResult(
            data=EngagementToolData(
                creator_id=report.creator_id,
                post_ids=report.post_ids,
                aggregate=report.aggregate,
                time_series=report.time_series,
                generated_at=report.generated_at,
            ),
            provenance=(
                CreatorToolProvenance(
                    source=report.source_system,
                    reference=f"creator:{report.creator_id}:engagement",
                ),
            ),
        )

    async def save_draft(
        self,
        request: SaveDraftInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        result = await self._drafts.save_draft(
            tenant_id=context.principal.tenant_id,
            creator_id=context.principal.creator_id,
            task_id=request.task_id,
            title=request.title,
            content_markdown=request.content_markdown,
            source_artifact_id=request.source_artifact_id,
            editor_type=_editor_type(context),
            actor_id=context.principal.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return ToolHandlerResult(
            data=_draft_data(result),
            provenance=(
                CreatorToolProvenance(
                    source="mindflow-draft-store",
                    reference=f"draft:{result.draft.id}:v{result.version.version}",
                    revision=result.version.content_sha256,
                ),
            ),
        )

    async def update_draft(
        self,
        request: UpdateDraftInput,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult:
        result = await self._drafts.update_draft(
            tenant_id=context.principal.tenant_id,
            creator_id=context.principal.creator_id,
            draft_id=request.draft_id,
            expected_version=request.expected_version,
            title=request.title,
            content_markdown=request.content_markdown,
            source_artifact_id=request.source_artifact_id,
            editor_type=_editor_type(context),
            actor_id=context.principal.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return ToolHandlerResult(
            data=_draft_data(result),
            provenance=(
                CreatorToolProvenance(
                    source="mindflow-draft-store",
                    reference=f"draft:{result.draft.id}:v{result.version.version}",
                    revision=result.version.content_sha256,
                ),
            ),
        )


def _community_scope(context: CreatorToolCallContext) -> CommunityAccessScope:
    principal = context.principal
    return CommunityAccessScope(
        tenant_id=principal.tenant_id,
        creator_id=principal.creator_id,
        actor_id=principal.actor_id,
        roles=principal.roles,
        trace_id=context.trace_id,
    )


def _post_reference(post: CommunityPost) -> PostReference:
    return PostReference(
        post_id=post.post_id,
        creator_id=post.creator_id,
        creator_name=post.creator_name,
        title=post.title,
        description=post.description,
        tags=post.tags,
        content_type=post.content_type,
        visibility=post.visibility,
        status=post.status,
        source_url=post.source_url,
        published_at=post.published_at,
        metrics=post.metrics,
    )


def _post_provenance(post: CommunityPost) -> CreatorToolProvenance:
    return CreatorToolProvenance(
        source=post.source_system,
        reference=post.source_url or f"post:{post.post_id}",
        revision=post.source_revision,
    )


def _draft_data(result) -> DraftToolData:
    return DraftToolData(
        draft_id=result.draft.id,
        task_id=result.draft.task_id,
        title=result.version.title,
        status=result.draft.status,
        current_version=result.draft.current_version,
        version=result.version.version,
        content_sha256=result.version.content_sha256,
        source_artifact_id=result.version.source_artifact_id,
        replayed=result.replayed,
        updated_at=result.draft.updated_at,
    )


def _editor_type(context: CreatorToolCallContext) -> str:
    caller = context.principal.caller
    if caller.endswith("Agent"):
        return "AGENT"
    if caller == "MCP":
        return "MCP"
    return "SERVICE"


def _tool_runtime_id(prefix: str, context: CreatorToolCallContext) -> str:
    digest = hashlib.sha256(f"{context.trace_id}:{prefix}".encode()).hexdigest()[
        :32
    ]
    return f"{prefix}-{digest}"


def _merge(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))
