import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from community.providers import (
    CommunityDataProvider,
    JavaCommunityDataProvider,
    create_community_provider,
)
from community.tools import CommunityContextToolset, default_community_context_loader
from database import DatabaseManager
from moderation.services.callback_outbox import ModerationCallbackDispatcher
from moderation.services.policies import ModerationPolicyService
from moderation.services.ports import (
    NoopKnowledgeIndex,
    NoopReviewQueueIndex,
    ReviewQueueIndex,
)
from moderation.services.queue import RedisReviewQueueIndex
from moderation.services.statistics import ModerationStatisticsService
from moderation.services.workflow import ModerationGraph, ModerationWorkflowService
from rag.cases import default_case_retriever
from rag.cases.database import DatabaseCaseRetriever
from rag.cases.hybrid import HybridCaseRetriever
from rag.embedding import HashingTextEmbedder
from rag.policy import (
    AgenticPolicyRetriever,
    default_agentic_policy_retriever,
    default_policy_retriever,
)
from rag.policy.database import DatabasePolicyRetriever
from rag.policy.hybrid import HybridPolicyRetriever
from rag.qdrant import ModerationQdrantIndex

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModerationServiceContainer:
    database: DatabaseManager
    workflow: ModerationWorkflowService
    policies: ModerationPolicyService
    statistics: ModerationStatisticsService
    community_provider: CommunityDataProvider
    callback_dispatcher: ModerationCallbackDispatcher


@asynccontextmanager
async def initialize_moderation_services(
    graph: ModerationGraph,
) -> AsyncGenerator[ModerationServiceContainer, None]:
    from core import settings

    database = DatabaseManager()
    await database.start(
        settings.moderation_database_url(),
        create_schema=settings.MODERATION_AUTO_CREATE_SCHEMA,
    )
    embedder = HashingTextEmbedder(settings.MODERATION_VECTOR_SIZE)
    qdrant_index: ModerationQdrantIndex | None = None
    redis_index: RedisReviewQueueIndex | None = None
    community_provider: CommunityDataProvider | None = None
    try:
        community_provider = create_community_provider(
            java_base_url=settings.JAVA_COMMUNITY_BASE_URL,
            java_auth_token=(
                settings.JAVA_COMMUNITY_AUTH_TOKEN.get_secret_value()
                if settings.JAVA_COMMUNITY_AUTH_TOKEN
                else None
            ),
            timeout=settings.COMMUNITY_HTTP_TIMEOUT,
        )
        context_tools = CommunityContextToolset(community_provider)
        default_community_context_loader.configure(context_tools)

        if settings.QDRANT_URL:
            candidate = ModerationQdrantIndex(
                url=settings.QDRANT_URL,
                api_key=(
                    settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None
                ),
                policy_collection=settings.QDRANT_POLICY_COLLECTION,
                case_collection=settings.QDRANT_CASE_COLLECTION,
                embedder=embedder,
            )
            try:
                await candidate.start()
                qdrant_index = candidate
            except Exception:
                logger.exception("Qdrant is unavailable; using database moderation retrieval")
                await candidate.close()

        queue_index: ReviewQueueIndex = NoopReviewQueueIndex()
        if settings.REDIS_URL:
            candidate_queue = RedisReviewQueueIndex(
                settings.REDIS_URL,
                settings.MODERATION_REDIS_QUEUE_KEY,
            )
            try:
                await candidate_queue.start()
                redis_index = candidate_queue
                queue_index = candidate_queue
            except Exception:
                logger.exception("Redis is unavailable; using the database review queue")
                await candidate_queue.close()

        database_policy_retriever = DatabasePolicyRetriever(database, embedder)
        policy_retriever = HybridPolicyRetriever(
            database_policy_retriever,
            qdrant_index,
        )
        case_retriever = HybridCaseRetriever(
            DatabaseCaseRetriever(database, embedder),
            qdrant_index,
        )
        default_policy_retriever.configure(policy_retriever)
        default_agentic_policy_retriever.configure(
            AgenticPolicyRetriever(
                database_policy_retriever,
                qdrant_index,
                settings.agentic_policy_rag_config(),
            )
        )
        default_case_retriever.configure(case_retriever)

        knowledge_index = qdrant_index or NoopKnowledgeIndex()
        policies = ModerationPolicyService(database, knowledge_index)
        if settings.MODERATION_SEED_DEFAULT_POLICIES:
            await policies.ensure_defaults()
        if qdrant_index is not None:
            try:
                await qdrant_index.sync_policies(database)
            except Exception:
                logger.exception(
                    "Existing policies could not be synchronized to Qdrant; database retrieval remains available"
                )

        workflow = ModerationWorkflowService(
            database=database,
            graph=graph,
            queue_index=queue_index,
            knowledge_index=knowledge_index,
        )
        callback_dispatcher = ModerationCallbackDispatcher(
            database=database,
            provider=community_provider,
            poll_seconds=settings.MODERATION_CALLBACK_POLL_SECONDS,
            concurrency=settings.MODERATION_CALLBACK_CONCURRENCY,
            lease_seconds=settings.MODERATION_CALLBACK_LEASE_SECONDS,
            retry_base_seconds=settings.MODERATION_CALLBACK_RETRY_BASE_SECONDS,
            retry_max_seconds=settings.MODERATION_CALLBACK_RETRY_MAX_SECONDS,
        )
        container = ModerationServiceContainer(
            database=database,
            workflow=workflow,
            policies=policies,
            statistics=ModerationStatisticsService(database),
            community_provider=community_provider,
            callback_dispatcher=callback_dispatcher,
        )
        yield container
    finally:
        default_community_context_loader.reset()
        default_policy_retriever.reset()
        default_agentic_policy_retriever.reset()
        default_case_retriever.reset()
        if redis_index is not None:
            await redis_index.close()
        if qdrant_index is not None:
            await qdrant_index.close()
        if isinstance(community_provider, JavaCommunityDataProvider):
            await community_provider.close()
        await database.close()
