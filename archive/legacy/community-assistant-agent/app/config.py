import json
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 用来配置 Settings 怎么从环境变量读配置。
    model_config = SettingsConfigDict(
        env_prefix="ASSISTANT_",  # 环境变量要带这个前缀
        case_sensitive=False,     # 大小写不敏感
        extra="ignore",           # 多出来的环境变量忽略，不报错
    )

    api_host: str = "127.0.0.1"
    api_port: int = 8094
    dev_reload: bool = True # 开发模式下，自动重新加载配置
    database_url: str = (   # postgresql数据库连接 URL
        "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
    )
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL")
    model_fast: str = "deepseek-v4-flash"
    model_strong: str = "deepseek-v4-pro"
    model_judge: str = "deepseek-v4-pro"
    model_fast_thinking: bool = False
    model_strong_thinking: bool = True
    model_judge_thinking: bool = False
    model_strong_reasoning_effort: str = "high"
    model_fast_timeout_seconds: float = 25.0
    model_strong_timeout_seconds: float = 90.0
    model_judge_timeout_seconds: float = 45.0
    model_failure_threshold: int = 2
    model_cooldown_seconds: float = 30.0
    model_route_overrides_json: str = "{}"
    # Java服务地址配置
    java_base_url: str = "http://127.0.0.1:8080"
    # 创作者服务地址配置
    creator_base_url: str = "http://127.0.0.1:8093"
    service_shared_secret: str = ""
    # 用户登录 Java → 拿到 JWT
    # 前端带 JWT 调助手
    # 助手访问 identity_jwks_url 取公钥 → 验签 → 确认是谁
    identity_jwks_url: str = "http://127.0.0.1:8080/.well-known/jwks.json"
    identity_issuer: str = "http://127.0.0.1:8080"
    # 身份认证服务受众
    identity_audience: str = "community-assistant-agent"
    allow_insecure_http: bool = True # 允许不安全的 HTTP 请求
    worker_poll_seconds: float = 0.8 # 轮询间隔时间
    run_concurrency: int = 4 # 并发执行任务数量
    scheduler_concurrency: int = 2 # 调度器并发执行任务数量
    tool_job_concurrency: int = 2 # 工具任务并发执行任务数量
    max_concurrent_runs_per_user: int = 1 # 每个用户最大并发运行任务数量
    max_concurrent_read_runs_per_user: int = 3 # 每个用户最大并发读取任务数量
    lease_seconds: int = 90 # 租约时间
    tool_job_lease_seconds: int = 90 # 工具任务租约时间
    tool_job_max_attempts: int = 4 # 工具任务最大重试次数
    tool_job_poll_seconds: float = 0.8 # 工具任务轮询间隔时间
    creator_timeout_seconds: int = 240 # 创作者服务超时时间
    creator_dependency_poll_seconds: float = 30.0 # 创作者服务依赖轮询间隔时间
    event_stream_poll_seconds: float = 1.0 # 事件流轮询间隔时间

    # Tool Runtime HTTP transport (Phase 5 Step 2)
    # Tests and local default: do not inherit HTTP_PROXY from the process env.
    tool_http_trust_env: bool = False
    tool_http_proxy: str | None = None
    tool_http_connect_timeout_seconds: float = 5.0
    tool_http_read_timeout_seconds: float = 30.0
    tool_http_pool_timeout_seconds: float = 5.0
    tool_http_verify_tls: bool = True

    # Redis 配置
    redis_url: str = "redis://:mindflow@127.0.0.1:26379/0"
    distributed_limits_enabled: bool = True # 分布式限制启用
    distributed_limits_required: bool = False # 分布式限制必选
    model_requests_per_minute: int = 60 # 模型请求每分钟限制
    user_model_requests_per_minute: int = 12 # 用户模型请求每分钟限制

    # 处理角色配置
    process_role: str = "all"
    max_model_calls: int = 6 # 模型调用最大次数
    max_tool_calls: int = 30 # 工具调用最大次数
    max_replans: int = 2 # 最大重规划次数
    max_run_attempts: int = 3 # 最大运行尝试次数
    run_timeout_seconds: int = 600 # 运行超时时间
    publication_min_lead_seconds: int = 15 # 发布最小提前时间
    publication_max_schedule_days: int = 6 # 发布最大调度天数
    deletion_batch_chunk_size: int = 20 # 删除批量分块大小
    conversation_context_max_chars: int = 16_000 # 对话上下文最大字符数
    tool_context_max_chars: int = 24_000 # 工具上下文最大字符数
    post_context_max_chars: int = 32_000 # 帖子上下文最大字符数
    memory_context_max_chars: int = 6_000 # 记忆上下文最大字符数
    episodic_memory_enabled: bool = True # 情节记忆开关
    episodic_memory_retention_days: int = 180 # 情节记忆保留天数
    episodic_memory_recall_limit: int = 5 # 情节记忆召回条数上限
    semantic_memory_enabled: bool = True # 语义记忆开关
    semantic_memory_required: bool = False # 语义记忆是否强依赖（不可用时是否失败）

    # 记忆配置 记忆服务地址配置
    memory_qdrant_url: str = "http://127.0.0.1:26333" # 记忆服务地址
    memory_qdrant_api_key: str = "" # 记忆服务API密钥
    memory_qdrant_collection: str = "greenbook_assistant_memory" # 记忆服务集合名称
    memory_embedding_provider: str = "hashing" # 记忆服务嵌入提供者
    memory_embedding_base_url: str = "https://api.openai.com/v1" # 记忆服务嵌入基础URL
    memory_embedding_api_key: str = "" # 记忆服务嵌入API密钥
    memory_embedding_model: str = "text-embedding-3-small" # 记忆服务嵌入模型
    memory_embedding_dimensions: int = 256 # 记忆服务嵌入维度
    memory_embedding_timeout_seconds: float = 30.0 # 记忆服务嵌入超时时间
    memory_semantic_score_threshold: float = 0.18 # 记忆服务语义分数阈值
    approval_ttl_minutes: int = 30 # 批准过期时间
    mcp_servers_json: str = "[]" # MCP服务器JSON配置
    mcp_max_result_chars: int = 24_000 # MCP最大结果字符数
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173" # 跨域请求允许源

    #  就是校验函数：配置加载完后自动执行，检查关键项是否合法；不通过就抛错，服务起不来。
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
            self.model_fast_timeout_seconds,
            self.model_strong_timeout_seconds,
            self.model_judge_timeout_seconds,
            self.model_failure_threshold,
            self.model_cooldown_seconds,
        ) <= 0:
            raise ValueError("Assistant model routing limits must be positive")
        if self.model_strong_reasoning_effort not in {"high", "max"}:
            raise ValueError(
                "ASSISTANT_MODEL_STRONG_REASONING_EFFORT must be high or max"
            )
        _ = self.model_route_overrides
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


    # 跨域请求允许源
    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def model_route_overrides(self) -> dict[str, str]:
        try:
            value = json.loads(self.model_route_overrides_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                "ASSISTANT_MODEL_ROUTE_OVERRIDES_JSON must be valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError("ASSISTANT_MODEL_ROUTE_OVERRIDES_JSON must be an object")
        allowed_operations = {
            "adaptive.route",
            "intent.understand",
            "planner.plan",
            "progress.assess",
            "verifier.verify",
            "answer.compose",
            "summary.post",
            "structured.repair",
        }
        unknown = set(value) - allowed_operations
        invalid_tiers = {item for item in value.values() if item not in {"fast", "strong", "judge"}}
        if unknown or invalid_tiers:
            raise ValueError(
                "Assistant model route overrides contain unknown operations or tiers"
            )
        return {str(key): str(item) for key, item in value.items()}

# 缓存配置，避免重复加载配置，提高性能
@lru_cache
def get_settings() -> Settings:
    # 返回配置实例，缓存起来，下次直接从缓存取
    return Settings()
