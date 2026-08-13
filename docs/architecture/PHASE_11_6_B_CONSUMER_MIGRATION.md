> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-B Runtime API Consumer Migration

## 1. 目标

本阶段把 Runtime-backed 请求的查询和控制主键迁移到 `execution_id`，同时保留 Legacy-only run 的 `run_id` API。没有删除 `assistant_runs`、`RunRepository` 或旧 API，也没有修改 Worker、Planner、ToolRuntime 或 ExecutionStateManager。

## 2. 修改内容

### Frontend

修改：

- `apps/frontend/src/types/assistant.ts`
- `apps/frontend/src/services/assistantService.ts`
- `apps/frontend/src/components/assistant/AssistantPanel.tsx`
- `apps/frontend/src/components/comments/CommentSection.tsx`
- `apps/frontend/src/pages/TaskCenterPage.tsx`

Runtime-backed response 现在透传：

- `execution_id`
- `execution_reference`

Assistant service 的行为：

- 有 `execution_id`：调用 `/executions/{execution_id}`、`/steps`、`/events/stream`。
- 取消：`POST /executions/{execution_id}/cancel`。
- 暂停：`POST /executions/{execution_id}/pause`。
- 恢复：`POST /executions/{execution_id}/resume`。
- 重试：读取 Runtime steps，选择失败 step 调用 `/executions/{execution_id}/steps/{step_id}/retry`。
- 没有 execution mapping：继续使用旧 `/runs/{run_id}` compatibility API。

`Execution Console` 原有的 `executionService`、`useExecutionConsole` 和执行详情页已经直接消费 execution API，本阶段将旧 Assistant 交互也接入同一服务。

### API

修改：

- `apps/assistant_api/greenbook_assistant_api/api/routes.py`

`GET /api/v1/assistant/runs/{run_id}` 现在先解析 Runtime link：

- mapped Runtime：status 和 steps 从 `PlanExecution` 获取。
- Legacy-only：status 和旧 steps 继续从 `assistant_runs` 获取。

事件和 SSE 的 mapped 分支此前已经通过 `RunOperationAdapter` 使用 Execution EventStore；Legacy-only 继续使用旧 `assistant_runs.events`。

正式 Runtime API 仍由 `runtime_routes.py` 提供：

```text
GET  /executions/{execution_id}
GET  /executions/{execution_id}/steps
GET  /executions/{execution_id}/events
GET  /executions/{execution_id}/stream
POST /executions/{execution_id}/pause
POST /executions/{execution_id}/resume
POST /executions/{execution_id}/cancel
POST /executions/{execution_id}/steps/{step_id}/retry
```

### Java client

审计确认 `packages/java_client/greenbook_java_client/client.py` 的 `agent_run_id` 仍用于 Java capability 请求头 `X-Agent-Run-Id`，不是 Assistant execution control contract。该字段同时承担下游 trace/业务关联语义，因此本阶段没有机械重命名。

后续应在 Java Assistant API contract 支持 `ExecutionReference` 后，按接口版本增加可选 `execution_id`，再逐个迁移 Runtime-backed 调用；现有 `agent_run_id` 继续兼容。

## 3. 数据来源边界

```text
Runtime-backed:
ExecutionReference -> execution_id -> PlanExecution / EventStore

Legacy-only:
run_id -> assistant_runs / Legacy API
```

`assistant_runs` 仍保留 conversation、用户归属、原始内容和 Legacy metadata。它不再作为 mapped Runtime 的 status、steps 或 events 真相源。`RunExecutionAdapter` 仍是当前兼容边界，尚不能删除，因为旧 API、approval 和部分外部 consumer 仍使用 `run_id`。

## 4. 保留行为

- Legacy-only run 没有 `execution_id` 时，查询、旧 SSE、cancel 和旧响应保持原路径。
- 旧 `/runs/{run_id}` API path 不删除。
- Runtime-backed run 仍可在 API response 中返回 `run_id`，用于兼容旧客户端，但前端控制不再使用它作为主键。
- 不修改 `assistant_runs` schema，不删除其 migration。

## 5. 测试与验证

- Frontend Vitest: `1` test file, `2 passed`
- Frontend production build: `npm run build` passed
- Compatibility tests: `pytest -q tests/compat/runtime` -> `21 passed`
- Python compile: `python -m compileall -q apps/assistant_api/greenbook_assistant_api packages/assistant_core/greenbook_assistant_core` passed
- `git diff --check`: passed

构建仅有已有的 Browserslist/Baseline browser data 更新提示，不影响构建结果。

## 6. 未覆盖内容

本阶段没有：

- 删除或归档 Legacy Agent/community-assistant-agent
- 删除 `assistant_runs` 或 `RunRepository`
- 修改数据库结构
- 迁移 Creator 自己的 `creator_runs` / `run_id`
- 迁移所有 Java `agent_run_id` trace 字段

这些工作需要后续独立的停用、数据归档和兼容窗口验证。
