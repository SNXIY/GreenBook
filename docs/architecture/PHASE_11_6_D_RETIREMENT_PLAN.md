# Phase 11.6-D Legacy Runtime Retirement Plan

## 1. 结论

当前不能安全删除 Legacy Runtime。虽然 Runtime 已是 ACTIVE execution source of truth，但 API 默认配置和路由仍允许甚至优先选择 Legacy：

- `apps/assistant_api/greenbook_assistant_api/main.py` 默认 `ASSISTANT_RUNTIME_MODE=off`。
- `RuntimeRouter` 在 `off`、没有 `task_intent` 或未支持场景时返回 `ExecutionPath.LEGACY`。
- `main.py` 始终构造 `LegacyAgentService`。
- `AssistantService` 默认 `ENABLE_LEGACY_AGENT_FALLBACK=true`。
- CI、脚本、E2E 和旧 API 仍引用 `community-assistant-agent` / `run_id`。

因此本阶段只输出迁移分析和安全删除候选，不删除数据库、`assistant_runs`、Legacy Agent 或 Runtime 代码。

## 2. Legacy Agent 调用链

```text
API startup
  -> LegacyAgentService
  -> AssistantService
  -> RuntimeRouter
  -> LegacyExecutionPort / LegacyFallbackAdapter
  -> packages/assistant_core/greenbook_assistant_core/agent.py
```

### 生产引用

- `apps/assistant_api/greenbook_assistant_api/main.py:38,189-193`
  - 构造并注入 `LegacyAgentService`。
- `apps/assistant_api/greenbook_assistant_api/api/routes.py:981-983`
  - 测试/懒初始化路径再次构造 Legacy service。
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
  - `_execute_legacy()` 是直接执行路径。
  - `_execute_runtime()` 在 Runtime 异常时按开关进入 fallback。
- `apps/assistant_api/greenbook_assistant_api/services/legacy_fallback_adapter.py`
  - 明确的 Legacy compatibility boundary。
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`
  - `off`、缺少 intent 和未覆盖 scenario 都路由到 Legacy。
- `packages/assistant_core/greenbook_assistant_core/agent.py`
  - 旧 `CommunityOperationsAssistant` 实现。

### 配置引用

- `ENABLE_LEGACY_AGENT_FALLBACK`：`AssistantService` 默认启用。
- `ASSISTANT_RUNTIME_MODE`：默认 `off`，导致 Runtime 不是默认执行路径。
- `ASSISTANT_IDENTITY_AUDIENCE=greenbook-assistant-runtime`：当前 ACTIVE Assistant Runtime 的统一 audience。

### 测试/运维引用

- `tests/unit/test_legacy_fallback_isolation.py`
- `tests/unit/test_runtime_router.py`
  - 明确验证多个场景仍返回 `ExecutionPath.LEGACY`。
- `scripts/verify-all.ps1`
- `scripts/smoke-test.ps1`
- `scripts/runtime-report.ps1`
- `scripts/run_p0_e2e.py`
- `.github/workflows/verify.yml`
- `community-assistant-agent/`

## 3. assistant_runs 使用点

### 3.1 必须迁移

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
  - `RunRepository.create/find_by_id/find_all_by_user/update`。
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  - `send_message()` 仍创建 run snapshot。
  - `/runs`、`/runs/{run_id}` 仍是旧 API contract。
  - Legacy-only cancel/interrupt 仍更新 `assistant_runs`。
  - Runtime mapped 的 events/SSE/control 已通过 adapter，但仍保留旧入口。
- `apps/frontend/src`
  - Legacy response、历史列表和旧 route 仍保留 `run_id`。
- `tests/integration/test_assistant_runtime_contracts.py` 及相关 E2E
  - 仍以 `/runs/{run_id}` 验证兼容行为。

迁移要求：

1. 所有新请求以 `execution_id` 创建和查询 Runtime。
2. mapped `/runs` 只作为兼容 projection/read view，不读取 `assistant_runs` 的 Runtime status/events。
3. 旧 API、approval、cancel、resume、SSE 和前端全部通过 `ExecutionReference`。
4. Legacy-only 数据进入只读历史模式。

### 3.2 可保留为历史数据

- `assistant_runs.run_id`
- conversation/user/tenant 关联
- 原始 prompt、Legacy response、trace metadata
- `session_snapshot`、`partial_results`
- Legacy-only `events`
- 历史 `approval_id` 关联

这些字段不是 Runtime 状态源，但在保留窗口内仍服务旧客户端、审计和恢复。

### 3.3 当前没有可立即删除的使用点

静态扫描没有找到满足全部条件的候选：无生产引用、无测试引用、无 CI/脚本引用、已有替代实现。`RunRepository` 和 `assistant_runs` 目前都不满足。

## 4. 分类清单

### KEEP（当前必须保留）

- `PlanExecution`、`ExecutionStateManager`、Worker、ToolRuntime、ExecutionEventStore。
- `RunExecutionAdapter`：旧 API、approval、SSE 和历史数据尚未全部迁移。
- `assistant_runs`、`RunRepository`：Legacy-only 和兼容读取仍存在。
- `community-assistant-agent`：CI、脚本、E2E、认证 audience 仍引用。
- `LegacyAgentService`：当前默认路由和 fallback 仍会调用。
- Legacy fallback tests 和 compatibility tests：用于退休前回归。

### MIGRATE

- `ASSISTANT_RUNTIME_MODE`：逐步从 `off` 切换为 `on`，先经过 dual/观察窗口。
- `ENABLE_LEGACY_AGENT_FALLBACK`：先改为默认关闭并保留显式紧急开关。
- `RuntimeRouter`：覆盖现有 Legacy scenario，确保所有新 Assistant execution 请求进入 Runtime。
- `main.py` 和 API lazy initialization：Runtime 成功路径不再要求构造 Legacy service。
- 前端、Java client、E2E、CI、脚本：迁移到 execution contract。
- `assistant_runs`：收敛为 Legacy metadata/projection，再进入只读历史。

### DELETE CANDIDATE（满足前置条件后）

- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_fallback_adapter.py`
- `packages/assistant_core/greenbook_assistant_core/agent.py` 中旧执行实现
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py` 的 Legacy 分支
- `ENABLE_LEGACY_AGENT_FALLBACK` 配置及相关 telemetry
- Legacy-only API/readers/tests/scripts
- `community-assistant-agent/` 及其 CI/Docker/script 引用
- 最终阶段的 `RunExecutionAdapter`、`RunRepository` 和 `assistant_runs`

以上均不是当前批准删除清单，只是依赖退休条件满足后的候选。

## 5. 安全退休顺序

### Phase 11.6-D1：Runtime 默认化

1. 完成 Router scenario coverage，所有 ACTIVE Assistant 请求可由 Runtime 执行。
2. 将 `ASSISTANT_RUNTIME_MODE` 在 staging/production 切换到 Runtime，并保留可回滚配置。
3. 确认 Runtime 失败返回明确失败、retryable/error trace，不触发 Legacy。
4. 保留 fallback metrics，观察一段完整发布窗口。

退出条件：Runtime success path 不依赖 Legacy，所有目标 scenario 无 Legacy route。

### Phase 11.6-D2：Fallback 关闭

1. `ENABLE_LEGACY_AGENT_FALLBACK=false` 成为默认值。
2. Legacy fallback 只允许显式运维开关，并记录每次使用。
3. 清理只测试 fallback 的新用例，保留一条兼容回归用例直到最终删除。
4. 更新启动文档、CI 和 E2E，禁止把 fallback 当正常执行路径。

退出条件：生产观察窗口内 fallback count 为 0，且回滚开关未被使用。

### Phase 11.6-D3：Legacy API 与 Projection 冻结

1. 停止创建新的 Legacy-only run。
2. `assistant_runs` 只读历史和必要 projection，不再保存 Runtime status/events 真相。
3. `/runs` API、approval、SSE、cancel/resume 全部通过 execution reference。
4. 将 `community-assistant-agent` 从 CI/脚本/部署中移除前，完成独立停用确认。

退出条件：无新 Legacy run，无 Runtime consumer 依赖 `assistant_runs` 状态。

### Phase 11.6-D4：最终删除

按以下顺序执行独立变更：

1. 删除 Legacy API fallback 分支和旧测试。
2. 删除 `LegacyAgentService`、adapter 和旧 `agent.py`。
3. 删除 `RunExecutionAdapter` 与 `RunRepository`。
4. 归档并验证历史数据后，再以独立数据库 migration 删除 `assistant_runs`。
5. 最后清理 CI、脚本、环境变量和文档。

数据库删除必须是最后一步，且需要备份、回滚 migration、恢复演练和业务批准。

## 6. 安全删除候选检查表

文件或资源只有同时满足以下条件才可从 DELETE CANDIDATE 进入实际删除：

- 无生产 import/调用。
- 无 API route、CI、Docker、启动脚本或 E2E 引用。
- 无 Legacy-only 数据读取需求。
- Runtime replacement 已在线验证。
- compatibility tests 已迁移或明确保留历史 contract。
- 有回滚路径和发布窗口记录。

当前满足全部条件的 Legacy execution 资源：无。

## 7. 本阶段保护范围

- 不删除 `assistant_runs`。
- 不删除任何数据库 migration。
- 不删除 `community-assistant-agent`。
- 不修改 Worker、Planner、ToolRuntime、ExecutionStateManager 或 PlanExecution。
- 不执行全局 `run_id` 替换。
- 仅新增本退休计划文档。
