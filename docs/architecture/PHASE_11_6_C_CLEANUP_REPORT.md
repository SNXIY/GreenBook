# Phase 11.6-C Cleanup Report

## 1. 扫描结论

全仓扫描了 `assistant_runs`、`RunRepository`、`LegacyAgent`、`legacy`、`run_id`、`ENABLE_LEGACY`、旧服务名及 Redis/Kafka/Qdrant/Docker 配置。

结果：

- `assistant_runs` / `RunRepository`：MIGRATE，仍被 Assistant API 和 Legacy-only contract 使用。
- `LegacyAgentService` / `LegacyAgent`：MIGRATE，仍受 fallback 配置和测试/服务边界保护。
- `community-assistant-agent`：KEEP temporarily，仍被 CI、启动脚本、E2E、身份 audience 和集成文档引用。
- `run_id`：按域处理；Assistant execution contract MIGRATE，Creator/Java/trace/TaskIntent 同名字段 KEEP until separately migrated。
- `RunExecutionAdapter`：KEEP temporarily，直到所有旧 API、approval、SSE 和历史数据完成 execution reference 迁移。
- Redis/Kafka/Qdrant/Docker：KEEP，当前存在 ACTIVE 或集成引用。

详细数据库审计：[DATABASE_CLEANUP_AUDIT.md](D:/agent/green-book/docs/architecture/DATABASE_CLEANUP_AUDIT.md)

详细中间件审计：[MIDDLEWARE_CLEANUP_AUDIT.md](D:/agent/green-book/docs/architecture/MIDDLEWARE_CLEANUP_AUDIT.md)

## 2. 本次安全删除

删除的仅是本地生成物：

- 根目录 `.pytest_cache`
- Python 源码树中的 `__pycache__` 目录和 `*.pyc`
- `apps/creator-agent/.jbeval`
- `apps/creator-agent/.mypy_cache`
- `apps/creator-agent/.pytest_cache`
- `apps/creator-agent/.ruff_cache`
- `community-assistant-agent/.pytest_cache`
- `community-assistant-agent/.ruff_cache`

共清理 65 个可访问生成缓存目录，另清理受保护路径下 4 个显式缓存目录。没有删除源码、测试、数据库、Docker service、middleware data 或架构文档。

## 3. 保留资源

- `assistant_runs`、`RunRepository`、相关 migration：保留，等待 Runtime projection 完成。
- `community-assistant-agent`、Legacy fallback 和 `ENABLE_LEGACY_AGENT_FALLBACK`：保留，CI/脚本/兼容流仍依赖。
- Runtime core、execution persistence、EventStore、checkpoint、lease：保留。
- Redis、Kafka、Qdrant、Docker service/volume：保留，存在运行或集成引用。
- 测试和历史文档：保留；没有删除仅因包含 Legacy 关键词的测试或报告。

## 4. 未删除的候选

没有删除以下不确定资源：

- `scripts/run_p0_e2e.py`、`scripts/runtime-report.ps1`、`scripts/verify-all.ps1`、`scripts/smoke-test.ps1`
- `docs/community-agent-orchestration.md` 和正式集成文档
- `node_modules/debug`，这是依赖包而非调试产物
- 任意 Redis key、Kafka topic、Qdrant collection 或 Docker volume

原因是这些资源仍有 CI、部署、运行、集成或历史恢复价值。

## 5. 风险评估

- 缓存删除只影响本地构建/测试缓存，会在下次运行时重建。
- `assistant_runs` 和 Legacy 资源仍处于兼容阶段，提前删除会破坏旧 API、E2E 或恢复流程。
- 中间件资源无法仅通过静态代码扫描证明无用，误删可能造成数据丢失或服务启动失败。

## 6. 保护确认

- Worker：未修改
- Planner：未修改
- ToolRuntime：未修改
- ExecutionStateManager：未修改
- PlanExecution：未修改
- 数据库 schema/migration：未修改
- Runtime 状态模型：未修改
