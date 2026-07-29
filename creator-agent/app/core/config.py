from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # A real model provider is mandatory; invalid or missing credentials fail startup.
    ai_provider: str = "deepseek"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 4096
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking_enabled: bool = False
    embedding_timeout_seconds: float = 30.0

    # Creator control plane.
    creator_database_url: str = "sqlite+aiosqlite:///./data/mindflow-creator.db"
    creator_database_echo: bool = False
    creator_database_pool_size: int = 5
    creator_database_max_overflow: int = 10
    creator_checkpoint_backend: str = "sqlite"
    creator_checkpoint_sqlite_path: str = "data/mindflow-checkpoints.db"
    creator_checkpoint_postgres_url: str = ""
    creator_checkpoint_auto_setup: bool = True
    creator_runtime_max_attempts: int = 3
    creator_run_lease_seconds: int = 120
    creator_retry_delay_seconds: int = 15
    creator_idempotency_ttl_seconds: int = 86400
    creator_max_runtime_events: int = 100
    creator_max_event_payload_bytes: int = 65536
    creator_max_supervisor_turns: int = 24
    creator_max_agent_dispatches: int = 24
    creator_max_model_calls: int = 24
    creator_max_output_tokens: int = 40000
    creator_max_replans: int = 4
    creator_max_writer_revisions: int = 2
    creator_model_analysis_model: str = ""
    creator_model_writer_model: str = ""
    creator_model_critic_model: str = ""
    creator_model_assist_model: str = ""
    creator_specialist_timeout_seconds: float = 90.0
    creator_model_timeout_seconds: float = 60.0

    # OpenTelemetry. Prompt and response bodies are intentionally never exported.
    creator_otel_enabled: bool = False
    creator_otel_service_name: str = "mindflow-creator"
    creator_otel_exporter_endpoint: str = ""
    creator_otel_exporter_headers: str = ""

    # HTTP API and local dispatcher.
    creator_api_execution_mode: str = "local"
    creator_api_create_schema: bool = True
    creator_api_tenant_id: str = "tenant-local"
    creator_api_worker_id: str = "creator-api"
    creator_api_worker_concurrency: int = 2
    creator_tenant_max_concurrent_runs: int = 4
    creator_user_max_concurrent_runs: int = 1
    creator_api_shutdown_grace_seconds: float = 10.0
    creator_api_default_page_size: int = 20
    creator_api_sse_poll_seconds: float = 1.0
    creator_api_sse_heartbeat_seconds: float = 15.0
    creator_api_sse_send_timeout_seconds: float = 15.0
    creator_workspace_catalog_path: str = "app/creator/api/workspace_catalog.zh-CN.json"
    creator_workspace_poll_interval_ms: int = 2_500

    # Local Basic Auth. Production deployments should use OIDC.
    creator_basic_username: str = "creator"
    creator_basic_password: str = ""
    creator_basic_creator_id: str = "creator-local"
    creator_basic_actor_id: str = "creator-local"
    creator_basic_display_name: str = "Demo Creator"
    creator_basic_roles: str = "CREATOR"
    creator_local_auto_login: bool = False

    # OIDC/JWKS identity.
    creator_identity_mode: str = "basic"
    creator_identity_issuer: str = ""
    creator_identity_audience: str = ""
    creator_identity_jwks_url: str = ""
    creator_identity_algorithms: str = "RS256"
    creator_identity_tenant_claim: str = "tenant_id"
    creator_identity_creator_claim: str = "creator_id"
    creator_identity_roles_claim: str = "roles"
    creator_identity_display_name_claim: str = "name"
    creator_identity_required_role: str = "CREATOR"
    creator_identity_leeway_seconds: float = 30.0
    creator_identity_jwks_cache_seconds: float = 300.0
    creator_identity_jwks_timeout_seconds: float = 5.0
    creator_identity_allow_insecure_http: bool = False

    # HMAC identity asserted by the Zhiguang Java gateway.
    creator_trusted_proxy_shared_secret: str = ""
    creator_trusted_proxy_allowed_service: str = "zhiguang-java-backend"
    creator_trusted_proxy_tenant_id: str = "zhiguang"
    creator_trusted_proxy_required_role: str = "CREATOR"
    creator_trusted_proxy_allowed_skew_seconds: int = 60
    creator_trusted_proxy_nonce_ttl_seconds: int = 120

    # Outbox runtime worker.
    creator_worker_id: str = "creator-runtime"
    creator_worker_concurrency: int = 4
    creator_worker_batch_size: int = 8
    creator_worker_poll_seconds: float = 0.5
    creator_worker_outbox_lease_seconds: int = 300
    creator_worker_heartbeat_seconds: float = 30.0
    creator_worker_max_attempts: int = 8
    creator_worker_retry_base_seconds: float = 2.0
    creator_worker_retry_max_seconds: float = 60.0
    creator_worker_shutdown_grace_seconds: float = 30.0
    creator_worker_health_file: str = "data/creator-worker-heartbeat"
    creator_worker_health_max_age_seconds: float = 90.0
    creator_worker_create_schema: bool = False

    # Creator Memory.
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_socket_timeout_seconds: float = 2.0
    creator_memory_enabled: bool = True
    creator_short_memory_enabled: bool = False
    creator_short_memory_required: bool = False
    creator_short_memory_ttl_seconds: int = 86400
    creator_long_memory_enabled: bool = True
    creator_semantic_memory_enabled: bool = False
    creator_semantic_memory_required: bool = False
    creator_semantic_memory_top_k: int = 6
    creator_memory_max_excerpt_chars: int = 1200
    creator_memory_qdrant_url: str = "http://127.0.0.1:6333"
    creator_memory_qdrant_api_key: str = ""
    creator_memory_qdrant_collection: str = "mindflow_creator_memory"
    creator_memory_embedding_provider: str = "hashing"
    creator_memory_embedding_dimensions: int = 256
    creator_memory_chunk_chars: int = 1200
    creator_memory_chunk_overlap_chars: int = 160
    creator_memory_score_threshold: float = 0.0

    # Agentic RAG.
    creator_retrieval_enabled: bool = True
    creator_retrieval_sql_enabled: bool = True
    creator_retrieval_qdrant_enabled: bool = False
    creator_retrieval_qdrant_required: bool = False
    creator_retrieval_qdrant_url: str = "http://127.0.0.1:6333"
    creator_retrieval_qdrant_api_key: str = ""
    creator_retrieval_qdrant_collection: str = "mindflow_creator_research"
    creator_retrieval_chunk_chars: int = 1200
    creator_retrieval_chunk_overlap_chars: int = 160
    creator_retrieval_score_threshold: float = 0.0
    creator_retrieval_max_queries_per_round: int = 3
    creator_retrieval_max_rounds: int = 2
    creator_retrieval_candidate_top_k: int = 20
    creator_retrieval_final_top_k: int = 6
    creator_retrieval_min_evidence: int = 2
    creator_retrieval_min_grade_score: float = 0.35
    creator_retrieval_source_timeout_seconds: float = 8.0
    creator_retrieval_max_excerpt_chars: int = 1200
    creator_retrieval_bm25_weight: float = 0.24
    creator_retrieval_vector_weight: float = 0.22
    creator_retrieval_business_weight: float = 0.16
    creator_retrieval_rrf_weight: float = 0.16
    creator_retrieval_freshness_weight: float = 0.07
    creator_retrieval_creator_affinity_weight: float = 0.05
    creator_retrieval_source_authority_weight: float = 0.10
    creator_retrieval_reranker_weight: float = 0.35
    creator_retrieval_reranker_provider: str = "heuristic"
    creator_retrieval_reranker_base_url: str = ""
    creator_retrieval_reranker_api_key: str = ""
    creator_retrieval_reranker_model: str = ""

    # Community provider and governed tools.
    creator_community_provider: str = "java"
    creator_community_timeout_seconds: float = 8.0
    creator_community_java_base_url: str = "http://127.0.0.1:8080"
    creator_community_java_shared_secret: str = ""
    creator_community_java_service_name: str = "mindflow-creator"
    creator_community_java_tenant_id: str = ""
    creator_tool_timeout_seconds: float = 10.0
    creator_tool_max_result_bytes: int = 262144

    # Publication handoff → Zhiguang Java ai-drafts API
    creator_publication_java_base_url: str = "http://127.0.0.1:8080"
    creator_publication_shared_secret: str = ""
    creator_publication_timeout_seconds: float = 30.0

    # MCP server.
    creator_mcp_transport: str = "stdio"
    creator_mcp_host: str = "127.0.0.1"
    creator_mcp_port: int = 8010
    creator_mcp_tenant_id: str = "tenant-local"
    creator_mcp_creator_id: str = "creator-local"
    creator_mcp_actor_id: str = "mindflow-mcp"
    creator_mcp_roles: str = "CREATOR"
    creator_mcp_allowed_tools: str = ""
    creator_mcp_bearer_token: str = ""
    creator_mcp_auth_issuer_url: str = "http://127.0.0.1:8010"
    creator_mcp_resource_server_url: str = "http://127.0.0.1:8010"
    creator_mcp_allowed_hosts: str = "127.0.0.1,127.0.0.1:*,localhost,localhost:*"
    creator_mcp_allowed_origins: str = ""

    # Versioned evaluation.
    creator_evaluation_dataset_path: str = (
        "app/creator/evaluation/datasets/smoke-v1.json"
    )
    creator_evaluation_observations_path: str = (
        "app/creator/evaluation/datasets/smoke-observations-v1.json"
    )
    creator_evaluation_output_path: str = "target/creator-evaluation-report.json"
    creator_evaluation_candidate_name: str = "mindflow-creator"
    creator_evaluation_candidate_version: str = "development"
    creator_evaluation_judge_provider: str = "deterministic"
    creator_evaluation_judge_base_url: str = ""
    creator_evaluation_judge_api_key: str = ""
    creator_evaluation_judge_model: str = ""
    creator_evaluation_judge_timeout_seconds: float = 30.0
    creator_evaluation_judge_max_context_chars: int = 24000
    creator_evaluation_judge_max_attempts: int = 2

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
