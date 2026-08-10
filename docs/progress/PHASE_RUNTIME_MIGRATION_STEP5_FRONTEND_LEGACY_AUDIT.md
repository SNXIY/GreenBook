# Phase 5：Frontend + Legacy Audit

审计范围：`zhiguang-fe`、`apps/frontend`、`apps/assistant_api`、
`packages/assistant_core`、`community-assistant-agent`、`creator-agent`、
`moderation-agent`、`zhiguang-be`、`archive/legacy` 以及
`docs/architecture`。

本阶段只做读取和引用扫描。没有修改业务代码，没有删除文件，也没有移动目录。

## 1. 结论摘要

当前状态不是“Frontend 已完成 Runtime 迁移后的 cleanup”，而是后端 Runtime
已经成为默认执行事实来源、前端仍使用旧 Run 合同、两者通过兼容投影并存的过渡态：

1. `apps/frontend` 当前不存在；实际唯一的 Web 前端是 `zhiguang-fe`。
2. `apps/assistant_api` 已注册 Runtime HTTP API，默认 `execution_mode=runtime`，
   新消息响应也可以携带 `execution_id`。
3. `zhiguang-fe` 的 Assistant UI、Task Center、评论助手仍以 `run_id`、
   `AssistantRun` 和 `/api/v1/assistant/runs/**` 为主；没有调用 `/api/v1/executions/**`。
4. `CommunityOperationsAssistant`、`run_store`、旧 `/runs` 路由和
   `assistant_runs` 仍有真实代码或测试引用。它们现在是兼容/历史边界，不是可以直接删除的死代码。
5. 部分 Phase 11.6-D4 文档描述了一个不存在于当前工作树的 `apps/frontend` 迁移结果，
   因此本报告以当前源码和实际路由为准。

## 2. 当前目录与运行边界

| 路径 | 当前事实 | 本次结论 |
|---|---|---|
| `apps/assistant_api/greenbook_assistant_api` | FastAPI Assistant API；`main.py` 初始化 Runtime providers，并同时注册 `routes` 与 `runtime_routes` | 当前 API 入口 |
| `apps/assistant_worker` | Runtime Worker 代码 | Runtime 必须保留 |
| `packages/assistant_core` | `PlanExecution`、Execution state/event、Task/Intent、兼容历史仓储 | Runtime 核心和兼容边界 |
| `services/greenbook_mcp` | Assistant 使用的 MCP 工具边界 | Runtime 工具依赖，必须保留 |
| `apps/backend` | 新 Java Backend | Runtime 后端依赖，必须保留 |
| `apps/creator-agent` | 新 Creator 服务/客户端边界 | 需按部署合同保留 |
| `zhiguang-fe` | 当前实际 React/Vite 前端 | 当前消费者，不能作为旧目录直接删除 |
| `apps/frontend` | 不存在（`Test-Path apps/frontend` 为 `False`） | 架构文档中的目标目录尚未落地 |
| `community-assistant-agent` | 独立旧 Assistant 应用、旧 routes、worker、migrations、tests | Legacy，暂不能删除 |
| `archive/legacy/community-assistant-agent` | 旧应用归档副本 | 历史归档，保留 |
| `creator-agent` | 另一套独立 Creator 项目，包含自身 runtime/memory/retrieval/evaluation | 与 Assistant Runtime 不可混删，先做部署合同审计 |
| `moderation-agent` | 独立审核服务，仍有源码、测试和 CI job | 非 Assistant Runtime 状态来源，暂保留 |
| `zhiguang-be` | 现存 Java Backend，含 `assistant_run_id` provenance 字段 | 仍有历史/审计合同，不能直接删除 |

`.github/workflows/verify.yml` 仍对 `zhiguang-fe`、`creator-agent`、
`moderation-agent` 和 `community-assistant-agent` 配置检查任务，这些目录当前仍是
仓库级消费者或交付对象。

## 3. 前端实际调用链

### 3.1 AssistantPanel 和评论助手

当前链路如下：

```text
AssistantPanel.tsx / CommentSection.tsx
  -> services/assistantService.ts
  -> POST /api/v1/assistant/conversations/{conversation_id}/messages
  -> RunAcceptedResponse (前端只取 accepted.run_id)
  -> waitForAssistantRun(run_id)
  -> GET /api/v1/assistant/runs/{run_id}/events/stream
  -> 事件帧后重新 GET /api/v1/assistant/runs/{run_id}
  -> AssistantRun / AssistantRunStep UI
```

证据：

- `zhiguang-fe/src/components/assistant/AssistantPanel.tsx:13,53,150-172` 保存
  `AssistantRun`，发送后用 `accepted.run_id` 调用 `waitForAssistantRun`。
- `zhiguang-fe/src/components/comments/CommentSection.tsx:7-8,35,102-161` 使用同一
  `run_id` 和旧审批合同。
- `zhiguang-fe/src/services/assistantService.ts:79-98` 的 `send` 返回类型是
  `AssistantRunAccepted`；实现没有读取 `execution_id`。
- `zhiguang-fe/src/services/assistantService.ts:204-258` 只打开
  `/assistant/runs/{run_id}/events/stream`，并以 `/runs/{run_id}` 轮询作为 fallback。

### 3.2 Task Center

`zhiguang-fe/src/pages/TaskCenterPage.tsx` 仍是 Run 驱动：

- `listRuns()` 请求 `/api/v1/assistant/runs?limit=30`；
- Assistant 行以 `assistant:${run.run_id}` 为 key；
- 进度和步骤从 `AssistantRunListItem.steps`/旧状态映射计算；
- 审批、retry、resume、interrupt、cancel 全部传 `run.run_id`；
- Creator 行另外使用 `creatorTaskService` 的 `task_id`，不能与 Assistant
  `execution_id` 全局混为一谈。

### 3.3 前端类型模型

`zhiguang-fe/src/types/assistant.ts` 仍是旧模型：

- `AssistantMessage` 有可选 `run_id`，没有 `execution_id`；
- `AssistantRunAccepted` 必填 `run_id`，没有 Runtime execution 字段；
- `AssistantRun`/`AssistantRunStep` 使用 `QUEUED`、`WAITING_LANE`、
  `WAITING_DEPENDENCY` 等 Run 状态和 `ordinal/kind/tool_name` 字段；
- 没有 `ExecutionStatusResponse`、`ExecutionStepsResponse`、
  `ExecutionEventsResponse` 或 Runtime 的 `capability/retry_count/error_code` 类型。

### 3.4 前端调用与 Runtime API 对照

| 前端实际调用 | 后端当前路由 | 实际状态 | Runtime 对应/缺口 |
|---|---|---|---|
| `POST /api/v1/assistant/conversations/{id}/messages` | `api/routes.py:811` | 仍是兼容消息合同；Runtime 分支可返回 `execution_id`，前端忽略它 | 应保存并使用 `execution_id` |
| `GET /api/v1/assistant/conversations` | `api/routes.py:538` | 后端返回 `{items,page,size,total}`；前端按裸数组 `AssistantConversation[]` 读取 | 存在响应包络不一致 |
| `GET .../messages` | `api/routes.py:1197` | 后端 `MessageView` 只含 `role/content/trace_id/created_at` | 前端类型还期待 `message_id/parts/run_id` |
| `GET /api/v1/assistant/runs` | `api/routes.py:1303` | 当前 Task Center 的主列表来源 | Runtime 列表应是 `/api/v1/executions` |
| `GET /assistant/runs/{id}` | `api/routes.py:1232` | 兼容投影，读取 `run_store` | 不是 Runtime canonical state |
| `GET /assistant/runs/{id}/events/stream` | `api/routes.py:1330` | 前端唯一 SSE；旧 Run SSE | Runtime SSE 是 `/api/v1/executions/{id}/stream` |
| `/runs/{id}/cancel`, `/interrupt` | `api/routes.py:1341,1352` | 修改兼容 `run_store` 状态 | Runtime 控制应使用 execution control API |
| `/runs/{id}/resume`, `/retry` | 前端已调用，但当前 active `routes.py` 没有对应实现 | 当前合同不完整 | Runtime 只有 pause/resume 和 step retry 路由 |
| `/runs/{id}/approvals/{approval_id}` | 前端 `assistantService.ts:179-191` 调用 | 当前 active API 没有该嵌套路由；只有 `/approvals/{id}/approve|reject`（`routes.py:1363,1391`） | approval 合同不一致，Runtime 也没有同名 approve route |
| `/api/v1/executions` | `api/runtime_routes.py:189` 等 | 后端已存在 | 当前前端完全没有 service/type/consumer |
| `/api/v1/executions/{id}/steps` | `runtime_routes.py:259` | Runtime steps API 已存在 | 当前前端未调用 |
| `/api/v1/executions/{id}/events` | `runtime_routes.py:285` | Runtime events API 已存在 | 当前前端未调用 |
| `/api/v1/executions/{id}/stream` | `runtime_routes.py:394` | Runtime SSE 已存在 | 当前前端未订阅 |

### 3.5 SSE、失败和审批结论

当前前端实时性实际是“Run SSE 帧到达后重新拉 Run”，而不是直接消费
`ExecutionEventStore` 的 Runtime event。即使 POST 响应已经包含 `execution_id`，
后续 UI 仍可能只看到兼容投影中的状态和步骤。

Retry/Resume/Cancel/Approval 也没有统一到 execution identity；尤其嵌套 approval
路径与 active API 不一致。因而当前不能宣称“前端已经完成 Runtime execution 控制面迁移”。

## 4. 后端 Runtime 与兼容入口事实

`apps/assistant_api/greenbook_assistant_api/main.py` 的 lifespan 当前初始化：

```text
ExecutionRepository
ExecutionEventStore
ExecutionStateManager
RuntimeManager
RuntimeAgentService
ConversationRuntimeAdapter
RunExecutionAdapter
```

并写入 `app.state`；`main.py:252-253` 同时注册：

```text
app.include_router(router)
app.include_router(runtime_router, prefix="/api/v1")
```

Runtime 路由的状态读取链是 `RuntimeManager -> ExecutionStateManager ->
PlanExecution`，事件来自 `ExecutionEventStore`；它不应从 `assistant_runs` 推导执行状态。

消息路由 `api/routes.py:823` 在 Runtime 开关开启时调用
`ConversationRuntimeAdapter`；但 `routes.py:1032` 仍直接构造
`CommunityOperationsAssistant`。`main.py` 默认 `ASSISTANT_EXECUTION_MODE=runtime`，
同时保留 `ASSISTANT_RUNTIME_ENABLED=false`/`execution_mode=legacy` 回滚开关。因此
Runtime 是默认路径，但 Legacy 仍是可执行回滚路径，不是已删除路径。

`apps/assistant_api/.../services/assistant_service.py` 是 Runtime-only 的
`AssistantService`/`RuntimeRouter` 外观，但当前源码扫描只发现它被
`tests/e2e/test_execution_presentation.py` 实例化，未发现 `main.py` 或 active route
实例化；当前实际 HTTP 消息接线仍在 `routes.py` 和
`ConversationRuntimeAdapter`，该 service 不能被误判为前端已经完成迁移的证据。

## 5. Legacy 使用扫描

### 5.1 直接引用和数据边界

| 对象 | 当前证据 | 当前角色 |
|---|---|---|
| `CommunityOperationsAssistant` | `packages/assistant_core/greenbook_assistant_core/agent.py:185`；active `routes.py:13,1032`；integration/revision tests 仍直接 import | Legacy 执行循环，仍可被回滚入口调用 |
| `assistant_runs` | `packages/assistant_core/.../db/repositories.py`、`db/migrations/001_assistant_runs_history_projection.sql`、旧 app migrations、Java provenance mapper、兼容测试 | 历史/对话回合投影；不是 Runtime 状态表 |
| `run_store` | `main.py` 初始化；Runtime 和 Legacy 消息分支均写入；`/runs` 读取 | active in-memory 兼容响应投影 |
| `/runs` API | `routes.py:1232,1292,1303,1330,1341,1352` | 前端仍真实消费的兼容查询、SSE、控制接口 |
| `community-assistant-agent` | root 独立旧 FastAPI app、worker、migrations、tests；另有 `archive/legacy` 副本 | 独立 Legacy 应用/历史归档 |
| `compatibility/history` | `RunExecutionAdapter`、`ExecutionReference`、link repository | `run_id <-> execution_id` 标识桥；不拥有 Runtime state/events |
| `compatibility/intent` | `task/understanding.py` 的 L2 仍 import/use historical intent adapter | 仍有 active semantic compatibility 依赖 |
| `creator-agent` | root 独立 Creator 项目；`apps/creator-agent` 是另一套服务 | 不能按 Assistant Legacy 直接删除 |
| `moderation-agent` | 独立服务和测试；CI 仍有 job；active Assistant Runtime 通过 MCP/Java 边界，不直接 import 其内部模块 | 独立功能/部署边界，非当前 Assistant execution source |
| `zhiguang-be` | `assistant_comment_migration.sql`、`CommentMapper.xml` 和 `AssistantCommentProvenanceMapper` 保留 `assistant_run_id` | Java 侧历史 provenance 合同 |

### 5.2 分类 A：Runtime 必须保留

以下对象属于 canonical Runtime 或仍是其必要依赖：

- `PlanExecution`、`ExecutionRepository`、`ExecutionEventStore`、
  `ExecutionStateManager`、`RuntimeManager`、`RuntimeAgentService`；
- `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py` 及其
  `/executions`、steps、events、stream、Runtime control endpoints；
- `IntentSpecProvider`、`TaskProvider`、`IntentCompiler`、
  `ConversationRuntimeAdapter`；
- `apps/assistant_worker`、`packages/assistant_core` 的 Runtime execution/tool
  contracts、`services/greenbook_mcp`、`apps/backend`、Creator HTTP boundary；
- `ExecutionReference`/Run-to-Execution 标识接口在迁移窗口内仍需保留（其具体桥实现
  见分类 C），否则旧响应无法安全指向 Runtime execution。

### 5.3 分类 B：迁移完成后的删除/归档候选

这些是目标候选，不是本阶段可执行删除项；每一项都需要先满足“没有 active consumer、
没有回滚用途、测试和 CI 已迁移、历史保留策略已确认”的门槛：

| 候选 | 删除/归档前置条件 |
|---|---|
| `CommunityOperationsAssistant`（`packages/.../agent.py`）及 `routes.py:1032` Legacy 分支 | 所有 HTTP、integration、revision/compat 测试改为 Runtime；Legacy 开关和部署回滚窗口结束 |
| `runtime_router.py` 的 Legacy 分支及 `ASSISTANT_EXECUTION_MODE=legacy` 配置 | Runtime-only 已被监控验证，且无回滚要求 |
| active `/runs` 控制/SSE 路由（不是整个 `routes.py`） | `zhiguang-fe` 改为 execution API，历史 Run 只读查询有独立合同 |
| `run_store` 的执行态投影代码 | 前端不再用 `/runs`，兼容响应不再需要执行状态投影 |
| root `community-assistant-agent/` | 部署、CI、脚本、外部调用确认迁移到 `apps/assistant_api`，并保留 `archive/legacy` 所需历史材料 |
| root `creator-agent/` | 与 `apps/creator-agent`/外部部署的 API、数据、CI 合同完成核对后，才可归档或删除重复实现 |
| `moderation-agent/` | 只有在确认审核服务已有替代部署且 CI/运行环境不再依赖后，才可单独退役；它不是 Assistant Runtime 清理项 |

`zhiguang-fe` 本身目前不是删除候选：它是当前唯一实际前端。只有在新的
`apps/frontend`（或明确的替代仓库）完成构建、发布和运行验证后，才可以重新评估旧前端目录。

### 5.4 分类 C：兼容层暂留

- `assistant_runs` 表、`LegacyRunHistoryRepository`/`RunRepository`、历史 projection
  migration：只能保存对话回合/历史引用，禁止重新成为 Runtime status source；
- `run_store` 的只读/响应兼容部分，以及旧 `/runs/{run_id}` 查询：当前前端仍需要，
  但应逐步降为 history-only；
- `RunExecutionAdapter`、`ExecutionReference`、`run_execution_link`：只做标识映射，
  不应承载 steps/events/status/control；
- `run_id` 字段、`RunAcceptedResponse` 的兼容字段、旧消息/审批字段：在前端完成
  execution identity 迁移前保留；
- `packages/assistant_core/.../compatibility/history` 和
  `compatibility/intent`：前者是历史桥，后者仍被 `TaskUnderstanding` L2 使用，
  不能按“目录名为 compatibility”整体删除；
- `archive/legacy/community-assistant-agent`、旧 migrations 和兼容测试：作为历史和
  回滚材料保留；
- `zhiguang-be` 中 `assistant_run_id` provenance：待 Java 数据/审计合同完成迁移后再评估。

## 6. 架构文档与源码的差异

以下文档的“已完成”描述与当前工作树不一致，应在后续收口时更新或标注为历史快照：

1. `docs/architecture/PHASE_11_6_D4_C2_FRONTEND_EXECUTION_MIGRATION.md` 声称
   `apps/frontend/src/services/executionService.ts` 和 `TaskCenterPage -> GET /executions`
   已存在；但当前 `apps/frontend` 不存在，`zhiguang-fe` 仍调用 `listRuns()`。
2. `docs/architecture/PHASE_11_6_D4_A_CONSUMER_MIGRATION.md` 声称 ACTIVE consumer
   已以 `execution_id` 为主；当前 AssistantPanel、CommentSection、TaskCenter 的代码仍
   以 `run_id` 为主。
3. `docs/architecture/PHASE_11_6_D4_C4_LEGACY_OPERATION_REMOVAL.md` 声称没有 `/runs`
   control/event route；当前 `apps/assistant_api/.../api/routes.py` 仍有上述路由。
4. `docs/architecture/PHASE_11_6_D_RETIREMENT_PLAN.md`、
   `docs/architecture/LEGACY_AUDIT.md` 关于“Runtime canonical、Legacy 先保留到消费者
   迁移后”的原则，与当前源码更吻合，可作为下一阶段门禁依据。

因此，Phase 文档不能单独作为“已完成清理”的证明；本报告按源码、路由注册和前端实际
调用为准。

## 7. 当前阶段判定与后续门禁

**判定：Runtime 后端迁移/E2E 已完成，Frontend + Legacy 收口尚未完成，当前为混合过渡态。**

下一阶段应按以下顺序建立证据（本报告不执行这些改动）：

1. 在实际 `zhiguang-fe` 增加 execution 类型/service，先读取 `/executions`、steps、events
   和 Runtime stream，同时保留 run 兼容字段；
2. 统一 approval/retry/resume/cancel 的 execution 合同，并补齐前后端路径测试；
3. 将 Task Center、AssistantPanel、CommentSection 的 ACTIVE 状态源切到
   `execution_id`/Execution API；
4. 对 `/runs` 和 `assistant_runs` 做消费者、历史保留、CI、Java provenance 四方核对；
5. 只有上述门禁全部通过，才进入 Legacy 分支关闭和目录 cleanup。

## 8. 审计验证记录

- `Test-Path apps/frontend`：`False`。
- `rg` 扫描确认 `zhiguang-fe/src/services/assistantService.ts`、
  `AssistantPanel.tsx`、`CommentSection.tsx`、`TaskCenterPage.tsx` 的 Run 调用。
- `rg` 扫描确认 active `routes.py` 的 `/runs`、`CommunityOperationsAssistant` 和
  Runtime routes 的 `/executions` endpoints。
- 未运行业务测试，因为本阶段要求只读审计；没有任何生产代码改动。
- 工作区原有两个不相关的未跟踪异常文件未触碰：
  `tash show --name-only …stash@{0}… findstr intent`、`tore .dir`。
