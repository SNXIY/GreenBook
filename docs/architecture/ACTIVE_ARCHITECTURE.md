# GreenBook Agent Runtime Active Architecture

本文档是 GreenBook Agent Runtime 当前正式架构入口。它定义主路径、兼容边界和模块职责；历史方案不应被视为新的生产设计。

## 1. 正式 Runtime 流程

```text
User Message
     |
     v
TaskUnderstanding
     |
     v
IntentSpec
     |
     v
IntentValidator / Targeted Repair
     |
     v
PlanningContext
     |
     v
Planner / TaskOrchestrator
     |
     v
TaskPlan / ExecutablePlan
     |
     v
PlanExecution
     |
     v
ExecutionStateManager
     |
     v
ExecutionWorker
     |
     v
CapabilityExecutor / ToolRuntime
     |
     v
MCP / Java Backend / Creator Agent
```

正式的意图到执行链路是：

```text
User
 -> IntentSpec
 -> Planner
 -> TaskPlan
 -> PlanExecution
 -> Worker
 -> ToolRuntime
```

### 1.1 Understanding

输入用户自然语言，输出结构化 `IntentSpec`。复杂请求使用 LLM Structured Output，并经过确定性 Validator 和 targeted repair。

`IntentSpec` 只描述用户想做什么，包括：

- mode
- goal
- actions
- resources
- conditions
- constraints

它不包含 step、DAG、dependency、execution order 或 tool selection。

### 1.2 Planner

输入 `IntentSpec` 和兼容用的 `TaskIntent`，通过 `PlanningContext` 生成 `TaskPlan`。Planner 负责能力映射、模板选择、步骤、依赖和执行顺序。

### 1.3 PlanExecution

Runtime 根据 `TaskPlan` 创建 `PlanExecution` 和对应的 `StepExecution`。执行过程中的状态、失败、暂停、恢复、重试和完成信息都归属于该 execution。

### 1.4 Worker

Worker 根据计划和步骤依赖执行 ready steps。每个 step 执行前检查 Runtime 状态，然后调用 CapabilityExecutor 和 ToolRuntime。Worker 不重新理解用户，也不重新生成计划。

### 1.5 ToolRuntime

ToolRuntime 位于模型与业务系统之间，负责受控工具调用、参数校验、超时、幂等、错误分类、审批信号和结果封装。

## 2. Legacy 边界

Legacy 代码可以继续作为兼容或回滚路径存在，但不属于当前正式 Runtime 设计。

### 2.1 Legacy Agent

Legacy Agent 包括：

- `greenbook_assistant_core/agent.py`
- `assistant_api/services/legacy_agent_service.py`
- `AssistantService` 中的 Legacy fallback
- `RuntimeRouter` 的 `LEGACY` 分支

它采用旧的单 Agent tool-calling loop，使用 `run_id`、会话状态和旧 approval/event 结构。除非明确处于兼容模式，否则新功能应接入正式 Runtime。

### 2.2 IntentDraft

`IntentDraft` 和 `IntentCompiler` 是早期的中间语义方案：

```text
User -> IntentDraft -> IntentCompiler -> IntentSpec
```

该方案不是正式 L2 主路径。它只允许作为历史兼容代码保留，不能重新接入 Direct IntentSpec 流程。

### 2.3 IntentElements

`IntentElements` 和 `IntentSpecBuilder` 是另一套早期中间表示：

```text
User -> IntentElements -> IntentSpecBuilder -> IntentSpec
```

它同样不属于正式理解链路。当前代码中仍保留相关方法、import 和单元测试，后续应通过 compatibility adapter 迁移，不能作为新的主路径使用。

### 2.4 旧 Run

旧 API 仍存在：

- `run_id`
- `RunResponse`
- `RunRepository`
- `assistant_runs`
- 旧的 in-memory run store

这些对象表示旧的对话回合或 API 兼容记录，不是新的 Runtime 状态源。新的执行状态必须使用 `execution_id` 对应的 `PlanExecution`。

## 3. 核心状态源

> `PlanExecution` 是唯一 execution source of truth。

执行状态只能通过 `ExecutionStateManager` 修改。其他模块只应读取状态或调用 StateManager 提供的控制接口。

### 3.1 状态对象

```text
PlanExecution
  ├── execution_id
  ├── plan_id
  ├── status
  ├── current_step_index
  └── steps: list[StepExecution]

StepExecution
  ├── step_id
  ├── capability
  ├── status
  ├── retry_count
  ├── error_code / error_message
  ├── input/output artifacts
  └── checkpoint_data
```

### 3.2 辅助存储不是状态源

以下组件不能替代 `PlanExecution`：

- Checkpoint：保存恢复快照；
- EventStore：保存事件历史；
- Trace：保存观测时间线；
- PostgreSQL Repository：持久化 PlanExecution 和步骤状态；
- Lease：控制 Worker 所有权；
- 旧 Run Repository：保存旧 API 回合记录。

它们都服务于 execution，但不拥有独立的执行状态语义。

## 4. 模块职责边界

| 模块 | 负责 | 不负责 |
|---|---|---|
| Understanding | 自然语言到 IntentSpec、意图校验和定向修复 | 生成执行步骤、DAG、工具调用 |
| Planner | IntentSpec 到 TaskPlan、能力映射、依赖和顺序 | 重新理解用户、直接执行工具 |
| PlanExecution / StateManager | execution 和 step 状态迁移、暂停、恢复、取消、失败和完成 | 选择业务策略、调用外部工具 |
| Worker | 按 TaskPlan 执行步骤、传递 Artifact、处理执行结果 | 修改 IntentSpec、重新规划、递归重试 |
| RuntimeGuard | 在执行边界检查 PAUSED、CANCELLED、WAITING 等状态 | 执行工具、修改业务数据 |
| ToolRuntime | 受控工具调用、参数、幂等、超时和错误结果 | 自主决定用户意图或计划顺序 |
| Human Interaction | 审批、澄清、用户输入和恢复信号 | 替代 Planner 或 Worker |
| Checkpoint / Recovery | 保存和恢复已完成步骤及可恢复失败 | 重新生成 TaskPlan |
| EventStore / Trace | 记录执行事件和诊断时间线 | 作为状态真相源 |
| Evaluation | 评估 Intent、Planner、Execution，保存 Badcase | 修改线上执行状态或重新生成计划 |

## 5. 正式 API 与可观测边界

Runtime API 面向 `execution_id` 提供：

- execution 状态查询；
- step 状态查询；
- execution event 历史；
- 基于 EventStore polling 的 SSE stream。

事件流可包括：

- STEP_STARTED
- STEP_COMPLETED
- STEP_FAILED
- STEP_RETRY_REQUESTED
- APPROVAL_REQUIRED
- EXECUTION_COMPLETED
- EXECUTION_FAILED

API 不应通过旧 `run_id` 推断新的 PlanExecution 状态。需要兼容时，应由明确的 adapter 完成映射。

## 6. 当前迁移状态

### ACTIVE

当前正式使用或应作为生产主路径维护的代码：

- `IntentSpec`
- Direct IntentSpec L2
- `IntentValidator`
- `IntentContextHint`
- `PlanningContext`
- `TaskPlan`
- `PlanExecution`
- `StepExecution`
- `ExecutionStateManager`
- `ExecutionWorker`
- `RuntimeGuard`
- Checkpoint / Retry / Recovery
- EventStore / Trace / Runtime API
- Capability Registry / CapabilityExecutor / ToolRuntime
- MCP、Java Client、Creator Client
- Runtime 和 Planner Evaluation

### COMPATIBILITY

当前仍需保留，但不应扩展为新架构的代码：

- `TaskIntent`
- `intent_compat.py`
- `run_id` 和旧 `RunResponse`
- `RunRepository`
- Legacy fallback adapter
- `IntentDraft`
- `IntentElements`
- 旧路径的 API/集成测试

### LEGACY

已经不属于正式架构，但仍可能被配置、回滚或测试使用：

- `greenbook_assistant_core/agent.py`
- `LegacyAgentService`
- `RuntimeRouter` 的 Legacy 分支
- `community-assistant-agent/`
- 未确认正式来源的旧 Creator Agent 实现

### ARCHIVE

当前不应再进入生产 import，迁移完成后可归档：

- 已验证不再被调用的 IntentDraft/IntentElements 实现；
- 已完成的 Phase 设计报告；
- 被另一套 Creator 服务替代的旧实现；
- 已确认不再部署的历史 Agent 服务。

当前 `ARCHIVE` 只表示目标状态，不表示本阶段已经执行了移动或删除。

## 7. 迁移规则

1. 先增加 compatibility adapter，再改变 import 路径。
2. 先迁移测试分类，再移除生产 import。
3. 每次迁移后运行 unit、integration、contract、evaluation 和 e2e 测试。
4. 不新增第二套 execution 状态模型。
5. 不让 IntentDraft 或 IntentElements 回到 L2 主流程。
6. 不在迁移任务中修改 Planner、Worker 或 Execution Runtime 的业务逻辑。
7. Creator Agent 必须先完成 API、部署和 migration 对照，才能决定保留哪一套实现。

