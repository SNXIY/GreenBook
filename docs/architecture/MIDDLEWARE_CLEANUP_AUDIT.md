> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Middleware Cleanup Audit

## Redis

Docker 配置仍提供 shared Redis：`docker-compose.yml` 的 `creator-redis` 和 `infra/docker-compose.dev.yml` 的 `redis`。环境变量包括 `REDIS_HOST`、`REDIS_PORT`、`REDIS_DATABASE`、`ASSISTANT_REDIS_URL`。

扫描结果：

- Creator 使用 Redis/queue/checkpoint 相关能力，不能按 Assistant Legacy 标识直接删除。
- Assistant/E2E 配置仍会连接 Redis，`scripts/run_p0_e2e.py` 明确设置 Redis DB 14。
- 未发现可证明为空且只属于 `assistant_runs` 的统一 key namespace。

结论：KEEP。后续应先按应用、数据库号和 key prefix 做运行时 inventory，再决定删除具体 key；本阶段不 flush Redis、不删除 volume、不修改 service。

## Kafka

`docker-compose.yml` 仍定义 `zhiguang-kafka`/Redpanda 和数据 volume；`apps/assistant_worker/pyproject.toml` 声明 Kafka consumer 依赖，`packages/contracts/greenbook_contracts/events.py` 定义 Kafka business event contract，worker 当前虽有 placeholder，但基础设施仍属于集成边界。

结论：KEEP / VERIFY。没有可靠证据证明 topic 无 producer/consumer，因此不删除 topic、service 或 volume。后续需要从部署配置、生产 ACL、consumer group 和 producer 日志生成 topic inventory。

## Qdrant

Docker 和 `.env.example` 配置 Qdrant；Assistant memory 使用 `ASSISTANT_MEMORY_QDRANT_COLLECTION=greenbook_assistant_memory`，Creator retrieval 代码使用 Qdrant channel 和可配置 collection。

结论：KEEP。`greenbook_assistant_memory` 和 Creator collection 不能视为 Legacy Runtime 资源；删除前必须分别确认数据所有权、重建方式和线上读写情况。本阶段不删除 collection 或 volume。

## Docker services

已扫描：

- `zhiguang-kafka`
- shared PostgreSQL services
- `creator-redis` / `redis`
- `greenbook-qdrant` / `qdrant`
- Assistant、Creator、backend/frontend 相关 service

`community-assistant-agent` 仍被 `.github/workflows/verify.yml`、`scripts/verify-all.ps1`、`scripts/smoke-test.ps1`、`scripts/runtime-report.ps1` 和 E2E harness 引用，因此不能删除其 service/config。Creator 也有独立的运行域和 Qdrant/Redis 依赖。

结论：没有满足“无 CI、无 Docker、无 script、无 runtime 引用”的可安全删除 service。

## 环境变量与 CI

`ENABLE_LEGACY_AGENT_FALLBACK` 仍控制 Legacy fallback，默认兼容值为 `true`，属于 MIGRATE，不是无用变量。

`ASSISTANT_IDENTITY_AUDIENCE=greenbook-assistant-runtime` 和相关启动脚本参与认证/服务启动，属于 ACTIVE Runtime 配置。

## 分类总结

### KEEP

- Runtime event/checkpoint/lease persistence
- PostgreSQL、Redis、Kafka、Qdrant 基础设施
- ACTIVE application service 配置
- Creator 独立资源

### MIGRATE

- Legacy Agent service/config
- `ASSISTANT_IDENTITY_AUDIENCE`
- Legacy fallback flag
- Redis key namespaces and Kafka topics, after runtime inventory

### DELETE CANDIDATE

当前没有已证明可删除的中间件资源。任何 cache/volume/topic/collection 删除都需要外部运行态证据，不由静态仓库扫描推断。
