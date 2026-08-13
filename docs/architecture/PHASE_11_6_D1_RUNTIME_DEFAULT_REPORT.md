> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D1 Runtime Default Migration Report

## 1. 默认行为

默认配置已改为：

```text
ASSISTANT_RUNTIME_MODE=on
ENABLE_LEGACY_AGENT_FALLBACK=false
```

修改位置：

- `.env.example`
- `apps/assistant_api/greenbook_assistant_api/main.py`
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`

`off` 仍是显式 Legacy mode，保留为回滚方式。`dual` 仍可用于灰度/兼容场景。

## 2. RuntimeRouter 行为

- `off`：所有请求走 Legacy。
- `on`：所有请求进入 Runtime，包括 task intent 缺失和暂未登记的 scenario。
- `dual`：已支持 scenario 进入 Runtime，其他请求保留 Legacy compatibility。

RuntimeRouter 默认构造模式已从 `off` 改为 `on`。没有进行全局 `run_id` 替换，也没有修改 Planner 或 IntentSpec。

## 3. AssistantService 行为

### Runtime success

Runtime 成功时直接返回 RuntimeResult，Legacy 不会被调用。

### Runtime failure

默认 `ENABLE_LEGACY_AGENT_FALLBACK=false`：

- 返回 `FAILED`
- `execution_path=runtime`
- `error_code=RUNTIME_ERROR`
- 保留 `run_id` 和 `trace_id`
- 不调用 Legacy Agent

### Emergency fallback

只有显式设置 `ENABLE_LEGACY_AGENT_FALLBACK=true` 且提供 Legacy service 时，Runtime failure 才会通过 `LegacyFallbackAdapter` 回退。fallback 计数、失败原因和路由仍记录在现有 telemetry 中。

### Runtime unavailable

Runtime mode 下未注册 Runtime service 时返回 `RUNTIME_UNAVAILABLE`，不隐式转入 Legacy。

## 4. LegacyAgentService 初始化

`main.py` 和 API lazy initialization 现在按以下规则创建 Legacy service：

- `ASSISTANT_RUNTIME_MODE=off`
- `ASSISTANT_RUNTIME_MODE=dual`
- 或显式开启 `ENABLE_LEGACY_AGENT_FALLBACK=true`

纯 Runtime 默认模式不会初始化 `LegacyAgentService`。Runtime service 在非 `off` 模式注册。

## 5. 修改文件

- `.env.example`
- `apps/assistant_api/greenbook_assistant_api/main.py`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`
- `tests/unit/test_legacy_fallback_isolation.py`
- `tests/unit/test_runtime_router.py`
- `docs/architecture/PHASE_11_6_D1_RUNTIME_DEFAULT_REPORT.md`

未修改：

- Worker
- Planner
- ToolRuntime
- ExecutionStateManager
- PlanExecution
- `assistant_runs`
- `RunRepository`
- Legacy Agent 源码
- `community-assistant-agent`

## 6. 测试结果

- Targeted unit tests: `15 passed`
- Compatibility tests: `21 passed`
- Python compileall: passed
- `git diff --check`: passed

全量 `pytest tests/unit` 在测试收集阶段受现有环境依赖阻断：当前 Anaconda Python 缺少 `fastapi`，导致既有 `test_revision_orchestration.py` 和 `test_time_parser.py` 无法导入 API routes。该问题不是本次断言失败；本次相关 unit 与 compatibility 测试均通过。

## 7. 回滚方式

将环境变量显式设置为：

```text
ASSISTANT_RUNTIME_MODE=off
ENABLE_LEGACY_AGENT_FALLBACK=true
```

即可恢复 Legacy 默认入口和 Runtime failure fallback。数据库、Legacy service 和旧 API 均未删除。
