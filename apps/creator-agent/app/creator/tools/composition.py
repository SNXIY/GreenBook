from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings
from app.creator.drafts.service import CreatorDraftService
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.providers.java import JavaCreatorCommunityProvider
from app.creator.providers.ports import CreatorCommunityProvider
from app.creator.retrieval.composition import open_creator_retrieval
from app.creator.retrieval.service import CreatorAgenticRetriever
from app.creator.tools.gateway import CreatorToolGateway
from app.creator.tools.models import CreatorToolCallContext, CreatorToolPrincipal
from app.creator.tools.service import CreatorToolService


@dataclass(frozen=True)
class CreatorToolRuntime:
    gateway: CreatorToolGateway
    community: CreatorCommunityProvider
    retrieval: CreatorAgenticRetriever | None
    database: CreatorDatabase
    principal: CreatorToolPrincipal

    def context(
        self,
        *,
        trace_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        remaining_call_budget: int = 1,
    ) -> CreatorToolCallContext:
        return CreatorToolCallContext(
            principal=self.principal,
            trace_id=trace_id or str(uuid.uuid4()),
            task_id=task_id,
            run_id=run_id,
            remaining_call_budget=remaining_call_budget,
        )


@asynccontextmanager
async def open_creator_tool_runtime(
    *,
    settings: Settings,
    database: CreatorDatabase | None = None,
    initialize_schema: bool = True,
) -> AsyncIterator[CreatorToolRuntime]:
    owns_database = database is None
    creator_database = database or CreatorDatabase.from_settings(settings)
    community: CreatorCommunityProvider | None = None
    try:
        async with AsyncExitStack() as stack:
            if initialize_schema:
                await creator_database.create_schema_for_development()
            retrieval = await stack.enter_async_context(
                open_creator_retrieval(
                    settings=settings,
                    database=creator_database,
                )
            )
            community = build_creator_community_provider(settings)
            draft_service = CreatorDraftService(creator_database.draft_store)
            tool_service = CreatorToolService(
                community=community,
                drafts=draft_service,
                retrieval=retrieval,
                profile_store=creator_database.profile_store,
            )
            gateway = CreatorToolGateway(
                tool_service.definitions(),
                audit_store=creator_database.tool_audit_store,
                timeout_seconds=settings.creator_tool_timeout_seconds,
                max_result_bytes=settings.creator_tool_max_result_bytes,
            )
            allowed = _csv(settings.creator_mcp_allowed_tools)
            unknown = set(allowed) - set(gateway.tool_names)
            if unknown:
                raise ValueError(
                    "CREATOR_MCP_ALLOWED_TOOLS contains unknown tools: "
                    + ", ".join(sorted(unknown))
                )
            principal = CreatorToolPrincipal(
                tenant_id=settings.creator_mcp_tenant_id,
                creator_id=settings.creator_mcp_creator_id,
                actor_id=settings.creator_mcp_actor_id,
                caller="MCP",
                roles=frozenset(_csv(settings.creator_mcp_roles)),
                allowed_tools=frozenset(allowed),
            )
            yield CreatorToolRuntime(
                gateway=gateway,
                community=community,
                retrieval=retrieval,
                database=creator_database,
                principal=principal,
            )
            if community is not None:
                await community.aclose()
                community = None
    finally:
        if community is not None:
            await community.aclose()
        if owns_database:
            await creator_database.dispose()


def build_creator_community_provider(
    settings: Settings,
) -> CreatorCommunityProvider:
    provider = settings.creator_community_provider.strip().lower()
    if provider != "java":
        raise ValueError("CREATOR_COMMUNITY_PROVIDER must be 'java'")
    return JavaCreatorCommunityProvider(
        base_url=settings.creator_community_java_base_url,
        shared_secret=settings.creator_community_java_shared_secret,
        service_name=settings.creator_community_java_service_name,
        allowed_tenant_id=settings.creator_community_java_tenant_id,
        timeout_seconds=settings.creator_community_timeout_seconds,
    )


def _csv(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )
