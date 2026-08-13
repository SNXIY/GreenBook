# GreenBook Agent Core Analysis

## 1. 定位

`packages/agent_core` 是 GreenBook Agent Runtime 的**核心模块**。它包含两个层次：

- **Intelligence Layer**: Command 理解、Goal 分解、AgentLoop 推理、动态规划
- **Reliable Execution Layer**: 队列、Worker、重试、检查点、幂等、租约、产物

总规模约 23,000 行 Python，131 个文件。

**核心原则**: LLM 做所有语义理解和决策；Python 代码只做 schema 校验、状态机和策略执行。

---

## 2. 目录结构

```
packages/agent_core/greenbook_agent_core/
│
├── command/                    # Command 理解 (LLM structured output)
│   ├── interpreter.py          # CommandInterpreter
│   ├── models.py               # Command, CommandContext, TargetReference
│   ├── target.py               # TargetResolver (确定性引用解析)
│   └── correction.py           # CorrectionEvent (用户纠正 → Memory)
│
├── agent/                      # AgentLoop (Observe→Reason→Act→Reflect)
│   ├── loop.py                 # AgentLoop.run() — 核心循环
│   ├── actions.py              # AgentAction, Reflection, AgentRunResult
│   ├── state.py                # AgentState, Observation
│   ├── recovery.py             # ResumeContext, AgentRecoveryService
│   └── selector.py             # ToolSelector (LLM 选择 tool)
│
├── goal/                       # Goal 理解与分解
│   ├── models.py               # Goal, TaskNode, GoalTree
│   ├── decomposer.py           # GoalDecomposer (LLM 分解)
│   └── compiler.py             # GoalCompiler (确定性编译 → PlanGraph)
│
├── planning/                   # 规划与验证
│   ├── contracts.py            # TaskPlan, PlanStep, PlanningDecision
│   ├── graph.py                # PlanGraph (DAG)
│   ├── dynamic.py              # DynamicPlanner (运行时重规划)
│   ├── models.py               # ExecutablePlan, ValidationError
│   └── validation.py           # PlanValidator (6 项确定性检查)
│
├── task/                       # Task 管理
│   ├── manager.py              # TaskManager (Goal 生命周期)
│   ├── models.py               # Task, TaskStatus, TaskRevision
│   ├── repository.py           # TaskRepository (乐观锁)
│   └── registry.py             # TaskRegistry (SQL 持久化)
│
├── execution/                  # 可靠执行层 (38 个文件)
│   ├── worker.py               # ExecutionWorker (DAG 执行)
│   ├── state_manager.py        # ExecutionStateManager (状态机)
│   ├── execution_queue.py      # 持久化执行队列
│   ├── execution_queue_worker.py  # 队列消费者
│   ├── retry_*.py (5 文件)     # 重试决策/调度/存储
│   ├── failure_decision.py     # 失败分类+策略
│   ├── checkpoint.py           # 执行检查点
│   ├── lease.py                # 执行租约
│   ├── evidence.py             # 执行证据
│   ├── reconciliation.py       # 对账服务
│   ├── timeline.py             # 执行时间线
│   ├── result_projection.py    # 结果投影
│   ├── runtime/                # Tool 运行时
│   │   ├── tool_runtime.py     # ToolRuntime (invoke + 超时 + 幂等)
│   │   ├── ledger.py           # ToolExecutionLedger
│   │   └── invocation_context.py  # 幂等 key 生成
│   └── ...
│
├── context/                    # 上下文构建
│   ├── builder.py              # ContextBuilder
│   ├── models.py               # ContextSnapshot, ContextBudget
│   └── projection.py           # 确定性投影
│
├── memory/                     # 长期记忆
│   ├── manager.py              # MemoryManager
│   ├── retriever.py            # MemoryRetriever
│   ├── repository.py           # MemoryRepository (PG)
│   ├── policy.py               # MemoryWritePolicy
│   └── models.py               # MemoryRecord, MemoryType
│
├── toolruntime/                # Tool 策略
│   ├── policy.py               # ToolPolicyGate
│   └── registry.py             # ToolRegistry (metadata)
│
├── capability/                 # 能力目录
│   ├── registry.py             # CapabilityRegistry (17 个能力)
│   ├── models.py               # Capability, CapabilityMatch
│   └── mapper.py               # CapabilityMapper
│
├── artifact/                   # 产物管理
│   ├── models.py               # Artifact (不可变)
│   ├── registry.py             # ArtifactRegistry
│   ├── store.py                # ArtifactStore (PG)
│   └── lifecycle.py            # 生命周期验证
│
├── human/                      # 人机交互
│   ├── approval_request.py     # ApprovalRequest (PG)
│   ├── manager.py              # HumanInteractionManager
│   └── models.py               # InteractionType
│
├── conversation/               # 会话管理
│   ├── service.py              # ConversationService
│   ├── control.py              # ExecutionControlCommand
│   └── preferences.py          # 用户偏好
│
├── observability/              # 可观测性
│   ├── collector.py            # TraceCollector
│   ├── trace.py                # AgentTrace
│   └── metrics.py              # MetricsCollector
│
├── db/                         # 数据库
│   ├── connection.py           # Async Engine
│   └── repositories.py         # SQL Repositories
│
├── compatibility/              # 历史兼容
│   └── history/                # run_id ↔ execution_id
│
└── runtime/
    └── container.py            # RuntimeContainer (组装)
```

---

## 3. Command 理解

### 为什么存在？

**将自然语言转换为结构化 Command**。不是分类，是理解。

### 为什么不是 Intent Router？

```
旧方式 (已淘汰):
  message → if keyword in text → intent → if-else → tool

新方式:
  message + context + memory + capabilities
       ↓
  LLM structured output → Command Object
```

**零硬编码关键词**。CommandInterpreter 调用 LLM 输出结构化的 `StructuredCommandOutput`，然后用 Pydantic 校验。

### 输入/输出

```
输入:
  - user_message: str
  - context: CommandContext (会话状态、活跃任务、候选目标)
  - llm: LLMClient
  - model: str

输出:
  - Command {
      command: "CREATE" | "MODIFY" | "CANCEL" | "QUERY" | "CONTROL"
      operation: str
      target: CommandTarget | None  (MODIFY/CANCEL 时必须)
      goals: list[GoalSpec] | None  (多目标时)
      confidence: float
      needs_clarification: bool
      clarification_question: str | None
      reasoning: str
    }
```

### Command 类型

| Command | 说明 | requires_target |
|---------|------|-----------------|
| CREATE | 创建新任务 | false |
| MODIFY | 修改已有任务 | true |
| CANCEL | 取消任务 | true |
| QUERY | 查询 | false |
| CONTROL | 执行控制 (暂停/恢复/审批) | true |

### TargetResolver (确定性引用解析)

Command 生成后，TargetResolver 负责解析会话中的引用：

```
TargetResolution:
  - Resolved: 找到一个匹配
  - Ambiguous: 多个匹配 (→ 请求用户澄清)
  - NotFound: 无匹配 (→ 创建新任务或报错)
```

解析策略 (按优先级):
1. explicit_id — 用户明确指定 ID
2. ACTIVE — 会话活跃目标
3. IDENTIFIER — label/标题匹配
4. ORDINAL — "第2个" (按时间戳排序)
5. PROPERTY — 属性匹配 (状态、类型)
6. TEMPORAL — "刚才" (按时间窗口)
7. 非结构化文本 — 自由文本匹配

**从不默默选择第一个**。多个匹配时返回 Ambiguous。

---

## 4. Context 构建

### ContextBuilder

```
输入:
  - conversation_id, user_id, tenant_id
  - 注入: task_manager, execution_repository, artifact_store,
          memory_retriever, preference_provider

输出:
  - ContextSnapshot {
      conversation: 最近消息 (上限 12 条)
      tasks: 活跃 Task 列表 (上限 20)
      goals: Goal 列表 (上限 40)
      executions: 最近执行记录
      artifacts: 产物引用
      preferences: 用户偏好
      memories: 召回记忆 (上限 8)
      targets: 候选目标 (上限 80)
    }
```

### ContextSnapshot 提供

- `history_for_model()` — LLM 可用的对话历史
- `decision_payload()` — Agent 决策时的完整上下文
- `target_payload()` — TargetResolver 的候选列表

### 三者区别

| 概念 | 存储 | 生命周期 | 内容 |
|------|------|----------|------|
| Conversation | PostgreSQL | 跨会话 | 所有消息历史、压缩摘要 |
| ContextSnapshot | 内存 (每次构建) | 单次请求 | 对话 + Task + 执行 + 产物 + 偏好 + 记忆 |
| Memory | PostgreSQL | 跨会话、跨天 | 情节记忆、语义偏好、过程策略 |

---

## 5. Goal 分解

### 为什么需要 Goal？

用户消息可能包含复杂目标，如 "分析AI趋势，写文章，明天发布"。Goal 将其分解为可执行的子目标。

### GoalTree

```
Goal:
  id: str
  description: str                    # "research AI trends"
  parent_id: str | None
  children: list[Goal]
  required_capabilities: list[str]    # ["search", "analyze"]
  dependencies: list[str]             # 依赖的 sibling goal ids
  expected_output: str | None         # "research_summary"

GoalTree:
  root: Goal
  all_goals() → 所有节点
  executable_goals() → 叶子节点 (无子节点)
  validate_tree() → 检查环/重复/未知引用
```

### GoalDecomposer

```
输入: Command + ContextSnapshot + capability descriptors
输出: GoalTree (LLM 生成, Pydantic 校验)

校验:
  1. 每个引用的 capability 在目录中存在
  2. Command.required_capabilities 都被满足
```

### GoalCompiler (确定性)

```
输入: GoalTree
输出: PlanGraph (DAG, PlanNode 之间边表示依赖)

_GOAL_TYPE_CAPABILITIES 映射:
  SEARCH → search
  CREATE → generate
  VALIDATE → validate
  ...
```

---

## 6. Task 管理

### Task 解决什么问题？

Task 是 Goal 的**持久化实体**。Goal 是概念上的目标，Task 是可追踪、可恢复的执行实体。

### Task 生命周期

```
TaskStatus:
  CREATED → PLANNING → READY → RUNNING → COMPLETED
                    ↓              ↓
                   FAILED     ┌─ WAITING
                              ├─ PAUSED
                              └─ CANCELLED
```

### Task 结构

```python
Task:
  task_id: str
  conversation_id: str
  user_id: str
  status: TaskStatus
  goal_tree_snapshot: dict        # GoalTree 快照
  goal_tree_version: int          # 乐观锁版本
  plan_history: list[PlanRevision]
  execution_refs: list[TaskExecutionRef]
  resource_index: dict[str, str]  # draft_id, schedule_id, post_id
  revisions: list[TaskRevision]
  created_at, updated_at: datetime
```

### TaskManager

```
方法:
  create_task(command, context) → Task
  get_task(task_id) → Task
  get_active_tasks() → list[Task]
  bind_goal_tree(task, goal_tree) → Task (bump goal_tree_version)
  bind_execution(task, execution_id) → Task
  record_replan(task, revision) → Task (bump plan_version)
  modify_task(task, command) → Task
  pause / resume / cancel / complete / fail
  preempt_for(active_goal, new_command)
```

### 任务抢占 (Preempt)

```
场景: 正在写文章A, 用户说"先分析一下旧文章"

TaskManager:
  1. 创建 Goal B: "analyze_old_post"
  2. 暂停 Goal A (PAUSED)
  3. 执行 Goal B
  4. B COMPLETED → 恢复 A (RUNNING)
```

---

## 7. Planning

### 为什么不是固定 Workflow？

旧系统有 11 个硬编码模板 (FULL_PIPELINE, CREATE_AND_PUBLISH, ...)。新增业务需要新增模板。

新系统使用 **DynamicPlanner**——LLM 生成 Plan，运行时根据 Observation 调整。

### DynamicPlanner

```
decide(goal_tree, agent_state, observations, context):
  输入: 当前 GoalTree + AgentState + 最近的 Observation
  输出: PlanningDecision
  
PlanningDecisionType:
  CONTINUE — 按计划继续
  INSERT_STEP — 插入新步骤
  REMOVE — 移除步骤
  REORDER — 重排步骤
  RETRY_WITH_NEW_ARGS — 换参数重试
  SELECT_ALTERNATIVE_TOOL — 换工具
  ASK_HUMAN — 需要人类决策
  FINISH — 目标完成
  ABORT — 目标不可完成

apply(decision, goal_tree):
  执行决策 → 新的 GoalTree (version+1)
```

### PlanValidator (6 项确定性检查)

1. capability 存在
2. tool 映射 (LLM-only steps 豁免)
3. 无环 (DFS)
4. 依赖满足
5. artifact flow (传递闭包)
6. 审批 + side-effect (ToolPolicyMetadata)

---

## 8. AgentLoop

### 核心循环

```
┌─────────────────────────────────────────────────────┐
│                  AgentLoop.run()                     │
│                                                      │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐       │
│  │ OBSERVE  │───▶│  REASON  │───▶│   ACT    │       │
│  └──────────┘    └──────────┘    └──────────┘       │
│        ▲                               │             │
│        │                               ▼             │
│        │                        ┌──────────┐        │
│        └────────────────────────│ REFLECT  │        │
│                                 └──────────┘        │
│                                      │               │
│                            ┌─────────┴─────────┐     │
│                            ▼                   ▼     │
│                        FINISH            continue ──┘
└─────────────────────────────────────────────────────┘
```

### Observe (观察)

```
输入:
  - user goal (from Command)
  - task state (from TaskManager)
  - tool result (from previous Action)
  - memory (from MemoryRetriever)
  - context (from ContextBuilder)

输出:
  - Observation {
      goal_progress: float         # 目标完成度 0.0-1.0
      last_action: AgentAction     # 上一动作
      last_result: ToolResult      # 上一结果
      pending_approvals: list      # 待审批
      active_task_ids: set[str]    # 活跃任务
      memory_snapshot: list        # 相关记忆
      constraints: dict            # 当前约束
    }
```

### Reason (推理)

```
输入: Observation + GoalTree + ToolCatalog
输出: AgentAction

AgentActionType:
  TOOL_CALL — 调用工具
  CREATE_TASK — 创建任务 (含 Plan)
  UPDATE_PLAN — 更新计划
  ASK_USER — 需要人类输入
  FINISH — 完成

ToolSelector.select():
  如果 AgentLoop 指定了 tool → 验证 (从不在 capability→tool 之间做位置映射)
  否则 → LLM 从 ToolMetadata catalog 中选择
```

### Act (行动)

```
TOOL_CALL:
  → ToolPolicyGate.enforce() → DENY/WAITING_HUMAN/QUEUE/SYNC
  → DENY: raise ToolPolicyDeniedError
  → QUEUE: 通过 execution_submission.submit_tool 提交
  → SYNC: tool_runtime.invoke() → ToolResult

CREATE_TASK:
  → GoalCompiler.compile + compile_plan
  → execution_submission.submit(plan) → Queue

ASK_USER:
  → HumanInteractionManager.pause() → WAITING_HUMAN
```

### Reflect (反思)

```
输入: Observation (act 之前) + AgentAction + ToolResult
输出: Reflection

Reflection:
  finished: bool              # 目标完成？
  progress_score: float       # 0.0-1.0
  needs_next_step: bool       # 需要下一步？
  needs_replan: bool          # 需要调整计划？
  summary: str                # 进展摘要
  concerns: list[str]         # 关注点
  next_action_hint: str       # 下一步方向

如果 needs_replan:
  → DynamicPlanner.replan()
    → apply()
      → TaskManager.record_replan()
```

### 终止条件

```
FINISH — 目标完成
WAITING_HUMAN — 暂停等待审批/输入
FAILED — 执行失败 (不可恢复)
BUDGET_EXCEEDED — 超出迭代/成本限制
```

### ResumeContext (恢复执行)

AgentLoop 支持从多种状态恢复:

- RESUME_EXECUTION — 继续之前暂停的执行
- REPLAN_FROM_FAILURE — 从失败中重规划
- WAIT_FOR_EXTERNAL — 等待外部异步任务
- WAIT_FOR_HUMAN — 等待人类决策
- RETRY_STEP — 重试失败的步骤

---

## 9. 模块间调用关系

```
AgentLoop
  ├── 调用 CommandInterpreter (理解用户输入)
  ├── 调用 GoalDecomposer (分解目标)
  ├── 调用 GoalCompiler (编译 Plan)
  ├── 调用 ContextBuilder (构建上下文)
  ├── 调用 ToolSelector (选择 tool)
  ├── 调用 ToolPolicyGate (执行前策略检查)
  ├── 调用 DynamicPlanner (运行时调整)
  ├── 调用 TaskManager (Goal 生命周期)
  ├── 调用 MemoryManager (读写记忆)
  └── 调用 ExecutionSubmissionService (提交执行)

ExecutionWorker (被 AgentLoop 通过 submission 调用)
  ├── 调用 ExecutionStateManager (状态转换)
  ├── 调用 StepScheduler (DAG 调度)
  ├── 调用 CapabilityExecutor (步骤执行)
  ├── 调用 ToolRuntime (Tool 调用)
  ├── 调用 FailureDecisionEngine (失败分类)
  ├── 调用 RetryDecisionEngine (重试决策)
  ├── 调用 ArtifactStore (产物存储)
  └── 调用 ExecutionEventStore (事件存储)
```

---

## 10. 关键设计原则

1. **LLM 做决策，Python 做校验**：语义理解全部由 LLM structured output 完成；Python 代码只做 schema 校验、状态转换、策略执行。

2. **零关键词路由**：没有中文关键词分类，没有 if-else 意图路由链。Command 由 LLM 根据完整上下文理解。

3. **Fail-closed**: 不确定时拒绝，不做假设。TargetResolver 从不默默选第一个；RetryDecision 在有副作用时不重试；ToolPolicyGate 在未知 tool 时拒绝。

4. **幂等性**: 所有关键操作有确定性 idempotency key。Tool 调用 (sha256 hash)、执行创建 (task+plan+step)、重试任务 (sha256)、产物 (content_sha256)。

5. **双持久化**: 每个存储组件都有内存实现 (测试) 和 PostgreSQL 实现 (生产)，通过 `RuntimePersistenceFactory` 环境变量选择。
