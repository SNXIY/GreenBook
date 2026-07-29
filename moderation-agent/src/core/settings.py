from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote_plus

from dotenv import find_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema.models import (
    AllModelEnum,
    AnthropicModelName,
    AWSModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    GoogleModelName,
    GroqModelName,
    OllamaModelName,
    OpenAICompatibleName,
    OpenAIModelName,
    OpenRouterModelName,
    Provider,
    VertexAIModelName,
)

if TYPE_CHECKING:
    from agents.moderation.routes import ModerationThresholds
    from moderation.schemas import (
        AgenticPolicyRAGConfig,
        EvidenceReviewerConfig,
        ToolCallingConfig,
    )
    from moderation.services.preflight import PreflightConfig

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent if CORE_DIR.parent.name == "src" else Path.cwd()
LOCAL_DATABASE_DIR = PROJECT_ROOT / "data" / "databases"
DEFAULT_CHECKPOINT_DB_PATH = LOCAL_DATABASE_DIR / "checkpoints.db"
DEFAULT_MODERATION_DB_PATH = LOCAL_DATABASE_DIR / "moderation.db"

_PROVIDER_DEFAULT_MODELS: dict[Provider, AllModelEnum] = {
    Provider.OPENAI: OpenAIModelName.GPT_5_NANO,
    Provider.OPENAI_COMPATIBLE: OpenAICompatibleName.OPENAI_COMPATIBLE,
    Provider.DEEPSEEK: DeepseekModelName.DEEPSEEK_V4_FLASH,
    Provider.ANTHROPIC: AnthropicModelName.HAIKU_45,
    Provider.GOOGLE: GoogleModelName.GEMINI_35_FLASH,
    Provider.VERTEXAI: VertexAIModelName.GEMINI_35_FLASH,
    Provider.GROQ: GroqModelName.LLAMA_31_8B,
    Provider.AWS: AWSModelName.BEDROCK_HAIKU,
    Provider.OLLAMA: OllamaModelName.OLLAMA_GENERIC,
    Provider.OPENROUTER: OpenRouterModelName.GEMINI_35_FLASH,
    Provider.AZURE_OPENAI: AzureOpenAIModelName.AZURE_GPT_5_MINI,
}


class DatabaseType(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    def to_logging_level(self) -> int:
        """Convert to Python logging level constant."""
        import logging

        mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
        }
        return mapping[self]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_default=False,
    )
    MODE: str | None = None

    HOST: str = "0.0.0.0"
    PORT: int = 8088
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    LOG_LEVEL: LogLevel = LogLevel.WARNING

    AUTH_SECRET: SecretStr | None = None

    OPENAI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GOOGLE_APPLICATION_CREDENTIALS: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    USE_AWS_BEDROCK: bool = False
    OLLAMA_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    OPENROUTER_API_KEY: str | None = None

    # If DEFAULT_MODEL is None, it will be set in model_post_init
    DEFAULT_MODEL: AllModelEnum | None = None  # type: ignore[assignment]

    # OpenAI-compatible endpoint configuration.
    COMPATIBLE_MODEL: str | None = None
    COMPATIBLE_API_KEY: SecretStr | None = None
    COMPATIBLE_BASE_URL: str | None = None

    # Database Configuration
    DATABASE_TYPE: DatabaseType = (
        DatabaseType.SQLITE
    )  # Options: DatabaseType.SQLITE or DatabaseType.POSTGRES
    SQLITE_DB_PATH: str = str(DEFAULT_CHECKPOINT_DB_PATH)

    # PostgreSQL Configuration
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_APPLICATION_NAME: str = "content-moderation-platform"
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = Field(default=1, ge=1, le=50)
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = Field(default=5, ge=1, le=50)

    # Content moderation domain database. When omitted, PostgreSQL settings are reused
    # if DATABASE_TYPE=postgres; otherwise a local async SQLite database is used.
    MODERATION_DATABASE_URL: str | None = None
    MODERATION_AUTO_CREATE_SCHEMA: bool = True
    MODERATION_SEED_DEFAULT_POLICIES: bool = True
    MODERATION_LOW_RISK_FAST_PATH_ENABLED: bool = True
    MODERATION_ADAPTIVE_CASCADE_ENABLED: bool = True
    MODERATION_POLICY_ENGINE_ENABLED: bool = True
    # Pre-graph cascade: L0 deterministic rules (default on); optional L1 Moderations API.
    MODERATION_L0_ENABLED: bool = True
    MODERATION_L1_ENABLED: bool = False
    MODERATION_L1_BASE_URL: str = "https://api.openai.com/v1"
    MODERATION_L1_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0.0, le=30.0)
    MODERATION_L1_ENFORCE_SCORE_MIN: float = Field(default=0.90, ge=0.0, le=1.0)
    MODERATION_L1_CLEAR_SAFE_SCORE_MAX: float = Field(default=0.05, ge=0.0, le=1.0)
    MODERATION_PASS_SCORE_MAX: float = Field(default=0.20, ge=0.0, le=1.0)
    MODERATION_REJECT_SCORE_MIN: float = Field(default=0.80, ge=0.0, le=1.0)
    MODERATION_AUTO_PASS_CONFIDENCE_MIN: float = Field(default=0.70, ge=0.0, le=1.0)
    MODERATION_AUTO_REJECT_CONFIDENCE_MIN: float = Field(default=0.80, ge=0.0, le=1.0)
    MODERATION_AUTO_LIMIT_CONFIDENCE_MIN: float = Field(default=0.80, ge=0.0, le=1.0)
    MODERATION_ADVERSARIAL_SCORE_MIN: float = Field(default=0.40, ge=0.0, le=1.0)
    MODERATION_ADVERSARIAL_SCORE_MAX: float = Field(default=0.85, ge=0.0, le=1.0)
    MODERATION_ADVERSARIAL_CONFIDENCE_MIN: float = Field(default=0.80, ge=0.0, le=1.0)
    MODERATION_ADVERSARIAL_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0, le=120.0)
    MODERATION_TOOL_CALLING_ENABLED: bool = True
    MODERATION_TOOL_MAX_ROUNDS: int = Field(default=2, ge=1, le=10)
    MODERATION_TOOL_MAX_TOTAL_CALLS: int = Field(default=4, ge=1, le=32)
    MODERATION_TOOL_MAX_PARALLEL_CALLS: int = Field(default=3, ge=1, le=8)
    MODERATION_TOOL_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0.0, le=60.0)
    MODERATION_TOOL_MAX_RESULT_CHARS: int = Field(default=4000, ge=512, le=20_000)
    MODERATION_TOOL_MAX_RETRIES: int = Field(default=1, ge=0, le=3)
    MODERATION_TOOL_AGENT_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0, le=120.0)
    MODERATION_POLICY_RAG_ENABLED: bool = False
    MODERATION_POLICY_RAG_MAX_QUERIES_PER_ROUND: int = Field(default=2, ge=1, le=3)
    MODERATION_POLICY_RAG_MAX_RETRIEVAL_ROUNDS: int = Field(default=1, ge=1, le=5)
    MODERATION_POLICY_RAG_MAX_TOTAL_POLICIES: int = Field(default=20, ge=1, le=100)
    MODERATION_POLICY_RAG_VECTOR_TOP_K: int = Field(default=5, ge=1, le=20)
    MODERATION_POLICY_RAG_KEYWORD_TOP_K: int = Field(default=5, ge=1, le=20)
    MODERATION_POLICY_RAG_FINAL_TOP_K: int = Field(default=8, ge=1, le=20)
    MODERATION_POLICY_RAG_VECTOR_WEIGHT: float = Field(default=0.65, ge=0.0, le=1.0)
    MODERATION_POLICY_RAG_KEYWORD_WEIGHT: float = Field(default=0.35, ge=0.0, le=1.0)
    MODERATION_POLICY_RAG_MIN_VECTOR_SCORE: float = Field(default=0.45, ge=0.0, le=1.0)
    MODERATION_POLICY_RAG_MIN_COMBINED_SCORE: float = Field(default=0.50, ge=0.0, le=1.0)
    MODERATION_POLICY_RAG_GRADER_MIN_CONFIDENCE: float = Field(default=0.65, ge=0.0, le=1.0)
    MODERATION_POLICY_RAG_ALLOW_PARTIAL: bool = True
    MODERATION_POLICY_RAG_FALLBACK_TO_DATABASE: bool = True
    MODERATION_POLICY_RAG_AGENT_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0.0,
        le=120.0,
    )
    MODERATION_EVIDENCE_REVIEWER_ENABLED: bool = False
    MODERATION_REVIEWER_MAX_ITERATIONS: int = Field(default=1, ge=1, le=5)
    MODERATION_REVIEWER_MAX_TOOL_REVISIONS: int = Field(default=0, ge=0, le=3)
    MODERATION_REVIEWER_MAX_POLICY_REVISIONS: int = Field(default=0, ge=0, le=3)
    MODERATION_REVIEWER_MAX_JUDGMENT_REVISIONS: int = Field(default=1, ge=0, le=5)
    MODERATION_REVIEWER_MIN_CONFIDENCE: float = Field(default=0.65, ge=0.0, le=1.0)
    MODERATION_REVIEWER_HUMAN_ON_BUDGET: bool = True
    MODERATION_REVIEWER_HUMAN_ON_ERROR: bool = True
    MODERATION_REVIEWER_ALLOW_FAST_PATH_ON_ERROR: bool = False
    MODERATION_REVIEWER_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0, le=120.0)
    MODERATION_GRAPH_RECURSION_LIMIT: int = Field(default=64, ge=25, le=200)
    MODERATION_ASYNC_ENABLED: bool = True
    MODERATION_EMBEDDED_WORKER_ENABLED: bool = True
    MODERATION_WORKER_POLL_INTERVAL_MS: int = Field(default=500, ge=50, le=60_000)
    MODERATION_WORKER_LEASE_SECONDS: float = Field(default=300.0, gt=0.0, le=3600.0)
    MODERATION_WORKER_CONCURRENCY: int = Field(default=2, ge=1, le=32)
    MODERATION_WORKER_ID: str | None = None
    MODERATION_CALLBACK_CONCURRENCY: int = Field(default=2, ge=1, le=16)
    MODERATION_CALLBACK_POLL_SECONDS: float = Field(default=0.5, gt=0.0, le=60.0)
    MODERATION_CALLBACK_LEASE_SECONDS: float = Field(default=30.0, gt=1.0, le=600.0)
    MODERATION_CALLBACK_MAX_ATTEMPTS: int = Field(default=8, ge=1, le=100)
    MODERATION_CALLBACK_RETRY_BASE_SECONDS: float = Field(
        default=2.0, ge=0.1, le=300.0
    )
    MODERATION_CALLBACK_RETRY_MAX_SECONDS: float = Field(
        default=300.0, ge=1.0, le=86_400.0
    )

    # Optional queue and vector infrastructure. Database queries remain the source of truth.
    REDIS_URL: str | None = None
    MODERATION_REDIS_QUEUE_KEY: str = "moderation:review:pending"
    QDRANT_URL: str | None = None
    QDRANT_API_KEY: SecretStr | None = None
    QDRANT_POLICY_COLLECTION: str = "moderation_policies"
    QDRANT_CASE_COLLECTION: str = "moderation_review_cases"
    MODERATION_VECTOR_SIZE: int = 256

    # Read-only source used by moderation context tools.
    COMMUNITY_PROVIDER: Literal["java"] = "java"
    JAVA_COMMUNITY_BASE_URL: str | None = None
    JAVA_COMMUNITY_AUTH_TOKEN: SecretStr | None = None
    COMMUNITY_HTTP_TIMEOUT: float = 10.0

    # Azure OpenAI Settings
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"

    def model_post_init(self, _context: Any) -> None:
        provider_configuration = {
            Provider.OPENAI: self.OPENAI_API_KEY,
            Provider.OPENAI_COMPATIBLE: self.COMPATIBLE_BASE_URL and self.COMPATIBLE_MODEL,
            Provider.DEEPSEEK: self.DEEPSEEK_API_KEY,
            Provider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            Provider.GOOGLE: self.GOOGLE_API_KEY,
            Provider.VERTEXAI: self.GOOGLE_APPLICATION_CREDENTIALS,
            Provider.GROQ: self.GROQ_API_KEY,
            Provider.AWS: self.USE_AWS_BEDROCK,
            Provider.OLLAMA: self.OLLAMA_MODEL,
            Provider.AZURE_OPENAI: self.AZURE_OPENAI_API_KEY,
            Provider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        active_providers = [provider for provider, value in provider_configuration.items() if value]
        if not active_providers:
            raise ValueError("At least one LLM API key must be provided.")

        if self.DEFAULT_MODEL is None:
            self.DEFAULT_MODEL = _PROVIDER_DEFAULT_MODELS[active_providers[0]]

        if Provider.AZURE_OPENAI in active_providers and not self.AZURE_OPENAI_ENDPOINT:
            raise ValueError("AZURE_OPENAI_ENDPOINT must be set")
        if self.COMMUNITY_PROVIDER != "java" or not self.JAVA_COMMUNITY_BASE_URL:
            raise ValueError(
                "COMMUNITY_PROVIDER=java and JAVA_COMMUNITY_BASE_URL are required"
            )

    def is_dev(self) -> bool:
        return self.MODE == "dev"

    def moderation_database_url(self) -> str:
        if self.MODERATION_DATABASE_URL:
            return self.MODERATION_DATABASE_URL
        if self.DATABASE_TYPE == DatabaseType.POSTGRES:
            user_setting = self.POSTGRES_USER
            password_setting = self.POSTGRES_PASSWORD
            host = self.POSTGRES_HOST
            port = self.POSTGRES_PORT
            database = self.POSTGRES_DB
            if not all((user_setting, password_setting, host, port, database)):
                raise ValueError("PostgreSQL moderation storage requires all POSTGRES_* settings")
            assert user_setting is not None
            assert password_setting is not None
            password = quote_plus(password_setting.get_secret_value())
            user = quote_plus(user_setting)
            return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"
        return f"sqlite+aiosqlite:///{DEFAULT_MODERATION_DB_PATH.as_posix()}"

    def moderation_tool_calling_config(self) -> "ToolCallingConfig":
        from moderation.schemas import ToolCallingConfig

        return ToolCallingConfig(
            enabled=self.MODERATION_TOOL_CALLING_ENABLED,
            max_rounds=self.MODERATION_TOOL_MAX_ROUNDS,
            max_total_calls=self.MODERATION_TOOL_MAX_TOTAL_CALLS,
            max_parallel_calls=self.MODERATION_TOOL_MAX_PARALLEL_CALLS,
            tool_timeout_seconds=self.MODERATION_TOOL_TIMEOUT_SECONDS,
            max_result_chars=self.MODERATION_TOOL_MAX_RESULT_CHARS,
            max_retries=self.MODERATION_TOOL_MAX_RETRIES,
            agent_timeout_seconds=self.MODERATION_TOOL_AGENT_TIMEOUT_SECONDS,
        )

    def moderation_thresholds(self) -> "ModerationThresholds":
        from agents.moderation.routes import ModerationThresholds

        return ModerationThresholds(
            pass_score_max=self.MODERATION_PASS_SCORE_MAX,
            reject_score_min=self.MODERATION_REJECT_SCORE_MIN,
            auto_pass_confidence_min=self.MODERATION_AUTO_PASS_CONFIDENCE_MIN,
            auto_reject_confidence_min=self.MODERATION_AUTO_REJECT_CONFIDENCE_MIN,
            auto_limit_confidence_min=self.MODERATION_AUTO_LIMIT_CONFIDENCE_MIN,
            adversarial_score_min=self.MODERATION_ADVERSARIAL_SCORE_MIN,
            adversarial_score_max=self.MODERATION_ADVERSARIAL_SCORE_MAX,
            adversarial_confidence_min=self.MODERATION_ADVERSARIAL_CONFIDENCE_MIN,
        )

    def moderation_preflight_config(self) -> "PreflightConfig":
        from moderation.services.preflight import PreflightConfig

        api_key = self.OPENAI_API_KEY.get_secret_value() if self.OPENAI_API_KEY else None
        return PreflightConfig(
            l0_enabled=self.MODERATION_L0_ENABLED,
            l1_enabled=self.MODERATION_L1_ENABLED,
            l1_api_key=api_key,
            l1_base_url=self.MODERATION_L1_BASE_URL,
            l1_timeout_seconds=self.MODERATION_L1_TIMEOUT_SECONDS,
            l1_enforce_score_min=self.MODERATION_L1_ENFORCE_SCORE_MIN,
            l1_clear_safe_score_max=self.MODERATION_L1_CLEAR_SAFE_SCORE_MAX,
        )

    def agentic_policy_rag_config(self) -> "AgenticPolicyRAGConfig":
        from moderation.schemas import AgenticPolicyRAGConfig

        return AgenticPolicyRAGConfig(
            enabled=self.MODERATION_POLICY_RAG_ENABLED,
            max_queries_per_round=self.MODERATION_POLICY_RAG_MAX_QUERIES_PER_ROUND,
            max_retrieval_rounds=self.MODERATION_POLICY_RAG_MAX_RETRIEVAL_ROUNDS,
            max_total_retrieved_policies=self.MODERATION_POLICY_RAG_MAX_TOTAL_POLICIES,
            vector_top_k=self.MODERATION_POLICY_RAG_VECTOR_TOP_K,
            keyword_top_k=self.MODERATION_POLICY_RAG_KEYWORD_TOP_K,
            final_top_k=self.MODERATION_POLICY_RAG_FINAL_TOP_K,
            vector_weight=self.MODERATION_POLICY_RAG_VECTOR_WEIGHT,
            keyword_weight=self.MODERATION_POLICY_RAG_KEYWORD_WEIGHT,
            min_vector_score=self.MODERATION_POLICY_RAG_MIN_VECTOR_SCORE,
            min_combined_score=self.MODERATION_POLICY_RAG_MIN_COMBINED_SCORE,
            grader_min_confidence=self.MODERATION_POLICY_RAG_GRADER_MIN_CONFIDENCE,
            allow_partial_policy_continue=self.MODERATION_POLICY_RAG_ALLOW_PARTIAL,
            fallback_to_database=self.MODERATION_POLICY_RAG_FALLBACK_TO_DATABASE,
            agent_timeout_seconds=self.MODERATION_POLICY_RAG_AGENT_TIMEOUT_SECONDS,
        )

    def evidence_reviewer_config(self) -> "EvidenceReviewerConfig":
        from moderation.schemas import EvidenceReviewerConfig

        return EvidenceReviewerConfig(
            enabled=self.MODERATION_EVIDENCE_REVIEWER_ENABLED,
            max_iterations=self.MODERATION_REVIEWER_MAX_ITERATIONS,
            max_tool_revisions=self.MODERATION_REVIEWER_MAX_TOOL_REVISIONS,
            max_policy_revisions=self.MODERATION_REVIEWER_MAX_POLICY_REVISIONS,
            max_judgment_revisions=self.MODERATION_REVIEWER_MAX_JUDGMENT_REVISIONS,
            min_reviewer_confidence=self.MODERATION_REVIEWER_MIN_CONFIDENCE,
            human_review_on_budget_exceeded=self.MODERATION_REVIEWER_HUMAN_ON_BUDGET,
            human_review_on_reviewer_error=self.MODERATION_REVIEWER_HUMAN_ON_ERROR,
            allow_deterministic_fast_path_on_error=(
                self.MODERATION_REVIEWER_ALLOW_FAST_PATH_ON_ERROR
            ),
            agent_timeout_seconds=self.MODERATION_REVIEWER_TIMEOUT_SECONDS,
        )


settings = Settings()
