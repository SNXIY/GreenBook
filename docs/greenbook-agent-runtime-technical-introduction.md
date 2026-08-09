# GreenBook Agent Runtime 项目技术介绍

> 面向第一次接触项目的工程师。本文以当前代码为准，区分已实现能力、兼容路径和后续方向。

## 1. 项目概述

GreenBook Agent Runtime 是面向社区运营场景的任务型 Agent 平台。它把用户的自然语言目标转换为结构化意图，进一步生成可验证的任务计划，并在一个可暂停、可恢复、可重试、可观测、可持久化的执行 Runtime 中完成任务。

它解决的不是“让模型回答一句话”，而是让 Agent 可靠地完成一组带条件、顺序、外部副作用和人工审批的业务动作。例如，用户说：

> 搜索最近热门 Agent 文章，分析受欢迎原因，如果有旧文章就优化，没有就创建，发布前让我确认，确认后五分钟发布。

普通 ChatBot 通常只产生文本，或者让一次模型调用直接选择工具。这种方式难以表达条件分支，无法稳定追踪中间产物，也无法在发布前暂停并在稍后继续；网络超时、重复写入、进程重启和多 Worker 并发也会使结果不可控。

因此项目将职责拆开：Understanding 理解用户意图，Planner 生成执行计划，Runtime 管理执行生命周期，Worker 执行计划步骤，Tool Runtime 负责受控的外部调用，Evaluation 持续发现问题。

## 2. 项目整体架构

```text
用户 / GreenBook 前端
          |
          v
Assistant API（FastAPI、JWT、会话入口）
          |
          v
TaskUnderstanding（L1 路由 / L2 LLM）
          |
          v
IntentSpec
          |
          v
IntentValidator + Targeted Repair
          |
          v
PlanningContext
          |
          v
Orchestrator / Planner
          |
          v
TaskPlan / ExecutablePlan
          |
          v
ExecutionStateManager（PlanExecution 真相源）
          |
          +--> RuntimeGuard --> ExecutionWorker
          |                         |
          |                         v
          |                CapabilityExecutor / ToolRuntime
          |                         |
          |                         v
          |                MCP Adapter / Clients
          |                         |
          v                         v
Execution Repository、Checkpoint、Event Store   Java 社区后端 / Creator Agent
          |
          +--> Runtime API / SSE
          +--> AgentTrace / Evaluation / Badcase
```

| 模块 | 输入 | 输出 | 职责 |
|---|---|---|---|
| Assistant API | HTTP 请求、JWT | 认证后的任务请求或 Runtime 查询 | 对外入口和身份边界 |
| TaskUnderstanding | 用户消息、上下文 | `IntentSpec` | 提取目标、动作、资源、条件和约束 |
| Validator | 原始消息、IntentSpec | 结构化问题 | 检查条件、审批、时间和空动作等确定性约束 |
| Planner | `PlanningContext` | `TaskPlan` | 选择模板、生成步骤、依赖和能力映射 |
| Execution Runtime | TaskPlan、控制命令 | `PlanExecution`、事件、Checkpoint | 管理状态、恢复、租约和可观测性 |
| Worker | 可执行计划、Runtime 状态 | Step 结果、Artifact | 按依赖执行步骤 |
| Tool Runtime | Capability、调用上下文 | 结构化 `ExecutionResult` | 参数、超时、幂等、错误和外部调用隔离 |
| 业务系统 | 工具请求 | 社区/内容数据 | Java 后端和 Creator Agent 是业务数据的来源 |

## 3. 一次完整 Agent 请求流程

以“搜索热门文章并在确认后发布”为例：

1. **自然语言理解**：API 将消息交给 `TaskUnderstanding`。简单请求可以走 L1；复合、条件、审批或时间请求进入 L2。
2. **生成 IntentSpec**：L2 使用结构化输出生成 `mode`、`actions`、`conditions` 和 `constraints`。该层只表达用户意图，不生成步骤或工具。
3. **校验与修复**：`IntentValidator` 检查复杂请求是否出现空动作、条件丢失、审批缺失或时间约束缺失；Targeted Repair 只补报错字段，并记录 trace。
4. **生成计划**：`PlanningContext` 同时保留旧的 `TaskIntent` 和 richer `IntentSpec`。Orchestrator 根据需求选择模板，生成 `TaskPlan`，例如搜索、分析、生成/优化、质量验证、定时发布及其依赖。
5. **创建执行**：执行层初始化 `PlanExecution` 和多个 `StepExecution`，由 `ExecutionStateManager` 负责状态迁移。
6. **执行前控制**：Worker 调度 ready step，在真正调用能力前经过 `RuntimeGuard`。只有 `RUNNING` 执行允许继续。
7. **产生中间结果**：搜索和分析结果以 Artifact 形式保存，后续步骤通过 ArtifactStore 获取，而不是依赖模型记忆上一轮文本。
8. **人工审批**：发布能力标记为高风险或需要审批。执行进入 `WAITING_APPROVAL`/`WAITING_HUMAN`，产生 `APPROVAL_REQUIRED` 事件；用户可通过交互接口作出决定。
9. **恢复与发布**：批准后由状态管理器恢复执行，Worker 从未完成步骤继续，按时间约束调用 `SCHEDULE_PUBLISH` 或 `PUBLISH_NOW`。
10. **运行可见性与恢复**：用户通过 Runtime API 查询状态、步骤和历史事件，通过 SSE 接收实时更新。超时等错误由 `RecoveryPolicy` 判断是否可重试；进程重启则利用持久化状态、Checkpoint 和 Lease 恢复。

这里的关键是：IntentSpec 保留“用户想做什么”，TaskPlan 保留“如何执行”，PlanExecution 保留“执行到哪里以及发生了什么”。三者不能混为一谈。

## 4. Agent Understanding 设计

### TaskUnderstanding

理解层采用 L1/L2 路由。L1 对明显简单的请求快速产出传统 `TaskIntent`；复杂请求交给 L2。L1 仍然有价值：低延迟、低成本、行为确定。但它不适合处理“多个动作 + 条件 + 审批 + 时间”的组合语义。

L2 的主架构是：

```text
自然语言 -> LLM Structured Output -> IntentSpec -> Validator -> Targeted Repair
```

模型被明确约束为意图抽取器，而不是回答用户、选择工具或生成执行计划。它在输出 JSON 前抽取动作、条件和约束，随后映射到固定 Schema。复杂请求使用上下文提示、adaptive token budget 和响应 trace，以降低长文本结构化输出被截断的风险。仓库仍保留早期 `IntentElements`/`IntentDraft` 相关兼容代码，但 Direct IntentSpec 是当前正式方向。

### IntentSpec

`IntentSpec` 的核心字段包括：

- `mode`：简单、条件等意图模式；
- `goal`：用户目标摘要；
- `actions`：`SEARCH`、`ANALYZE`、`CREATE`、`UPDATE`、`UPDATE_OR_CREATE`、`PUBLISH` 等；
- `resources`：内容、草稿、帖子、日程等资源类型；
- `conditions`：`IF_EXISTS`、`IF_NOT_EXISTS` 等条件；
- `constraints`：`APPROVAL`、`TIME`、`USER_INPUT` 等约束；
- `target_hint`、`confidence`、`source`：目标提示和来源信息。

Validator 输出 `EMPTY_ACTIONS`、`MISSING_CONDITION`、`MISSING_APPROVAL`、`MISSING_TIME_CONSTRAINT` 等结构化问题。Repair 接收原消息、现有 IntentSpec 和问题列表，遵循“只补缺失字段、保留已有语义”的原则。

理解和规划分离，是为了避免模型一次性决定业务语义、执行顺序和工具调用。这样可以独立评估抽取质量，也可以用确定性的 Planner 替换或演进执行策略。

## 5. Planner 设计

Planner 的输入优先是 `PlanningContext`。其中既保留 legacy `TaskIntent`，也保留可选的 `IntentSpec` 及其 actions、resources、conditions、constraints。旧请求没有 IntentSpec 时仍可以走兼容投影。

Planner 的职责是：

- 将意图映射为业务能力和计划模板；
- 实例化 `PlanStep`；
- 设置 `step_id`、顺序、依赖和输入输出 Artifact 类型；
- 校验能力名称和约束透传；
- 产出可执行的 `TaskPlan`/`ExecutablePlan`。

Planner 不负责自然语言理解、不负责重新解释用户、不负责决定最终工具参数，也不把 DAG、dependency 或 execution order 塞回 IntentSpec。

当前模板覆盖单步操作、研究后创建、创建并发布、研究后改进以及完整流水线。典型完整流水线为：

```text
SEARCH_COMMUNITY -> ANALYZE_CONTENT_PATTERNS
                  -> GENERATE_CONTENT / IMPROVE_CONTENT
                  -> VALIDATE_QUALITY -> SCHEDULE_PUBLISH
```

让 LLM 直接生成执行步骤会把理解和调度耦合在一起，带来依赖错误、能力越权、不可验证和难以重试的问题。Planner 则把步骤和依赖变成可测试的系统对象。

## 6. Agent Execution Runtime

普通调用链只关心一次函数调用是否返回；Agent Runtime 还要处理长任务、外部副作用、暂停、重试、重启和并发。因此执行层以 `PlanExecution` 为唯一状态真相源，以 `StepExecution` 表示每一步的状态。

### ExecutionStateManager

它是执行状态迁移的唯一入口，负责初始化、启动、完成、失败、暂停、恢复、取消、审批恢复、重试重置和崩溃恢复规范化。状态包括 `PENDING`、`RUNNING`、`PAUSED`、`WAITING_APPROVAL`、`WAITING_HUMAN`、`COMPLETED`、`FAILED`、`CANCELLED`。集中迁移避免 Worker、API 和恢复服务各自修改状态造成不一致。

### Worker

`ExecutionWorker` 从 Scheduler 找到依赖已满足的 ready steps，注入上游 Artifact，调用 `CapabilityExecutor`，处理成功、审批、可重试失败和永久失败。它不递归重启自己；重试由 RetryManager/下一次 Worker 入口驱动。每个 step 开始前经过 RuntimeGuard。

### RuntimeGuard

RuntimeGuard 是只读闸门：仅 `RUNNING` 允许执行；`PAUSED`、`WAITING_APPROVAL`、`WAITING_HUMAN`、`FAILED`、`CANCELLED`、`COMPLETED` 均阻止继续。暂停只改变 Runtime 状态，不粗暴杀死 Worker；Worker 在下一个执行边界检查状态。

### Checkpoint 与 Recovery

Checkpoint 保存 execution id、已完成步骤、当前步骤和快照。恢复时保留 `SUCCESS` 步骤，只将中断或符合策略的失败步骤恢复为可执行状态，不重新生成 Plan。它把“已经产生的业务结果”与“还需要继续的步骤”分开。

### Retry

`RecoveryPolicy` 由 error code 驱动。`TIMEOUT`、`NETWORK_ERROR`、`RATE_LIMIT`、`TEMPORARY_UNAVAILABLE` 默认可重试；参数错误、权限错误、资源不存在和业务错误通常不可重试。RetryManager 会检查次数、将失败步骤重置为 `PENDING`、保存 Checkpoint 并发出 retry 事件，避免把所有异常简单包装成 try/catch。

### Lease 与 Persistence

Memory Repository 用于本地测试；PostgreSQL adapter 持久化 execution、step、event、checkpoint 和 lease。`ExecutionLease` 以 `execution_id`、`worker_id`、`lease_until` 防止多个 Worker 同时执行同一任务。这里保留 `PlanExecution`/`StepExecution` 模型，没有另建 Run 状态体系。

### Event Stream 与 Runtime API

EventStore 记录创建、启动、step、审批、重试、完成、失败和取消事件。轮询式 async subscription 为 SSE 提供事件流，客户端断开或执行进入终态时结束。API 支持查询执行状态、步骤、事件，以及订阅 `/executions/{execution_id}/stream`。

## 7. Human-in-the-loop

人工介入适用于发布、删除、敏感内容修改等有外部副作用或高风险的能力。Capability Registry 标记风险级别和 `requires_approval`；Tool/Capability Executor 返回 approval-required 结果后，Runtime 将执行置于 `WAITING_APPROVAL` 或统一的人机交互状态，并发出 `APPROVAL_REQUIRED`。

`HumanInteractionRequest` 包含 execution、task、step、问题、选项、上下文和过期时间；用户回复 `ACCEPT`、`REJECT`、选择或输入后，交互管理器保存响应，Runtime 决定恢复或结束任务。`PAUSED` 表示用户主动暂停，`WAITING_APPROVAL`/`WAITING_HUMAN` 表示业务流程在等人，语义不同。

## 8. Tool Runtime 设计

LLM 不直接访问 Java REST 接口。能力层先通过 `CapabilityRegistry` 描述可用能力、输入、输出 Artifact、风险和工具映射；`CapabilityExecutor` 再把计划步骤转换为受控调用。

Tool Runtime/Invocation Context 提供：

- Tool Registry 和 capability-to-tool 映射；
- 输入校验与结构化 `ExecutionResult`；
- 超时元数据和错误码；
- 稳定的幂等键及 execution ledger；
- Artifact 输出和跨步骤传递；
- approval-required、retryable/permanent error 信号。

MCP server 是业务工具边界，负责认证上下文、参数检查和调用 Java/Creator client。Java 后端是社区业务数据的 source of truth；写操作要求幂等键，并通过明确错误码避免把“连接失败”误报成业务成功。

这层隔离避免模型绕过权限、误传用户身份、重复发布或在不知情时执行破坏性操作。

## 9. Evaluation 体系

Agent 质量不是“HTTP 返回 200”就能说明的。项目的 Evaluation 分为四层：

1. **Intent Evaluation**：检查 mode、action、resource、condition、constraint，以及复杂请求是否为空动作；支持 action coverage，而不只依赖 exact match。
2. **Planner Evaluation**：对已有 IntentSpec 和 TaskPlan 做确定性检查，评估 action coverage、resource match、依赖顺序合理性和约束透传，不重新生成 Plan。
3. **Execution Evaluation**：`ExecutionEvaluator` 读取 `ExecutionRecord`、ExecutionEvents、StepExecutions 和 Trace，计算成功、延迟、步数、重试、失败、人工介入、工具调用和质量分。
4. **Badcase Analysis**：失败案例保存用户输入、IntentSpec、TaskPlan、执行 trace、失败原因和期望行为，形成回归样本。

运行时指标包括 Execution Success Rate、Average Latency、Retry Rate、Failure Rate、Human Approval Rate 和 Tool Failure Rate。Evaluation 的意义是把“模型偶尔成功”转化为可定位的质量信号：可以判断问题发生在理解、规划、工具还是恢复，而不是只看最终自然语言。

## 10. 当前项目技术亮点

1. **LLM Structured Output**：让开放式语言理解落到可校验的数据契约。
2. **IntentSpec 抽象**：完整承载动作、资源、条件和约束，减少复杂语义在 legacy projection 中丢失。
3. **Understanding 与 Planner 解耦**：意图不携带 step/DAG，执行策略可以独立演进。
4. **PlanningContext 兼容层**：保留 `TaskIntent`，同时让 Planner 优先消费 richer intent，支持渐进迁移。
5. **模板化 Planner**：通过能力和模板生成可验证计划，降低 LLM 直接编排的风险。
6. **Execution State Machine**：集中管理暂停、审批、失败、恢复和终止状态。
7. **Step 级状态与 Artifact 传递**：中间结果可持久化、复用和诊断。
8. **Checkpoint Recovery**：重启或失败后跳过已完成步骤，避免重新执行副作用操作。
9. **Error-code 驱动 Retry**：区分临时故障与永久业务错误，控制重试边界。
10. **Human Approval**：把高风险动作变成显式状态，而不是隐藏在 Prompt 中。
11. **RuntimeGuard**：在工具调用前执行暂停/取消/等待状态检查。
12. **Execution Lease**：避免多 Worker 对同一 execution 重复执行。
13. **Event Stream + SSE**：用户能看到 step、审批、重试和终态，而非只看到最终结果。
14. **Badcase 回归闭环**：失败输入、计划和 trace 可保存，支持持续评测。
15. **业务工具边界**：MCP、Capability 和 Java source of truth 把模型与真实业务副作用隔离。

## 11. 技术栈

| 技术 | 作用 |
|---|---|
| Python 3.12+ | Agent Runtime、理解、规划、执行和评测主实现 |
| Pydantic 2 | IntentSpec、TaskPlan、Execution 和评测模型的 Schema/校验 |
| FastAPI / Starlette | Assistant API、Runtime 查询接口和 SSE |
| OpenAI-compatible client | 调用 DeepSeek 等结构化理解模型 |
| SQLAlchemy / asyncpg | PostgreSQL 连接与异步数据访问基础；执行持久化还提供同步 adapter 边界 |
| PostgreSQL | execution、step、event、checkpoint、lease 及业务持久化 |
| httpx | Java 后端与 Creator Agent 的 HTTP client |
| MCP adapter | 统一暴露社区、内容、发布、互动和分析能力 |
| Redis、Kafka/Redpanda、Qdrant | Docker 基础设施中可用的缓存/消息/向量能力，当前 Runtime 核心不把它们作为内存 EventStore 的必需依赖 |
| LangGraph | `creator-agent` 等相邻服务使用；当前 `assistant_core` 主 Runtime 采用轻量任务循环和显式状态模型 |
| Pytest / pytest-asyncio | 单元、集成、评测和契约测试 |

## 12. 项目目录说明

```text
packages/
  assistant_core/       Understanding、Planning、Execution、Capability、Artifact、Memory
  evaluation/           数据集、评测运行器、Runtime/Planner evaluator、Badcase、指标
  contracts/            ToolResult、ErrorCode、AuthContext 等跨包契约
  java_client/          Java 后端 HTTP client
  creator_client/       Creator Agent 任务 API client
  security/             JWT 校验和身份解析
  observability/        OpenTelemetry/可观测性相关组件

apps/
  assistant_api/        FastAPI 入口、认证 middleware、业务与 Runtime routes
  assistant_worker/     Kafka consumer 和定时任务等异步 Worker 入口

services/
  greenbook_mcp/        MCP server、Tool Registry 和具体业务工具
  creator_agent/        内容创作相邻服务

apps/backend/           Java 社区业务后端
apps/frontend/          React/Vite 前端
tests/
  unit/ integration/ contract/ evaluation/ e2e/
docs/
  reports/              各阶段设计、诊断和架构报告
```

旧的相邻 Assistant workspace 属于历史实现，不能与当前 `assistant_core` 主 Runtime 混为一套执行模型。

## 13. 和普通 Agent Demo 的区别

普通 Demo 通常是：

```text
User -> Prompt -> LLM -> Tool -> Text
```

GreenBook 是：

```text
User
  -> Understanding / IntentSpec
  -> Validator / Repair
  -> Planning / TaskPlan
  -> PlanExecution / StepExecution
  -> Guard / Worker / Tool Runtime
  -> State / Checkpoint / Retry / Lease
  -> Events / SSE / Evaluation
```

差异不在于“调用了更多工具”，而在于是否把 Agent 当成一个长生命周期、可能失败、带副作用的分布式任务。普通 Demo 难以回答“现在执行到哪一步、为什么停住、能否只重试失败步骤、重启后是否会重复发布”；GreenBook 将这些问题建模为状态、事件、检查点、租约和评测对象，因此更接近生产级 Agent Runtime。

## 14. 当前不足和未来方向

当前实现仍有明确边界：

- **多租户和权限体系**：API 已有 JWT/AuthContext 边界，但细粒度的 execution、resource、tool 权限和租户隔离仍需完整化。
- **Memory 深度**：目前支持 episodic、semantic、procedural 记录与关键词式 recall；尚未形成跨会话的语义检索和成熟的策略学习闭环。
- **双轨兼容**：旧 Agent、旧 Understanding 路径及部分遗留代码仍在仓库中，迁移和统一 wiring 需要继续收敛。
- **Persistence 集成**：PostgreSQL repository 已提供执行模型的持久化 adapter，但生产部署仍需统一异步连接、迁移、事务和 schema 生命周期。
- **Event 分发规模**：当前 SSE 基于 EventStore polling；大规模多实例场景需要可扩展的事件总线或订阅层。
- **Replay 与调试**：已有 Trace、事件和 Checkpoint，但完整的确定性 replay、时间旅行调试和副作用模拟仍是后续能力。
- **MCP 扩展**：可继续扩大 capability discovery、版本协商、权限声明和外部 MCP server 生态。
- **Evaluation 平台化**：目前已具备 evaluator、metrics 和 badcase 基础，后续可增加线上采样、版本对比、人工标注、回归门禁和自动告警。
- **复杂规划**：当前模板化 Planner 适合已知能力域；更开放的跨域任务需要更强的资源解析、计划验证和人工确认策略。

总体方向不是让一个更大的 Prompt 承担更多职责，而是继续保持“LLM 负责泛化理解、Schema 保证结构、Planner 负责计划、Runtime 负责可靠执行、Evaluation 负责反馈”的边界，并把每一层的可观测性和可恢复性做深。
