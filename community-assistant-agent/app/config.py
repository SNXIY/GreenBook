from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",
        case_sensitive=False,
        extra="ignore",
    )

    api_host: str = "127.0.0.1"
    api_port: int = 8094
    dev_reload: bool = True
    database_url: str = (
        "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
    )
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    java_base_url: str = "http://127.0.0.1:8080"
    creator_base_url: str = "http://127.0.0.1:8092"
    moderation_base_url: str = Field(
        default="http://127.0.0.1:8088", alias="MODERATION_AGENT_BASE_URL"
    )
    moderation_auth_secret: str = Field(
        default="", alias="MODERATION_AGENT_AUTH_SECRET"
    )
    service_shared_secret: str = ""
    identity_jwks_url: str = "http://127.0.0.1:8080/.well-known/jwks.json"
    identity_issuer: str = "http://127.0.0.1:8080"
    identity_audience: str = "community-assistant-agent"
    allow_insecure_http: bool = True
    worker_poll_seconds: float = 0.8
    run_concurrency: int = 4
    scheduler_concurrency: int = 2
    tool_job_concurrency: int = 2
    max_concurrent_runs_per_user: int = 1
    max_concurrent_read_runs_per_user: int = 3
    lease_seconds: int = 90
    tool_job_lease_seconds: int = 90
    tool_job_max_attempts: int = 4
    tool_job_poll_seconds: float = 0.8
    creator_timeout_seconds: int = 240
    creator_dependency_poll_seconds: float = 30.0
    event_stream_poll_seconds: float = 1.0
    redis_url: str = "redis://:mindflow@127.0.0.1:26379/0"
    distributed_limits_enabled: bool = True
    distributed_limits_required: bool = False
    model_requests_per_minute: int = 60
    user_model_requests_per_minute: int = 12
    process_role: str = "all"
    max_model_calls: int = 6
    max_tool_calls: int = 30
    max_replans: int = 2
    max_run_attempts: int = 3
    run_timeout_seconds: int = 600
    publication_min_lead_seconds: int = 15
    publication_max_schedule_days: int = 6
    deletion_batch_chunk_size: int = 20
    conversation_context_max_chars: int = 16_000
    tool_context_max_chars: int = 24_000
    post_context_max_chars: int = 32_000
    memory_context_max_chars: int = 6_000
    episodic_memory_enabled: bool = True
    episodic_memory_retention_days: int = 180
    episodic_memory_recall_limit: int = 5
    semantic_memory_enabled: bool = True
    semantic_memory_required: bool = False
    memory_qdrant_url: str = "http://127.0.0.1:26333"
    memory_qdrant_api_key: str = ""
    memory_qdrant_collection: str = "greenbook_assistant_memory"
    memory_embedding_provider: str = "hashing"
    memory_embedding_base_url: str = "https://api.openai.com/v1"
    memory_embedding_api_key: str = ""
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_dimensions: int = 256
    memory_embedding_timeout_seconds: float = 30.0
    memory_semantic_score_threshold: float = 0.18
    approval_ttl_minutes: int = 30
    mcp_servers_json: str = "[]"
    mcp_max_result_chars: int = 24_000
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    @model_validator(mode="after")
    def validate_production_dependencies(self) -> "Settings":
        if not self.deepseek_api_key.strip():
            raise ValueError(
                "DEEPSEEK_API_KEY is required: Community Assistant has no mock mode"
            )
        if not self.service_shared_secret.strip():
            raise ValueError("ASSISTANT_SERVICE_SHARED_SECRET is required")
        if self.process_role not in {
            "all",
            "api",
            "run-worker",
            "scheduler-worker",
            "tool-worker",
        }:
            raise ValueError(
                "ASSISTANT_PROCESS_ROLE must be all, api, run-worker, "
                "scheduler-worker or tool-worker"
            )
        if min(
            self.run_concurrency,
            self.scheduler_concurrency,
            self.tool_job_concurrency,
            self.max_concurrent_runs_per_user,
            self.max_concurrent_read_runs_per_user,
        ) < 1:
            raise ValueError("Assistant concurrency limits must be positive")
        if min(self.tool_job_lease_seconds, self.tool_job_max_attempts) < 1:
            raise ValueError("Assistant tool queue limits must be positive")
        if min(
            self.model_requests_per_minute,
            self.user_model_requests_per_minute,
        ) < 1:
            raise ValueError("Assistant distributed rate limits must be positive")
        if min(
            self.publication_min_lead_seconds,
            self.publication_max_schedule_days,
            self.deletion_batch_chunk_size,
        ) < 1:
            raise ValueError("Assistant side-effect policy limits must be positive")
        if self.episodic_memory_retention_days < 1:
            raise ValueError("ASSISTANT_EPISODIC_MEMORY_RETENTION_DAYS must be positive")
        if not 1 <= self.episodic_memory_recall_limit <= 20:
            raise ValueError(
                "ASSISTANT_EPISODIC_MEMORY_RECALL_LIMIT must be between 1 and 20"
            )
        if self.memory_context_max_chars < 1_000:
            raise ValueError("ASSISTANT_MEMORY_CONTEXT_MAX_CHARS must be at least 1000")
        if self.memory_embedding_provider not in {"hashing", "openai"}:
            raise ValueError(
                "ASSISTANT_MEMORY_EMBEDDING_PROVIDER must be hashing or openai"
            )
        if self.memory_embedding_dimensions < 32:
            raise ValueError(
                "ASSISTANT_MEMORY_EMBEDDING_DIMENSIONS must be at least 32"
            )
        if (
            self.semantic_memory_enabled
            and self.memory_embedding_provider == "openai"
            and not self.memory_embedding_api_key.strip()
        ):
            raise ValueError(
                "ASSISTANT_MEMORY_EMBEDDING_API_KEY is required for openai embeddings"
            )
        if not 0.0 <= self.memory_semantic_score_threshold <= 1.0:
            raise ValueError(
                "ASSISTANT_MEMORY_SEMANTIC_SCORE_THRESHOLD must be between 0 and 1"
            )
        if (
            not self.allow_insecure_http
            and self.identity_jwks_url.lower().startswith("http://")
        ):
            raise ValueError("Insecure JWKS URL is disabled")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
