> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2
# Phase 3.5 Architecture Cleanup

## 1. Cleanup Goal

本阶段把 Phase 1-3 已建立的 Command、Goal、AgentLoop 边界落到生产代码：

- 每个用户消息只有一个 canonical Command/Goal/AgentLoop 入口。
- 旧 Intent、旧 Resolver、旧 Agent wrapper 不再承担默认生产职责。
- LLM 负责理解、拆解、规划、工具选择和反思；确定性 Runtime 负责权限、状态、队列、幂等、重试、Checkpoint、Ledger、持久化和副作用保护。
- 保留 Reliable Execution Layer，不重写 `execution/`、`artifact/`、Worker、Queue、Checkpoint、Ledger、ToolRuntime 或 MCP handler。

## 2. Architecture Before

此前生产代码同时存在：

```text
agent.py keyword/direct-tool route
conversation TaskCommand / Intent adapter
task IntentSpecProvider / TaskGraphBuilder
orchestration templates / TaskOrchestrator
agent_runtime executor wrappers
AgentLoop + Goal Runtime
```

它们共享部分模型但职责重叠。Resolver 也分散在 conversation、task 和 resource
目录，规划层还带有虚假的 `SearchAgent`、`CreatorAgent` 等 owner 信息。

## 3. Architecture After

```text
User/API
  -> command.CommandInterpreter
  -> command.Command
  -> command.target.TargetResolver
  -> goal.GoalDecomposer
  -> goal.GoalTree
  -> agent.AgentLoop (Observe -> Reason -> Act -> Reflect)
       |-> ToolSelector -> ToolMetadata -> ToolRuntime/MCP
       |-> GoalCompiler -> Task/Plan compatibility contract
  -> Reliable Execution Runtime
       -> Queue/Worker/Retry/Checkpoint/Ledger/Artifact
  -> external community systems
```

`GoalCompiler` 和旧 `TaskOrchestrator` 只编译 typed Goal/Plan，不重新理解用户。
`AgentLoop` 只决定下一步，不替代 Worker、Queue、Retry、Checkpoint 或 Ledger。

## 4. Final Module Responsibilities

| Layer | Canonical responsibility | Current entry |
| --- | --- | --- |
| Command | 理解本轮用户表达和控制语义 | `command/` |
| Goal | 表达用户目标、子目标、依赖和约束 | `goal/` |
| Agent | 运行时决策和反思 | `agent/` |
| Task | 持久工作单元和当前兼容生命周期 | `task/` |
| Plan | Goal 到可执行步骤的 typed projection | `goal/compiler.py` / `orchestration/models.py` |
| Execution | 可靠执行、状态、重试、幂等和证据 | `execution/` |
| Tool | 外部能力描述和调用边界 | `ToolMetadata` / `ToolRuntime` / MCP |
| Capability | 语义标签和检索索引 | `capability/` |
| Context | 当前会话、目标和工作集 | `context.py`, `conversation/` |
| Memory | 长期经验和偏好 | `agent_memory/` |
| Artifact | 跨步骤数据和 provenance | `artifact/` |

Capability catalog 仍服务于 Goal/Plan 的语义校验；具体工具的 schema、permission、
risk、approval、side effect、retry 和 cost 以 `ToolMetadata`/`ToolContract` 为描述
边界，MCP handler 仍是执行边界。

## 5. Dependency Rules

允许的单向依赖：

```text
API -> Command -> Goal -> Agent -> Task/Plan -> Execution -> ToolRuntime -> external systems
```

- `Execution` 不解析 Command，不读取用户原始文本。
- `Tool` 不调用 AgentLoop。
- `Planner` 不读取 API request，也不重新解释用户。
- `AgentLoop` 不直接写 Java Backend；写入动作经 ToolRuntime 或 Execution Runtime。
- `GoalDecomposer` 不调用 ToolRuntime。
- API 控制 payload 通过 typed Command adapter 投影，不能直接创建 Execution。
- Artifact、Queue、Worker、Checkpoint、Ledger 和 MCP 只通过已有 typed contract 通信。

## 6. Communication Contracts

- `Command`, `CommandTarget`, `CommandContext`：用户表达和结构化 target。
- `Resolved` / `Ambiguous` / `NotFound`：唯一 target resolution facade 的结果。
- `Goal`, `GoalTree`, `TaskNode`：LLM structured output 和 Goal 编译输入。
- `AgentState`, `AgentAction`, `Observation`, `Reflection`：AgentLoop 的状态机契约。
- `ToolMetadata`：Agent Intelligence 唯一的工具描述入口；不携带 handler。
- `TaskPlan`, `PlanStep`, `ConversationTaskGraph`：现有 Worker/Execution 的兼容投影。
- `Execution`, `Artifact`, `ToolResult`：Reliable Execution Layer 的持久和结果契约。

## 7. Deleted Files

### Runtime and fake Agent wrappers

| Path | 删除原因 | 原职责 | 替代模块 |
| --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | 默认入口已迁移，关键词路由和直连工具职责已失效 | 旧 direct-tool Assistant 和 routing hint | `agent/loop.py` + `command/` |
| `packages/assistant_core/greenbook_assistant_core/agent_runtime/__init__.py` | 无生产调用 | 旧 Runtime wrapper export | `execution/worker.py` + `agent/loop.py` |
| `packages/assistant_core/greenbook_assistant_core/agent_runtime/base.py` | 无生产调用 | 假 Agent 基类 | `agent/state.py` |
| `packages/assistant_core/greenbook_assistant_core/agent_runtime/executors.py` | ExecutionWorker 已直接持有 CapabilityExecutor | Agent executor wrapper | `execution/capability_executor.py` |
| `packages/assistant_core/greenbook_assistant_core/agent_runtime/runtime.py` | 无生产调用 | 旧 AgentRuntime loop wrapper | `agent/loop.py` |
| `packages/assistant_core/greenbook_assistant_core/orchestration/agent_registry.py` | fake Agent owner 和 executor registry 已无生产职责 | Search/Analytics/Creator/Publish/Quality Agent metadata | `CapabilityRegistry` + `ToolMetadata` |
| `apps/assistant_api/greenbook_assistant_api/services/group_executor.py` | 重复执行 wrapper，无独立 lifecycle/reasoning | Group Agent execution shim | `execution/worker.py` |

### Intent, Resolver and Resource duplicates

| Path | 删除原因 | 原职责 | 替代模块 |
| --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/conversation/target_resolver.py` | 第二套 Resolver 已被 canonical facade 替代 | conversation target matching | `command/target.py` |
| `packages/assistant_core/greenbook_assistant_core/task/reference_resolver.py` | 无生产调用，重复 target/reference matching | task reference resolver | `command/target.py` + Task evidence helper |
| `packages/assistant_core/greenbook_assistant_core/task/decomposer.py` | 旧 decomposer 不再是 Goal 入口 | 固定 Task 分解 | `goal/decomposer.py` |
| `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py` | 重复 Intent draft schema | Intent intermediate model | `command/models.py` / `goal/models.py` |
| `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py` | 重复 Intent element schema | Intent intermediate model | `Command` / `GoalTree` |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/adapter.py` | 无生产调用 | 旧 Intent adapter implementation | `command/adapter.py` |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py` | 无生产调用 | compatibility draft schema | `command/adapter.py` |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py` | 无生产调用 | compatibility element schema | `command/adapter.py` |
| `packages/assistant_core/greenbook_assistant_core/resource/__init__.py` | resource resolver package 无生产调用 | resource resolver export | `command/target.py` |
| `packages/assistant_core/greenbook_assistant_core/resource/models.py` | resource resolver package 无生产调用 | duplicate resource target models | `command/models.py` |
| `packages/assistant_core/greenbook_assistant_core/resource/resolver.py` | 无生产调用 | duplicate resource resolution | `command/target.py` |

### Retired tests

删除了只验证已退休设计的测试：旧 Agent runtime/direct assistant、旧 decomposer、
旧 reference/resource resolver、旧 multi-task resolver、旧 group executor，以及
compatibility Intent draft/elements tests。保留的 artifact、TaskGraph、Worker 和
conversation tests 已改为验证当前 typed contracts。

## 8. Renamed / Moved Files

| Before | After | Reason |
| --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/task/resolver.py` | `task/target_evidence.py` | 明确它只是 TaskProvider 的 deterministic evidence helper，不是用户入口 Resolver |
| `TaskTargetResolver` | `TaskTargetEvidenceProvider` | 消除与 canonical `TargetResolver` 的命名冲突 |
| `ResolvedConversationTarget` consumers | `command.TargetCandidate` | 控制和 approval 服务统一使用 canonical target candidate |

## 9. Retained Legacy

以下不是无限期“兼容垃圾”，每项都有明确调用方和退出条件：

| Retained contract | Why it remains | Current callers | Removal trigger |
| --- | --- | --- | --- |
| `conversation/commands.py` `TaskCommand` | execution control、approval 和显式 `body.command` 仍使用它 | `ConversationControlService`, `ApprovalRuntimeService`, `command/adapter.py` | TaskManager 接受 canonical Command 后迁移 control payload |
| `task/intent_spec_provider.py` / `task/understanding.py` | 旧 TaskProvider/TaskGraph fallback 需要 structured IntentSpec projection | fallback graph path、TaskProvider tests | 默认入口不再构造 TaskIntent 后删除 |
| `task/intent_compat.py` | 当前 Task persistence 和 Runtime service 的一次性 typed projection | `task_provider.py`, `intent_compiler.py`, Runtime service | TaskManager/TaskRepository 改用 canonical Task contract |
| `task/multi_task.py` | 当前 conversation adapter 仍需 TaskSegment 和 Task index projection | `conversation_runtime_adapter.py` | GoalTree/TaskManager 持久化依赖上线 |
| `task/graph.py` `TaskGraphBuilder` | 旧 graph 是现有 Conversation/Execution 的 recovery fallback | adapter fallback and graph tests | 所有 graph callers 改用 GoalCompiler |
| `orchestration/templates.py` | 已知可靠流程的 deterministic fallback | `TaskOrchestrator` legacy path | GoalTree 覆盖所有 production messages |
| `orchestration/orchestrator.py` | 现有 TaskPlan/Validator/Worker contract 仍被 Runtime service 消费 | `RuntimeAgentService` | TaskManager/GoalCompiler 完成 Plan migration |
| Artifact `created_by_agent` / event `agent_name` columns | 这是持久 provenance schema，删除需要 migration 和历史数据处理 | Artifact store/repository/timeline | provenance schema versioned migration；本阶段不再生成 fake Agent owners |
| `apps/creator-agent` registry and graph | 它是外部 Creator execution boundary，不是 assistant_core fake Agent registry | Creator service runtime | 独立服务 contract migration |

## 10. Test Results

- Targeted Phase 3.5 regression: **119 passed**。
- Covered Command Runtime, Goal Runtime, AgentLoop, canonical target resolution,
  ToolMetadata/ToolContract, CapabilityExecutor, ExecutionWorker,
  ConversationRuntimeAdapter, TaskGraph, TaskProvider and Intent fallback.
- `compileall` for assistant core, assistant API, contracts: **passed**。
- Ruff for all changed Phase 3.5 modules and tests: **passed**。
- Full-repository Ruff currently reports 1452 existing lint/import findings;
  the cleanup scope was checked separately and is clean。
- API `main` import with the repository runtime paths: **passed**。
- Full collection reached 758 tests but remains blocked by six baseline items:
  missing optional `yaml`, stale private `_run_projection_fields`, stale
  `_conversation_target_task`, stale `_append_schedule_confirmation`, and stale
  `_close_request_db_session` imports. These tests assert retired route/private
  APIs and were not restored as a second production path.

## 11. Remaining Technical Debt

1. Drain `TaskCommand`/Intent projection callers into a canonical TaskManager and
   remove the remaining Intent compatibility boundary.
2. Make `ToolMetadata` the only policy source for risk, permission, approval,
   side-effect, retry and cost; keep CapabilityRegistry as semantic retrieval only.
3. Move remaining TaskGraph fallback callers to GoalCompiler.
4. Migrate or delete tests importing retired route-private helpers and add the
   repository's missing optional OpenAPI test dependency.
5. Remove historical Agent provenance columns only after a versioned data migration.

## 12. Next Phase

The next phase should introduce Dynamic Planning/TaskManager on top of this
boundary. It should consume `GoalTree`, `ToolMetadata`, typed Task contracts and
existing Execution services; it must not reintroduce `agent.py`, Intent routing,
fake Agent classes, or a second Resolver.
