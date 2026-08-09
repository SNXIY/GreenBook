from __future__ import annotations

from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.creator.agents.gateway import (
    AiClientCreatorModelGateway,
    RoutedCreatorModelGateway,
)
from app.creator.agents.specialists import build_default_specialists
from app.creator.memory.ports import CreatorMemoryReader
from app.creator.model_client import CreatorModelClient
from app.creator.providers.ports import CreatorCommunityProvider
from app.creator.retrieval.ports import CreatorRetrievalReader
from app.creator.runtime.graph import CreatorRuntimeGraph
from app.creator.runtime.models import BudgetLimits
from app.creator.runtime.ports import CreatorArtifactStore, CreatorModelGateway
from app.creator.runtime.registry import CreatorAgentRegistry
from app.creator.runtime.runtime import LangGraphCreatorRuntime
from app.creator.runtime.supervisor import CreatorSupervisorAgent


class CreatorRuntimeSettings(Protocol):
    ai_provider: str
    ollama_base_url: str
    ollama_model: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    deepseek_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_thinking_enabled: bool
    creator_model_analysis_model: str
    creator_model_writer_model: str
    creator_model_critic_model: str
    creator_model_assist_model: str
    creator_max_supervisor_turns: int
    creator_max_agent_dispatches: int
    creator_max_model_calls: int
    creator_max_output_tokens: int
    creator_max_replans: int
    creator_max_writer_revisions: int
    creator_specialist_timeout_seconds: float


def build_creator_runtime(
    *,
    settings: CreatorRuntimeSettings,
    ai_client: CreatorModelClient,
    artifact_store: CreatorArtifactStore,
    checkpointer: BaseCheckpointSaver,
    memory: CreatorMemoryReader | None = None,
    retrieval: CreatorRetrievalReader | None = None,
    community: CreatorCommunityProvider | None = None,
    model_gateway: CreatorModelGateway | None = None,
) -> LangGraphCreatorRuntime:
    model = model_gateway or build_creator_model_gateway(
        settings=settings,
        ai_client=ai_client,
    )
    registry = CreatorAgentRegistry(
        build_default_specialists(
            model,
            memory,
            retrieval,
            community=community,
        )
    )
    supervisor = CreatorSupervisorAgent(registry)
    graph = CreatorRuntimeGraph(
        registry=registry,
        supervisor=supervisor,
        artifact_store=artifact_store,
        checkpointer=checkpointer,
        specialist_timeout_seconds=settings.creator_specialist_timeout_seconds,
    )
    return LangGraphCreatorRuntime(
        graph=graph,
        artifact_store=artifact_store,
        limits=BudgetLimits(
            max_supervisor_turns=settings.creator_max_supervisor_turns,
            max_agent_dispatches=settings.creator_max_agent_dispatches,
            max_model_calls=settings.creator_max_model_calls,
            max_output_tokens=settings.creator_max_output_tokens,
            max_replans=settings.creator_max_replans,
            max_writer_revisions=settings.creator_max_writer_revisions,
            specialist_timeout_seconds=settings.creator_specialist_timeout_seconds,
        ),
    )


def build_creator_model_gateway(
    *,
    settings: CreatorRuntimeSettings,
    ai_client: CreatorModelClient,
) -> CreatorModelGateway:
    validate_creator_model_settings(settings)
    base: CreatorModelGateway = AiClientCreatorModelGateway(ai_client)
    return RoutedCreatorModelGateway(
        base,
        analysis_model=settings.creator_model_analysis_model,
        writer_model=settings.creator_model_writer_model,
        critic_model=settings.creator_model_critic_model,
        assist_model=settings.creator_model_assist_model,
    )


def validate_creator_model_settings(settings: CreatorRuntimeSettings) -> None:
    provider = settings.ai_provider.strip().lower()
    if provider == "ollama":
        required = {
            "OLLAMA_BASE_URL": settings.ollama_base_url,
            "OLLAMA_MODEL": settings.ollama_model,
        }
    elif provider == "openai":
        required = {
            "OPENAI_BASE_URL": settings.openai_base_url,
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENAI_MODEL": settings.openai_model,
        }
    elif provider == "deepseek":
        required = {
            "DEEPSEEK_BASE_URL": settings.deepseek_base_url,
            "DEEPSEEK_API_KEY": settings.deepseek_api_key,
            "DEEPSEEK_MODEL": settings.deepseek_model,
        }
    else:
        raise ValueError(
            "AI_PROVIDER must be a real provider: 'ollama', 'openai', or 'deepseek'"
        )
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise ValueError(
            f"Creator model provider '{provider}' is missing: " + ", ".join(missing)
        )
