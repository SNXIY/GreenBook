# Phase 9 Execution Console Design

## 1. Scope and Goal

Execution Console 将现有 Agent Runtime 的状态、步骤、事件和人工等待点呈现给用户，并提供受控的生命周期操作。它是 Runtime 的观察和控制客户端，不拥有执行状态，也不重新解释 IntentSpec、不生成 TaskPlan。

目标闭环：

```text
Execution API
    -> initial snapshot
SSE event stream
    -> live step/status updates
Execution controls
    -> Runtime control API
```

## 2. Current Frontend Stack

`apps/frontend/` 当前使用：

- React 18.2；
- TypeScript 5.2；
- Vite 5；
- React Router 6；
- CSS Modules 和全局 `index.css`；
- `react-markdown` / `remark-gfm` 用于内容展示；
- `clsx` 用于样式组合。

没有 Redux、Zustand、React Query 或其他全局数据缓存框架。认证使用 `AuthContext`，其余页面以本地 `useState`、`useEffect`、`useCallback` 管理请求和刷新状态。

## 3. Existing Routing and Layout

入口链路为：

```text
main.tsx
 -> BrowserRouter
 -> AuthProvider
 -> App.tsx
 -> Routes
```

当前路由包括首页、创建、任务中心、通知、个人资料和帖子详情。执行控制台建议新增：

```text
/executions/:executionId
```

执行详情页应复用 `AppLayout`、`Sidebar`、`MainHeader` 和现有认证上下文。任务中心仍可作为入口：当任务具有关联 `execution_id` 时，跳转到执行详情页；旧的 `run_id` 任务继续走兼容页面或由 API adapter 提供映射。

## 4. Existing API Client and Authentication

`src/services/apiClient.ts` 统一封装 `fetch`：

- 默认使用相对路径，开发环境通过 Vite proxy 转发；
- 生产环境可由 `VITE_API_BASE_URL` 配置；
- 自动从 `localStorage` 读取 `zhiguang_auth_tokens`；
- 使用 `Authorization: Bearer <accessToken>`；
- 支持 `AbortSignal`；
- 统一转换非 2xx 响应为 `ApiError`。

Execution Console 应新增专用的 `executionService`，复用 `apiFetch`，避免页面直接调用 `fetch`。服务层负责类型化 response、错误信息和控制操作的并发锁。

## 5. Existing Runtime API

`apps/assistant_api/.../api/runtime_routes.py` 当前注册在应用根路由，提供：

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/executions/{execution_id}` | status、current step、progress、timestamps |
| GET | `/executions/{execution_id}/steps` | step 状态、retry count、错误信息 |
| GET | `/executions/{execution_id}/events` | 历史事件 |
| GET | `/executions/{execution_id}/stream` | SSE 实时事件 |

现有 `StepExecutionResponse` 已包含 `step_id`、`capability`、`status`、`retry_count`、`error_code`、`error_message` 和时间字段，但当前 response 没有 input/output artifact 字段。因此 artifact 面板需要等待已有 ArtifactStore 的只读 API，或在 UI 第一版只显示事件 payload 中已经公开的结果摘要。

## 6. API Contract Gap

RuntimeManager 已有 `pause_execution`、`resume_execution`、`cancel_execution`，RetryManager 已有 `retry_step`，但当前 Runtime API 路由没有对应的用户控制 endpoint。要支持本设计，API 层需要后续增加兼容的控制 contract，例如：

```text
POST /executions/{execution_id}/pause
POST /executions/{execution_id}/resume
POST /executions/{execution_id}/cancel
POST /executions/{execution_id}/steps/{step_id}/retry
```

这些 endpoint 必须只调用 `RuntimeManager` / `ExecutionStateManager` 的既有入口；UI 不应直接访问 repository、Worker 或 ToolRuntime。该缺口属于 API 层前置工作，本设计阶段不修改执行代码。

## 7. SSE and Transport Design

当前服务端 SSE 使用 `StreamingResponse` 和 `subscribe_execution_events`，通过 polling EventStore 推送以下事件：

- `STEP_STARTED`
- `STEP_COMPLETED`
- `STEP_FAILED`
- `STEP_RETRY_REQUESTED`
- `APPROVAL_REQUIRED`
- `EXECUTION_COMPLETED`
- `EXECUTION_FAILED`

浏览器原生 `EventSource` 不支持自定义 `Authorization` header，而现有前端认证依赖 Bearer token。因此首选实现是使用带 Bearer header 的 `fetch` 流式读取 SSE，并用 `AbortController` 在页面卸载或 execution terminal 状态时关闭连接。若后端未来提供同源 HttpOnly cookie 认证，才可改用原生 `EventSource`。

不引入 WebSocket：当前数据模型是单向事件流，SSE 已足够；pause/resume/cancel/retry 通过普通 HTTP mutation 完成。

连接策略：

1. 首次进入页面并行获取 status、steps、events；
2. 建立 SSE，使用事件顺序和 event id 去重；
3. 收到事件后局部更新，必要时重新获取 status/steps；
4. 页面离开、token 失效或 terminal 状态时关闭流；
5. 断线时指数退避重连，并重新拉取快照，避免只依赖丢失事件。

## 8. Execution Console Page

建议布局：

```text
Execution Header
  status | progress | current step | created/updated | controls

Main content
  Step timeline              Detail panel
  ordered step states        selected step input/output/error
  retry count                artifact links
  duration                   event timestamp

Attention strip
  approval waiting / blocked / retryable failure
```

### Status Header

展示 `execution_id`、状态、百分比进度、当前 step、更新时间和连接状态。状态颜色必须同时配合文字，不能只依赖颜色：

- `RUNNING`：执行中；
- `WAITING_APPROVAL` / `WAITING_HUMAN`：等待用户处理；
- `PAUSED`：用户主动暂停；
- `FAILED`：执行失败；
- `COMPLETED`：已完成；
- `CANCELLED`：已取消。

### Step Timeline

按 API 返回顺序展示步骤，使用稳定的 `step_execution_id` 作为 key。每项展示：step 名称、capability、状态、开始/完成时间、耗时、retry count 和错误摘要。展开后展示完整错误和事件上下文。

### Artifact Detail

选中 step 后展示：

- input 参数摘要；
- output/artifact 摘要；
- artifact 链接或下载入口；
- 生成时间和 MIME 类型；
- 访问失败或内容不可用状态。

UI 不自行解析工具私有 payload，不把 artifact 内容当作执行状态。没有 artifact API 时显示明确的“暂无可查看产物”，不伪造数据。

### Approval Waiting

当状态为 `WAITING_APPROVAL` 或收到 `APPROVAL_REQUIRED` 时，顶部显示高优先级人工操作区，包含：

- 等待原因；
- 关联 step；
- 即将执行的 capability 摘要；
- 继续/批准入口（由现有 human interaction contract 决定）；
- 取消入口。

“暂停”和“批准”是不同操作：暂停是用户控制 Runtime，批准是业务确认并恢复等待中的操作，UI 必须分开表达。

## 9. Controls and State Rules

| Current state | Allowed UI actions |
| --- | --- |
| `RUNNING` | pause, cancel |
| `PAUSED` | resume, cancel |
| `WAITING_APPROVAL` | approval action, cancel |
| `WAITING_HUMAN` | human response, cancel |
| `FAILED` | retry only when selected step is retryable; cancel may be disabled |
| `COMPLETED` | view only |
| `CANCELLED` | view only |

每次 mutation 都需要：禁用重复点击、显示进行中状态、处理 409/422 等非法状态转换、成功后立即刷新快照，并等待 SSE 或主动刷新确认最终状态。前端不能乐观地把执行状态写成目标状态。

## 10. State Management Proposal

第一版不新增全局状态库。新增页面级 `useExecutionConsole(executionId)` hook，内部维护：

- `execution` snapshot；
- `steps`；
- `events`；
- `selectedStepId`；
- `connectionState`；
- `loading/error`；
- `pendingOperation`。

将 SSE 事件规整为 reducer action，统一处理重复事件、乱序事件和 terminal 状态。若未来任务列表、通知和执行详情需要共享实时 execution cache，再评估引入轻量缓存层；本阶段不提前引入 Redux/Zustand。

## 11. Error, Loading and Empty States

- 首次加载：显示 status/step skeleton，不阻塞整个页面布局；
- 单项接口失败：保留已成功加载的区域，并显示可重试提示；
- SSE 断开：显示“实时连接已断开，正在重连”，同时保留最近快照；
- execution 不存在：展示 404 页面和返回任务中心操作；
- 无 steps：显示计划尚未展开或数据不可用，不显示假进度；
- retry 不允许：展示服务端错误原因，不在前端自行判断可恢复性；
- artifact 不存在：显示空状态而不是空白面板。

## 12. Security and Data Boundaries

- 所有 GET 和 mutation 使用当前用户 Bearer token；
- 不在 URL、日志或事件列表中暴露 token；
- 服务端负责 execution 所属用户/租户授权，前端不能通过隐藏按钮代替授权；
- artifact 展示遵守现有 artifact 访问策略；
- 错误详情按 API 返回内容展示，避免直接渲染不可信 HTML。

## 13. Suggested Implementation Sequence

1. 确认并补齐 Runtime API 的控制 contract 和 artifact read contract；
2. 新增前端 execution types、`executionService` 和授权 SSE reader；
3. 新增 `useExecutionConsole` 快照/SSE reducer；
4. 新增 `/executions/:executionId` 页面和 timeline；
5. 接入任务中心的 execution link；
6. 增加 API mock、SSE 断线、状态转换和权限测试；
7. 运行 frontend `npm run lint`、`npm run build` 以及现有 Python tests。

## 14. Explicit Non-Goals

本设计不修改：

- Execution Runtime 状态模型；
- ExecutionStateManager、Worker、ToolRuntime；
- Planner、TaskPlan、IntentSpec；
- retry 或 checkpoint 算法；
- Legacy Run 的数据模型；
- WebSocket、Redis/Kafka event bus 或新的全局状态框架。

