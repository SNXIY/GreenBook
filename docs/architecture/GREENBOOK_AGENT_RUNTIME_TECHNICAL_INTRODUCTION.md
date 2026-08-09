# GreenBook Agent Runtime 项目技术介绍

> 本文基于当前仓库代码、`docs/architecture/` 架构记录和 Phase 11.6 迁移记录编写。文中的“当前”指 ACTIVE Runtime 代码路径；历史 Agent、旧目录和旧状态模型只在迁移背景或兼容边界中出现。

## 一、项目背景与定位

GreenBook 是面向社区运营场景的 Agent Runtime。它服务的不是一次性的问答，而是包含查询、分析、内容生成、修改、审批和外部系统写操作的业务任务。例如，用户希望系统分析近期帖子表现、总结原因、生成运营建议，或者基于已有内容创建草稿并在确认后发布。这类请求天然具有多步骤、长耗时、可暂停、可重试和需要审计的特征。

因此，GreenBook 的核心问题不是“如何让模型生成更像人的回复”，而是如何把自然语言目标变成可验证、可执行、可观测、可恢复的任务。主链路是：

```text
用户请求
  -> Intent Understanding
  -> IntentSpec / TaskIntent
  -> Planner
  -> TaskPlan
  -> PlanValidator
  -> PlanExecution(execution_id)
  -> ExecutionStateManager / ExecutionWorker
  -> ToolRuntime / MCP capability
  -> Execution result、事件和用户反馈
```

普通 LLM Chat 通常以请求为边界：模型返回文本，工具调用和中间状态散落在对话上下文或应用代码中。它难以表达“已经完成哪些步骤”“当前等待什么”“某一步失败是否能安全重试”“用户暂停后怎样恢复”。GreenBook 把这些问题提升为 Runtime 的显式生命周期，并将执行事实从历史展示数据中隔离出来。

当前职责分层如下：

| 分类 | 当前组件 | 职责 |
|---|---|---|
| ACTIVE Runtime | `PlanExecution`、`ExecutionStateManager`、`ExecutionEventStore`、`ExecutionWorker`、`RuntimeManager`、`ToolRuntime` | 执行实例、状态迁移、事件、步骤执行、控制和受控工具调用 |
| COMPATIBILITY History | `RunExecutionLink`、`ExecutionReference`、`LegacyRunHistoryRepository`、`assistant_runs` | `run_id` 历史查询、标识映射和 Legacy history projection |
| ARCHIVE Legacy | `archive/legacy/`、`docs/archive/`、历史迁移报告 | 历史实现和审计证据，不是当前 Runtime 能力 |

## 二、整体系统架构

### 2.1 端到端架构

```text
React Frontend / Execution Console
          |
          v
      Assistant API
          |
          +--> JWT/AuthContext、conversation、chat API
          |
          v
  Intent Understanding + Task Decomposer
          |
          v
       Planner / TaskPlan
          |
          v
       PlanValidator
          |
          v
  PlanExecution(execution_id)
          |
          +--> ExecutionStateManager --> ExecutionRepository
          |                                  |
          |                                  +--> PostgreSQL adapter 可用
          |                                  +--> 当前默认内存实现
          |
          +--> ExecutionEventStore --> SSE / timeline
          +--> CheckpointStore、Recovery、Lease、Retry
          |
          v
     ExecutionWorker
          |
          v
   CapabilityExecutor
          |
          v
       ToolRuntime
          |
          v
   In-process MCP adapter / Tool Registry
          |
          +--> Java Community Backend
          +--> Creator Service
```

### 2.2 目录结构与模块归属

`apps/` 是可运行应用边界：

- `apps/assistant_api/`：FastAPI Assistant API，负责认证、请求入口、Runtime 查询和控制接口，并组装 LLM、Java client、Creator client 与 MCP server。
- `apps/assistant_worker/`：异步 Worker 应用入口。当前代码是保持进程运行并持有 Java client 的骨架，Kafka consumer 和定时任务仍是待接入位置，不能描述为已经完成的 Runtime 调度集群。
- `apps/frontend/`：React/Vite 前端，包含任务中心、执行详情和 Execution Console。
- `apps/backend/`：Java 社区业务后端，使用 MySQL、Redis、Kafka 等基础设施承载业务数据和业务 API。
- `apps/creator-agent/`：独立 Creator 服务，负责创作工作流；它是当前产品组成部分，但不是 Assistant Execution Runtime 的状态源。

`packages/` 是可复用领域能力：

- `assistant_core/`：意图理解、任务分解、规划、执行模型、状态管理、事件、恢复、重试、工具 Runtime、审计、记忆和数据库适配器。
- `contracts/`：跨服务身份、错误、工具结果和业务事件契约。
- `java_client/`、`creator_client/`：对 Java 后端和 Creator 服务的 HTTP 客户端，集中处理请求模型、认证和错误映射。
- `security/`：JWT、JWKS、`AuthContext` 和审批相关安全能力。
- `observability/`：trace 与采集模型；`evaluation/`：Intent、Planner、Runtime 和 badcase 评估。

`services/` 提供独立服务能力：

- `services/greenbook_mcp/`：MCP 风格的业务工具服务和 Tool Registry。Assistant 通过 MCP adapter 调用社区、内容、互动和分析能力。
- `services/creator_agent/`：Creator 相关服务边界。

`contracts/` 目录包含 OpenAPI 和事件契约，例如 `assistant-openapi.yaml`、`java-openapi.yaml` 和 Kafka 业务事件模型。它解决的是服务之间的契约稳定性，不承担 Runtime 状态持久化。

`scripts/` 按用途分为 `dev/`、`verify/` 和 `ops/`。启动、E2E、smoke、Runtime report、JWT key 和环境导入脚本属于工程流程；`ops/promote-admin.ps1` 是运维入口。脚本不是业务 Runtime 的实现，不应成为状态来源。

`docs/architecture/` 保存当前架构、Execution Console 设计、`run_id -> execution_id` 迁移、assistant_runs projection contract、资源审计和兼容边界报告。Phase 11.6 的核心结论是：Runtime execution state 与 Legacy history projection 必须分离。

## 三、Agent Runtime 核心设计

### 3.1 为什么需要 Execution Runtime

传统 Agent loop 把“模型思考、工具调用、结果拼接”放在一次 HTTP 请求中，至少有四个结构性问题：

1. 请求即执行，缺少明确的创建、运行、等待、完成和失败生命周期。
2. 长任务中途断开后，无法可靠区分已完成步骤和正在执行步骤。
3. 外部工具失败后，应用只能重新跑整轮，容易产生重复副作用。
4. 状态可能同时存在于对话、`assistant_runs`、内存对象和日志中，查询结果不一致。

GreenBook 用 `PlanExecution` 表示一个 `TaskPlan` 的执行实例。其代码模型包含：

- `execution_id`：Runtime 的主执行键；
- `plan_id`、`task_id`：关联计划和业务任务；
- `status`、`current_step_index`：执行级生命周期；
- `steps: list[StepExecution]`：每一步的 capability、依赖、状态、重试次数、错误和 artifact；
- `requires_approval`、`has_side_effects`：执行风险和人工确认元数据；
- `created_at`、`updated_at`、`completed_at`、`version`：时间和乐观版本信息。

事件和 checkpoint 在实现上由 `ExecutionEventStore` 与 `CheckpointStore` 独立保存，不是 `PlanExecution` Pydantic 对象中的内嵌 `events` 字段。架构上它们共同构成一个 execution aggregate：状态由 `PlanExecution` 管理，事件由 EventStore 管理，恢复快照由 CheckpointStore 管理。

### 3.2 状态迁移边界

`ExecutionStateManager` 是状态变化的唯一入口。它实现创建执行、启动、完成步骤、失败、显式重试、暂停、恢复、取消、审批等待和崩溃恢复等操作。API 通过 `RuntimeManager` 和 `RetryManager` 调用它，而不是直接修改 repository。

主要状态包括 `PENDING`、`RUNNING`、`PAUSED`、`WAITING_APPROVAL`、`WAITING_HUMAN`、`COMPLETED`、`FAILED` 和 `CANCELLED`；步骤状态包括 `PENDING`、`RUNNING`、`WAITING_APPROVAL`、`COMPLETED`、`FAILED_RETRYABLE`、`FAILED` 和 `SKIPPED`。状态迁移带有前置状态校验，非法迁移返回错误而不是静默覆盖。

## 四、任务执行生命周期

以“帮我分析最近帖子表现，并生成运营建议”为例：

### 4.1 意图理解

Assistant API 先通过认证后的用户上下文创建 `RuntimeContext`，将用户消息、会话、租户、`trace_id` 和 LLM client 传给 `TaskUnderstanding`。理解层将自然语言转为结构化 `TaskIntent`/`IntentSpec`，包括目标类别、需求、资源引用和约束。`TaskDecomposer` 负责把复合请求拆成可排序的子任务，并在目标不明确时触发 clarification。

### 4.2 规划与校验

`TaskOrchestrator` 基于目标类别和 capability registry 生成 `TaskPlan`。计划中的 `PlanStep` 描述 step id、capability、顺序、依赖、输入 artifact 类型和输出 artifact 类型。

`PlanValidator` 把 `TaskPlan` 转成 `ExecutablePlan`，校验 capability 是否存在、依赖和 artifact 类型是否匹配，以及是否包含需要审批的副作用。只有有效计划才会进入 Runtime。

### 4.3 创建执行和 Worker 执行

`ExecutionStateManager.init_execution()` 从校验后的计划创建 `PlanExecution` 和对应 `StepExecution`，生成 `execution_id`。`ExecutionWorker` 按 ordinal 和 dependency 读取待执行步骤，调用 `CapabilityExecutor`，在每个步骤前后更新状态并产生事件。

查询、分析类步骤可以先调用 analytics/community 工具，再将输出封装为 artifact；后续步骤通过 artifact handle 使用前序结果，避免把大量原始工具输出直接塞回模型上下文。

### 4.4 工具调用与结果反馈

`CapabilityExecutor` 将计划步骤转换成 `ToolInvocationContext`，再委托 `ToolRuntime.invoke()`。ToolRuntime 负责 invocation id、幂等 ledger、超时和审计 trace；MCP server 的 handler 执行具体社区业务调用。最终 RuntimeResult 由 API 返回给客户端，并保留 `execution_id`、trace 和错误信息用于后续查询。

### 4.5 同步、异步、失败与恢复

当前 `RuntimeAgentService.execute()` 是一次请求内完成规划和 Worker pass 的异步调用；当遇到审批或人工澄清时返回等待结果，并由 Human Interaction 记录继续处理所需上下文。独立 Runtime API 可以按 `execution_id` 查询状态、步骤和事件，前端通过 SSE 订阅变化。

失败时，`ExecutionStateManager.fail_step()` 根据 `max_retries` 将步骤置为 `FAILED_RETRYABLE` 或 `FAILED`。显式 retry 会把失败步骤重置为 `PENDING`；`recover_execution()`/`resume_execution()` 会把进程中断时的 `RUNNING` 步骤恢复为可执行状态，并保留已完成步骤，避免从头执行整个计划。实际生产部署仍需把持久化 repository、事件 store、lease 和 worker 调度完整接入应用生命周期。

## 五、多 Agent / Tool 设计

### 5.1 Registry 与 capability

当前核心执行模型以 capability 为计划和执行单位。`CapabilityRegistry` 注册可执行能力，`CapabilityMapper` 根据目标类别选择能力，`TaskOrchestrator` 只把已注册能力写入计划。MCP 服务侧的 Tool Registry 进一步给每个工具定义名称、参数模型、描述、分类和风险等级，并在启动/测试时校验 handler 签名与参数模型一致。

这是一种“规划层选择 capability、执行层解析 tool”的分离：模型可以参与意图和计划生成，但不能绕过计划直接调用任意 HTTP endpoint。项目的 Agent 协作重点也不是让多个 Agent 自由互相调用，而是通过任务分解、GroupExecutor、能力注册和受控执行形成可观测的协作链路。

### 5.2 为什么 Tool 不直接暴露给模型

直接把业务函数暴露给模型会让模型同时承担权限判断、参数构造、重试和副作用控制，失败时无法判断请求是否已经送达。GreenBook 将这些职责集中到 ToolRuntime 与 MCP 边界：

- 参数模型校验，拒绝字段漂移和非法输入；
- `AuthContext` 传递用户、租户和身份信息，权限由服务端判断，模型不能自行提供身份；
- capability 和 Tool Registry 的 allow-list，工具默认拒绝；
- risk/side-effect 标记，高风险动作可进入 approval；
- 每次调用有 invocation/tool-call id、幂等键和 `ToolExecutionLedger` 审计记录；
- timeout、retryable、request_sent 和依赖不可用错误被结构化返回；
- MCP server 统一映射 Java/Creator 等下游错误，避免模型把网络异常当成业务成功。

`ToolRuntime` 的超时和 ledger 是调用级可靠性；`ExecutionStateManager` 的步骤状态和 retry 是执行级可靠性，二者不能混为同一层。

## 六、Runtime 与 Legacy 架构演进

迁移前，旧请求通常以 `run_id` 为主，由 Legacy Agent 和 `RunRepository` 写入 `assistant_runs`。该模型把对话历史、工具轮次、状态和错误放在同一个旧记录里，难以表达独立步骤、事件顺序、checkpoint 和恢复语义。

迁移后的边界是：

```text
ACTIVE Runtime
execution_id -> PlanExecution
             -> ExecutionStateManager
             -> ExecutionEventStore

COMPATIBILITY History
run_id -> RunExecutionLink -> execution_id (如果已映射)
run_id -> LegacyRunHistoryRepository -> assistant_runs
```

`RunExecutionLink`/`ExecutionReference` 只负责 identifier mapping 和 history lookup。映射存在时，Runtime status、steps、events 必须分别从 Execution API/PlanExecution、StateManager、EventStore 读取；映射不存在时，才允许把旧记录作为 Legacy-only history 返回。它不是新的执行状态模型。

没有直接删除 `assistant_runs`，是因为历史数据、旧 API 消费者和回滚/保留要求仍然存在。通过先冻结 Runtime 写入、再建立 projection contract、重组 History compatibility、最后保留兼容 alias，可以把数据保留与执行模型迁移解耦，降低一次性删除带来的不可逆风险。

## 七、assistant_runs Projection 设计

`assistant_runs` 的最终职责是 Legacy History Projection，不是 Runtime database。Runtime-backed projection 只允许保存历史识别和展示所需的 metadata：

- `run_id`；
- `conversation_id`；
- `user_id`；
- `tenant_id`；
- `content`；
- `trace_id`。

Runtime projection 禁止写入 `status`、`events`、`error_code`、`error_message`、`tool_rounds`、`partial_results`、`progress`、`current_step`、`execution_state`、`checkpoint` 和 `retry_state`。即使传入 `status="UNKNOWN"`、`status="RUNTIME_BACKED"` 或 `events=[]` 这样的 fake value，也属于违反契约，而不是“无害默认值”。

`LegacyRunHistoryRepository.create()` 和 `update()` 要求调用者显式传入 `_legacy_projection`：`False` 表示 Runtime projection，`True` 才允许旧字段。代码通过 `validate_run_projection_fields()` 拒绝跨边界字段。`RunRepository` 仍保留为兼容别名，但新代码应使用语义更明确的 `LegacyRunHistoryRepository`。

这条规则的核心收益是避免双写状态：同一个执行不能一边由 StateManager 更新，一边由旧 repository 写入另一份 status/events。出现差异时，`execution_id` 对应的 PlanExecution 和 EventStore 是唯一 Runtime 事实；assistant_runs 只用于历史兼容。

## 八、可靠性设计

### 8.1 状态和版本

StateManager 对执行和步骤执行做显式 transition check。`StepExecution.version`、`PlanExecution.version` 和 repository 的更新逻辑为并发/重试提供版本基础；PostgreSQL 适配器以 execution 行和 step 行保存状态快照。

### 8.2 Event 与 SSE

`ExecutionEventStore` 保存 `EXECUTION_STARTED`、`STEP_STARTED`、`STEP_COMPLETED`、`STEP_FAILED`、`STEP_RETRY_REQUESTED`、`APPROVAL_REQUIRED`、暂停/恢复/取消和终态事件。API 的 `/executions/{execution_id}/events` 直接读取 EventStore，`/stream` 使用 `subscribe_execution_events()` 以 SSE 输出。事件用于可观测时间线和客户端增量刷新，不取代当前状态快照。

### 8.3 Retry、checkpoint、recovery

步骤失败根据 retry budget 分成可重试和永久失败；`RetryManager` 只允许对失败步骤发起显式重试。Checkpoint 保存已完成步骤、当前步骤和快照；恢复逻辑把异常中断的 RUNNING 步骤回到 PENDING，而不是重复认定为成功。工具层还通过 ledger 记录请求是否已发出，避免在不确定的网络失败后盲目重复有副作用调用。

### 8.4 Pause、resume、cancel 和 Human in the loop

用户暂停只改变 execution 状态，Worker 在执行边界通过 Runtime guard 判断是否继续。恢复时保留已完成步骤，重新调度待执行或可重试步骤。取消是终态操作。高风险 capability 可以进入 `WAITING_APPROVAL`；需要用户补充信息的资源/引用解析则由 Human Interaction Manager 生成澄清请求，使用 `execution_id` 关联后续处理。

这些机制共同解决长任务的三个问题：状态可查询、失败可定位、恢复可从步骤边界继续。它们并不意味着所有外部工具都天然 exactly-once；外部副作用仍依赖下游幂等契约和 ToolRuntime ledger。

## 九、API 设计

当前 Assistant API 的 Runtime 路由位于 `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`：

| 方法 | 路径 | 数据来源/动作 |
|---|---|---|
| `GET` | `/executions` | 列出经过授权的 Runtime execution，支持 cursor 分页 |
| `GET` | `/executions/{execution_id}` | 读取状态、当前步骤和进度 |
| `GET` | `/executions/{execution_id}/steps` | 读取 StateManager 管理的步骤快照 |
| `GET` | `/executions/{execution_id}/events` | 读取 EventStore 事件 |
| `GET` | `/executions/{execution_id}/stream` | SSE 订阅 Runtime 事件 |
| `POST` | `/executions/{execution_id}/pause` | StateManager pause |
| `POST` | `/executions/{execution_id}/resume` | StateManager resume |
| `POST` | `/executions/{execution_id}/cancel` | StateManager cancel |
| `POST` | `/executions/{execution_id}/steps/{step_id}/retry` | RetryManager 重试步骤 |

所有 Runtime 路由先经过 JWT/JWKS 认证；控制操作还要求 `auth_context` 和可选的 execution authorizer。`execution_id` 比 `run_id` 更准确，因为它明确指向计划执行实例，能承载 step、event、checkpoint、lease 和控制操作。`run_id` 只在兼容 API 和 History link 边界使用。

当前路由集合未提供一个独立的“创建 execution”控制 API；执行通常从 Assistant 请求链路进入 RuntimeAgentService，再由 Worker 初始化 PlanExecution。审批业务接口由 Human/approval 相关服务负责，不应把旧 run status 当作审批状态源。

## 十、前端执行控制台

`apps/frontend/` 是 React/Vite 应用。Execution Console 的实现集中在：

- `src/services/executionService.ts`：以 `execution_id` 调用列表、详情、步骤、事件、控制、重试和 SSE 接口；
- `src/hooks/useExecutionConsole.ts`：并行加载快照/步骤/事件，消费 SSE，按 event 去重并更新 reducer；连接中断时指数退避重连，终态后关闭连接；
- `src/pages/executions/ExecutionDetailPage.tsx`：展示状态、当前步骤、进度、控制按钮和人工等待提示；
- `src/components/execution/ExecutionTimeline.tsx`：展示步骤时间线、错误、重试次数和事件时间线。

前端采用“初始快照 + SSE 增量”模式，而不是只依赖事件重放。快照保证刷新和断线重连后能恢复完整视图，SSE 负责低延迟变化；状态控制仍由后端鉴权和 StateManager 决定，前端按钮只是命令入口。

## 十一、数据存储设计

### 11.1 PostgreSQL

Assistant Core 的 repository 层使用 SQLAlchemy/asyncpg 访问 PostgreSQL，保存会话、消息、任务、Legacy history、审批等 Assistant 数据。Execution 模块另外提供 `PostgresExecutionRepository`、`PostgresExecutionEventStore` 和 `PostgresCheckpointStore`，分别保存 execution/steps、事件和 checkpoint。它们的表结构位于 `execution/persistence.py`。

需要区分“适配器已实现”和“运行时已接线”：当前应用启动代码初始化的是 Assistant DB session/table；`RuntimeAgentService` 和 runtime route 的默认 manager 仍可回退到内存 `ExecutionRepository`/`ExecutionEventStore`。因此 PostgreSQL Execution 持久化是可用基础设施，但当前代码尚未证明所有线上 Runtime 请求都已使用它。

### 11.2 MySQL

Java 后端使用 MySQL 保存 GreenBook 社区领域数据，例如用户、帖子、评论、关系和业务配置。Assistant 不直接拥有这些事实，而是通过 `JavaClient`/MCP capability 调用 Java API。MySQL 是业务数据源，不是 Assistant execution state store。

### 11.3 Redis

Redis 在 compose 中作为共享缓存/限流和服务侧短期状态基础设施，Java 使用独立 database index；Assistant 配置 `ASSISTANT_REDIS_URL` 使用 DB0。它适合低延迟、短生命周期或分布式协调数据，不替代 PlanExecution 的持久化状态和 EventStore 事实。

### 11.4 Qdrant

Qdrant 用于 Assistant 的 semantic memory（可选）和 Creator 的向量记忆/检索。Assistant 配置了 memory collection 和 embedding provider；语义记忆用于上下文召回，不是执行状态或事件存储。

### 11.5 Kafka/Redpanda

Root compose 使用 Redpanda 兼容 Kafka 协议，Java 业务事件契约位于 `packages/contracts`/`contracts/events`。它适合跨服务业务事件和异步集成。Assistant Worker 的 Python 入口目前仍是 Kafka consumer 的骨架，不能把 Kafka 描述为当前已完成的 Assistant Runtime execution queue。

### 11.6 Creator 服务

Creator 使用独立 PostgreSQL、Redis、Qdrant 等配置，负责内容创作工作流和创作记忆。Assistant 通过 Creator client/MCP 调用它；Creator 的内部 checkpoint 或 task id 与 Assistant 的 `execution_id` 不能混用。

## 十二、项目技术难点

### 1. Agent 长任务状态管理

**问题：** 一次请求可能包含多个外部动作，HTTP 请求生命周期不足以表达执行进度。

**方案：** 用 PlanExecution/StepExecution 建立显式 execution 和 step 状态，由 StateManager 统一迁移。

**收益：** 能按 execution 查询当前步骤、完成比例、错误和终态，前端也有稳定控制键。

### 2. Runtime 与 Legacy 平滑迁移

**问题：** 旧 API 和历史数据仍以 run_id/assistant_runs 为中心，直接删除会破坏历史查询和回滚。

**方案：** 用 RunExecutionLink 做 identifier-only bridge，LegacyRunHistoryRepository 继续保留历史写读，Runtime 统一使用 execution_id。

**收益：** 执行模型可以演进，历史数据可以保留，兼容面不会重新成为 Runtime 状态源。

### 3. Execution 生命周期设计

**问题：** 计划创建、步骤依赖、审批、失败、取消和完成需要一致的状态规则。

**方案：** PlanValidator 先把 TaskPlan 转为 ExecutablePlan，StateManager 对每个 transition 做前置校验。

**收益：** 非法状态覆盖变成显式错误，生命周期行为可单测和审计。

### 4. 多轮任务与上下文管理

**问题：** 用户的“刚才那个帖子”“修改上一版草稿”等引用需要结合会话和最近任务解析，不能仅靠当前 prompt。

**方案：** TaskDecomposer、ReferenceResolver、ResourceResolver、Conversation/Memory store 分工处理任务分解、资源消歧和上下文召回。

**收益：** 复合任务可以拆解，歧义可以转为 clarification，而不是执行错误对象。

### 5. Tool 调用安全

**问题：** 任意模型生成的工具名、参数和身份都不能直接成为业务写操作。

**方案：** Registry allow-list、Pydantic 参数模型、AuthContext、risk/capability、MCP server、ToolRuntime timeout/ledger/audit 和 approval 边界共同约束调用。

**收益：** 工具契约可验证，副作用有风险分级，网络失败和业务失败不会混成成功。

### 6. 事件驱动执行观测

**问题：** 仅保存最终 status 无法支持实时界面、故障定位和执行回放。

**方案：** EventStore 以 execution_id 追加结构化 ExecutionEvent，API 同时提供历史 events 和 SSE stream。

**收益：** 前端可以快照加载后增量更新，事件是可审计的执行轨迹。

### 7. Projection 数据一致性

**问题：** Runtime status/events 若回写 assistant_runs，会形成第二套状态库和双写冲突。

**方案：** projection contract 只允许 history metadata；repository 强制显式 `_legacy_projection`，禁止 fake status/events 绕过。

**收益：** Runtime source of truth 可验证，历史表仍可兼容而不污染执行状态。

### 8. 兼容历史 API

**问题：** 外部调用者仍依赖 RunRepository 名称和 run_id。

**方案：** canonical `LegacyRunHistoryRepository` + deprecated `RunRepository` alias + History compatibility link。

**收益：** 保持 API behavior，同时把新开发者引导到正确的 history boundary。

### 9. 外部副作用的重试语义

**问题：** 网络超时不代表下游没有收到请求，整步重跑可能重复发布、回复或修改。

**方案：** ToolRuntime/ledger 记录 invocation、幂等键、timeout、retryable、request_sent；最终还依赖 Java/Creator 下游幂等契约。

**收益：** 可以区分“可安全重试”和“请求状态未知”，降低重复副作用风险。

## 十三、项目亮点（简历版）

- 基于 `PlanExecution + ExecutionStateManager` 建立显式 Agent 执行生命周期，解决多步骤任务无法暂停、恢复和追踪的问题。
- 基于 `ExecutionEventStore + SSE` 实现执行事件时间线和前端增量更新，解决长任务黑盒执行问题。
- 基于 `TaskPlan -> PlanValidator -> ExecutablePlan` 建立计划校验边界，避免模型直接驱动未经验证的业务工具。
- 基于 Tool Registry、MCP adapter 和 `ToolRuntime` 实现参数校验、权限、超时、幂等 ledger、风险分级和审计链路。
- 基于 `RunExecutionLink` 完成 `run_id -> execution_id` History compatibility 映射，在保留历史数据的同时隔离 Runtime 状态。
- 基于 assistant_runs projection contract 限制 Runtime projection 字段，阻止 legacy history 重新演变为第二套 execution store。
- 基于步骤级 retry、checkpoint 和 recovery 设计失败恢复路径，使重试从可定位的步骤边界开始。
- 基于 React Execution Console 实现 execution list、步骤/事件时间线、SSE 重连及 pause/resume/cancel/retry 控制。

## 十四、面试讲解版本（约 5 分钟）

GreenBook 是一个面向社区运营的 Agent Runtime。用户可以用自然语言提出分析帖子、生成内容、修改草稿或执行运营动作等目标。这个项目的重点不是做一个只能返回文本的 ChatBot，而是把一个自然语言目标转换成结构化意图、经过校验的任务计划，再作为一个可观察、可暂停、可恢复的执行实例运行。

整体链路是 Frontend 到 Assistant API，API 做 JWT/JWKS 认证和上下文组装，然后进入 Intent Understanding 和 Task Decomposer。Planner 生成 TaskPlan，PlanValidator 检查能力、依赖、artifact 类型和副作用风险。校验通过后，系统创建 `PlanExecution`，生成唯一的 `execution_id`，由 ExecutionWorker 按步骤执行。每个步骤通过 CapabilityExecutor 进入 ToolRuntime，再通过 MCP adapter 调用 Java Community Backend 或 Creator Service。

Runtime 的核心设计是把 execution state 和 history metadata 分开。`PlanExecution` 保存执行级和步骤级状态，`ExecutionStateManager` 负责所有状态迁移，`ExecutionEventStore` 保存结构化事件，CheckpointStore 和 recovery 负责进程中断后的继续执行。状态可以是 RUNNING、PAUSED、WAITING_APPROVAL、FAILED 或 COMPLETED，失败步骤可以按 retry budget 重试，用户也可以按 execution_id pause、resume、cancel。前端先加载 execution snapshot、steps 和 events，再通过 SSE 获取增量事件，因此刷新和断线后仍能恢复视图。

迁移上，旧系统的 `run_id`、`RunRepository` 和 `assistant_runs` 曾经同时承载历史和运行状态。我们没有直接删除历史表，而是把它收敛成 Legacy History Projection。现在 `RunExecutionLink` 只做 `run_id` 到 `execution_id` 的标识映射；如果存在映射，状态必须从 PlanExecution/StateManager/EventStore 读取；没有映射的记录才作为 Legacy-only history 读取。Repository 还要求显式声明 `_legacy_projection`，从代码层防止 status、events 和 retry 信息回写 assistant_runs，避免双状态源。

工具安全也是一个重点。模型不能直接调用任意业务函数。能力先由 registry 注册，参数由模型校验，调用经过 AuthContext、MCP、ToolRuntime 和 ledger；ToolRuntime 处理 timeout、幂等和审计，具有副作用的 capability 可以进入人工审批。这样模型负责理解和规划，系统代码负责权限、状态和副作用控制。

项目中我会特别说明一个工程事实：Execution 的 PostgreSQL repository、event store 和 checkpoint store 已经实现，但当前 Assistant 启动路径的默认 Runtime manager 仍可能使用内存 repository，独立 Worker 也还是 Kafka consumer 的骨架。因此，下一步不是继续堆概念，而是把持久化 Runtime store、lease、worker 调度和应用生命周期接线完成，并统一 `.env.example` 的 `ASSISTANT_DATABASE_URL`/代码读取名。这个边界说明了项目当前完成度，也避免把适配器存在误说成生产部署已经完成。

如果总结项目价值，我会说：GreenBook 把 Agent 从“模型驱动的一次调用”提升成“有计划、有状态、有事件、有控制、有兼容边界的业务执行系统”，同时通过渐进迁移保留历史 API，降低了 Runtime-only 架构落地的风险。

## 当前实现核对与已知差异

以下事项是根据当前代码发现的实现边界，不能被本介绍解读为已完成能力：

1. `PlanExecution` 模型本身不内嵌 `events` 和 `checkpoints` 字段；二者由 `ExecutionEventStore` 和 `CheckpointStore` 独立存储，架构图中的 aggregate 表示逻辑归属。
2. `PostgresExecutionRepository`、`PostgresExecutionEventStore` 和 `PostgresCheckpointStore` 已提供，但 `RuntimeAgentService` 创建 `ExecutionWorker` 时使用默认 `ExecutionRepository`，Runtime API 的 manager fallback 也使用内存 repository/event store。持久化适配器尚未在当前启动路径中得到完整证明。
3. `apps/assistant_worker` 当前是保持进程存活的骨架，代码注释把 Kafka consumer、Creator completion event 和 analytics job 标为后续接入点；Kafka 已在基础设施和契约层存在，但不能称为已完成的 Assistant execution queue。
4. `.env.example` 提供 `ASSISTANT_DATABASE_URL`，而 `apps/assistant_api/greenbook_assistant_api/main.py` 当前读取 `ASSISTANT_DB_URL`。这是配置命名不一致，部署前必须统一，否则 Assistant 可能落回默认数据库连接。
5. 当前运行入口仍保留 `ASSISTANT_RUNTIME_MODE=on/off` 和显式 Legacy compatibility 代码。本文将 Legacy 仅作为兼容边界介绍，不把它描述为当前 Runtime 能力；最终退休条件见 `PHASE_11_6_RUNTIME_ONLY_MIGRATION_PLAN.md` 和 `DEPRECATION_STATUS.md`。

## 代码与文档索引

- Runtime 模型：`packages/assistant_core/greenbook_assistant_core/execution/models.py`
- 状态迁移：`packages/assistant_core/greenbook_assistant_core/execution/state_manager.py`
- 事件与持久化：`packages/assistant_core/greenbook_assistant_core/execution/event_store.py`、`persistent_stores.py`、`persistence.py`
- Worker 和恢复：`execution/worker.py`、`recovery_service.py`、`retry_manager.py`
- Tool Runtime：`execution/runtime/tool_runtime.py`、`execution/capability_executor.py`
- Assistant Runtime pipeline：`apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`
- Runtime API：`apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`
- Frontend Console：`apps/frontend/src/services/executionService.ts`、`src/hooks/useExecutionConsole.ts`、`src/pages/executions/`
- History compatibility：`packages/assistant_core/greenbook_assistant_core/compatibility/history/`
- History repository：`packages/assistant_core/greenbook_assistant_core/db/repositories.py`
- 迁移依据：`docs/architecture/ACTIVE_ARCHITECTURE.md`、`RUN_TO_EXECUTION_MIGRATION.md`、`PHASE_11_6_D8_3_PROJECTION_CONTRACT_ENFORCEMENT.md`
