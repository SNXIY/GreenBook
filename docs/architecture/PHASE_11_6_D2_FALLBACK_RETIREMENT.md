# Phase 11.6-D2 Legacy Fallback Retirement

## 1. 目标

Legacy 已从正常执行路径降级为 emergency compatibility。Runtime 是默认执行路径；只有显式 `ASSISTANT_RUNTIME_MODE=off` 才允许正常 Legacy execution，Runtime failure 不会自动切换 Legacy。

## 2. RuntimeRouter 收敛

`RuntimeRouter` 现在的规则：

- `on` -> `RUNTIME`
- `dual` -> `RUNTIME`，作为 Runtime-only compatibility alias
- `off` -> `LEGACY`
- task intent 缺失 -> 在 `on`/`dual` 仍为 `RUNTIME`
- 未登记 scenario -> 在 `on`/`dual` 仍为 `RUNTIME`

这样不会因为理解结果缺失、未覆盖类别或 relation 不匹配而隐式进入 Legacy。`supported_scenarios()` 保留用于能力登记和历史测试，不再决定正常执行路径。

## 3. AssistantService fallback 边界

- `ENABLE_LEGACY_AGENT_FALLBACK` 默认值为 `false`。
- Runtime success 永远不调用 Legacy。
- Runtime failure 默认返回 `FAILED` / `RUNTIME_ERROR`，并保留 Runtime path、run id 和 trace id。
- 只有显式 `ENABLE_LEGACY_AGENT_FALLBACK=true` 且 Legacy service 已注入时，才通过 `LegacyFallbackAdapter` 执行 emergency fallback。
- `Runtime mode` 下 Runtime 未注册时返回 `RUNTIME_UNAVAILABLE`，不转 Legacy。

## 4. Legacy route 分类

### 必须保留的 emergency compatibility

- `ASSISTANT_RUNTIME_MODE=off` 的显式回滚模式。
- `LegacyFallbackAdapter` 和 `LegacyExecutionPort`，直到 emergency window 关闭。
- `LegacyAgentService` 注入能力，直到生产确认 fallback 使用量为零并完成停用窗口。
- `ENABLE_LEGACY_AGENT_FALLBACK` 配置和 fallback metrics，作为临时运维开关与审计信号。
- `assistant_runs`、`RunRepository`、旧 `/runs` API、`community-assistant-agent`，本阶段不处理。

### 可以在后续阶段删除

只有满足退休条件后，才可删除：

- RuntimeRouter 的 Legacy 分支和 `off` mode。
- `LegacyFallbackAdapter` / `LegacyExecutionPort`。
- `LegacyAgentService` 及 `packages/assistant_core/.../agent.py` 旧实现。
- `ENABLE_LEGACY_AGENT_FALLBACK` 与 fallback telemetry。
- 仅验证正常 Legacy execution 的测试。

当前没有可立即删除的生产 Legacy 文件，因为 `off` mode、旧 API、CI/E2E 和历史数据仍保留。

## 5. 初始化行为

`main.py` 与 API lazy initialization 仅在以下条件创建 `LegacyAgentService`：

- `ASSISTANT_RUNTIME_MODE=off`
- 显式 `ENABLE_LEGACY_AGENT_FALLBACK=true`

`dual` 不再因为自身名称初始化 Legacy；除非同时开启 emergency fallback。

## 6. 测试调整

更新：

- `tests/unit/test_runtime_router.py`
  - 默认 mode 为 Runtime。
  - `on`/`dual` 在缺失 intent、unsupported scenario 时仍进入 Runtime。
  - 只有 `off` 进入 Legacy。
- `tests/unit/test_legacy_fallback_isolation.py`
  - Runtime success 不调用 Legacy。
  - 默认 failure 不 fallback。
  - 显式 emergency fallback 才调用 Legacy。

正常 Legacy 路由测试已收敛为显式 `off` compatibility 测试；没有删除 `assistant_runs` 或 Legacy service 测试。

## 7. 修改文件

- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`
- `apps/assistant_api/greenbook_assistant_api/main.py`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `tests/unit/test_runtime_router.py`
- `docs/architecture/PHASE_11_6_D2_FALLBACK_RETIREMENT.md`

本阶段没有修改 Worker、Planner、ToolRuntime、ExecutionStateManager、PlanExecution，也没有删除 `assistant_runs`、`RunRepository`、LegacyAgent 或 `community-assistant-agent`。
