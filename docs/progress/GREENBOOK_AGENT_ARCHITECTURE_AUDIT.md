# GreenBook Agent Architecture Audit

> **日期**: 2026-08-11 | **审计范围**: 完整代码库（基于实际代码阅读，非文档推测）
> **比对参考**: nanobot (HKUDS/nanobot)、Claude Computer Use / OpenAI Operator 设计理念

---

## 第一部分：当前架构

### 1.1 当前整体架构图

```
                          ┌─────────────────────────┐
                          │     Frontend (:5173)     │
                          │     Vite + React         │
                          └───────────┬─────────────┘
                                      │ HTTP + JWT
                          ┌───────────▼─────────────┐
                          │   Assistant API (:8094)  │
                          │                          │
                          │  ┌─────────────────────┐ │
                          │  │ JWT Auth Middleware  │ │
                          │  └─────────┬───────────┘ │
                          │            │              │
                          │  ┌─────────▼───────────┐ │
                          │  │ ConversationRuntime  │ │
                          │  │ Adapter              │ │
                          │  └─────────┬───────────┘ │
                          │            │              │
                          │     ┌──────┴──────┐      │
                          │     │             │      │
                          │  ┌──▼──────┐ ┌───▼────┐ │
                          │  │ Legacy  │ │Runtime │ │
                          │  │ agent.py│ │ Path   │ │
                          │  └────┬────┘ └───┬────┘ │
                          └───────┼──────────┼──────┘
                                  │          │
                    ┌─────────────┘   ┌──────┴──────────────┐
                    ▼                 ▼                       ▼
          ┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐
          │ DeepSeek API │  │ Assistant Worker │  │ PostgreSQL       │
          │ (LLM)        │  │ (独立进程)        │  │ (mindflow_creator)│
          └──────────────┘  └────────┬─────────┘  └──────────────────┘
                                     │
                          ┌──────────┴──────────┐
                          │ Execution Queue     │
                          │ Worker + Retry      │
                          └──────────┬──────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
    ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
    │ Java Backend    │  │ Creator Agent    │  │ MCP Server       │
    │ (:8080, MySQL)  │  │ (:8092, PG)      │  │ (16 tools)       │
    └─────────────────┘  └──────────────────┘  └──────────────────┘
```

### Runtime 内部执行路径

```
User Message
  │
  ▼
┌─ ConversationRuntimeAdapter ──────────────────────────────────┐
│  1. IntentSpecProvider.resolve(message)                       │
│  2. TaskGraphBuilder.build(message)      ← LLM 意图+图分析    │
│  3. TaskProvider.create_task(scope, spec) ← PG 持久化         │
│  4. IntentCompiler.compile(spec, task)    ← TaskContext 组装  │
│  5. RuntimeAgentService.execute(ctx)                          │
└────────────────────────┬──────────────────────────────────────┘
                         │
                         ▼
┌─ RuntimeAgentService ─────────────────────────────────────────┐
│  6. MemoryManager.recall(ctx.user_id)   ← 召回记忆            │
│  7. TaskOrchestrator.generate_plan()    ← 模板选择+实例化     │
│  8. PlanValidator.validate(plan)        ← 校验              │
│  9. ArgumentBinder.bind_plan(plan)      ← 参数绑定           │
│ 10. AgentRuntime(executor_overrides)    ← Agent 包装          │
│ 11. ExecutionWorker(agent_runtime)      ← Worker 构建         │
│ 12. worker.init_from_plan(executable)   ← 创建 PlanExecution  │
│ 13. worker.run(execution_id)            ← 步进执行            │
└────────────────────────┬──────────────────────────────────────┘
                         │
                         ▼
┌─ ExecutionWorker.run() ───────────────────────────────────────┐
│  for each ready step:                                         │
│    14. CapabilityExecutor.execute_step(plan_step)             │
│          │                                                    │
│          ├─ capability registry lookup                        │
│          ├─ tool_name = cap.tools[0]    ← 固定取第一个工具     │
│          ├─ ArgumentBinder.bind(step)   ← 最终参数绑定         │
│          └─ invoke_fn(invocation_ctx)                         │
│               │                                               │
│               ▼                                               │
│    15. ToolRuntime.invoke(ctx)                                │
│          ├─ idempotency check (ledger replay)                  │
│          ├─ asyncio.wait_for(handler(), timeout)              │
│          └─ evidence recording                                │
│               │                                               │
│               ▼                                               │
│    16. GreenBookMCPServer.execute_tool(name, auth, args)      │
│          ├─ Pydantic input validation                         │
│          ├─ handler(ctx, **args)                              │
│          ├─ Pydantic output validation                        │
│          └─ → Java Backend or Creator Agent or local logic    │
│                                                                 │
│  on step failure:                                              │
│    17. FailureDecisionEngine.decide(failure)                   │
│    18. RetryDecisionEngine.decide_for_step(failure, step)      │
│    19. StateManager.fail_step() / retry / skip_downstream     │
│                                                                 │
│  on completion:                                                │
│    20. ArtifactStore.create_from_result()                      │
│    21. MemoryManager.remember_execution()                     │
│    22. RuntimeResult assembly → API response                  │
└────────────────────────────────────────────────────────────────┘
```

---

### 1.2 当前核心模块职责（基于实际代码）

#### Conversation Runtime

**负责**:
- 会话级状态管理（`SessionContext`）：最近实体、工具调用记录、active_draft_id/schedule_id
- 跨 Turn 目标消歧（`ConversationTaskIndex`、`ConversationTargetResolver`）
- 将单次 Turn 转化为 Runtime 执行（`ConversationRuntimeAdapter`）

**不负责**:
- 会话持久化（`conversation_store = {}`，重启丢失）
- 消息历史管理（通过 `conversation_history: list[dict]` 参数传递，无截断/摘要）
- 上下文窗口管理（只有 `*_MAX_CHARS` 环境变量硬截断）

**关键代码位置**: `context.py:35`, `conversation_runtime_adapter.py:63`, `multi_task.py:165`

---

#### Intent / Command

**当前机制**: 双层识别

1. **L1 — 关键词匹配** (`understanding.py:481-585`, `agent.py:77-91`)
   - 硬编码中文关键词集合：`_CREATE_WORDS`、`_REVISE_WORDS`、`_SCHEDULE_WORDS` 等
   - 输出：`TaskIntent`（简化的意图模型）
   - 得分型 L2 触发：`_needs_l2_v2()` 用计分规则决定是否升级到 LLM

2. **L2 — LLM 结构化输出** (`understanding.py:791-917`)
   - `_L2_SYSTEM_V2` prompt → LLM → `IntentSpec` Pydantic 校验
   - 失败时尝试 Targeted Repair（`_llm_repair_spec`）
   - 同时支持 Draft（自由格式）和 Elements（结构化元素）两种中间格式

3. **双轨并行问题**:
   - `agent.py` 有独立的 `_turn_intents()` 关键词匹配（与 `understanding.py` 的 L1 重复）
   - `agent.py` 有独立的 `_turn_routing_hint()` 和 `_turn_tool_filter()`

**存在的问题**:
- L1 关键词是纯中文硬编码，无法国际化，新增场景需改代码
- `agent.py` 和 `understanding.py` 有两套独立的关键词匹配逻辑
- L1 的置信度标记为 0.85 但实际准确率依赖关键词覆盖
- "把刚才那个改成晚上10点" → L1 需要 `_SCHEDULE_NOUNS` 特殊处理来区分 "修改内容" vs "修改定时"

**关键代码位置**: `understanding.py:34-88`, `understanding.py:680-731`, `agent.py:25-91`

---

#### TaskGraph

**如何拆任务**:

1. 句子级拆分 (`multi_task.py:90-136` `split_task_segments`)
   - "第一，...第二，..." → 多个 `TaskSegment`
   - "；然后分析..." → 查询型拆分
   - 保守策略：只有明确的编号或分隔才拆分

2. LLM 语义拆分 (`intent_spec_provider.py:112-196` `resolve_graph`)
   - 调用 LLM 判断消息是否是多个独立目标
   - LLM 输出 `{"goals": [{text, intent, depends_on, artifact_inputs, artifact_outputs}]}`
   - 只有 LLM 判断为独立目标时才拆分

3. 图构建 (`graph.py:99-153` `TaskGraphBuilder.build`)
   - 将 LLM 的 `resolve_graph` 结果转化为 `ConversationTaskGraph`
   - 验证 DAG 无环，返回拓扑序

**支持**:
- ✅ 多任务（通过 LLM `resolve_graph` + `execute_graph`）
- ✅ 多目标（`TaskGoal` 模型 + `depends_on_goal_ids`）
- ⚠️ 动态修改：`MODIFY_TASK`/`CONTINUE_TASK` 关系存在，但修改的是已有 Task 的属性（如追加 Goal），不是修改正在执行中的步骤

**不足**:
- 任务拆分完全依赖 LLM 判断，无确定性 fallback
- 无法在执行中动态插入/删除/重排步骤
- 图结构来自 LLM 一次性输出，不是 Agent 逐步推理的产物

**关键代码位置**: `graph.py:77`, `multi_task.py:90`, `intent_spec_provider.py:112`

---

#### Planner

**当前是 Workflow Planner，不是 Agent Planner。**

证据：
- 计划来源：固定模板目录 (`templates.py:233-247` `ALL_TEMPLATES`)
- 模板选择：if-else 规则匹配 (`orchestrator.py:223-280`)
- 模板实例化：`PlanTemplate.instantiate(task_id)` 直接复制 steps
- 无 LLM 参与规划（LLM 只做意图理解，不做执行规划）
- 无 Replan 机制（执行中失败不会重新规划下游步骤）

**模板数量**: 11 个（SINGLE_CREATE/IMPROVE/SEARCH/PUBLISH/CANCEL/MANAGE_SCHEDULE + CREATE_WITH_RESEARCH + CREATE_AND_PUBLISH + CREATE_AND_IMPROVE + FULL_PIPELINE + IMPROVE_WITH_RESEARCH）

**验证**: `PlanValidator` 只检查 capability 是否注册、依赖是否合法、工具是否映射

**关键代码位置**: `templates.py:1-252`, `orchestrator.py:222-281`, `planning/validation.py`

---

#### AgentRuntime

**不是真正的 Agent 自主执行，而是 Executor 包装。**

`AgentRuntime` (`agent_runtime/runtime.py:16`) 的职责：
1. 从 `AgentRegistry` 查找 Agent 元数据
2. 校验输入/输出 Artifact 的 Schema 兼容性
3. 将执行委托给注册的 `AgentExecutor`
4. 发布输出的 Artifact 到 `ArtifactRegistry`

它**不做**：
- Agent 循环（无 Observe → Reason → Act 循环）
- Agent 自主决策（步进由 `ExecutionWorker` 驱动，agent 只是被动执行 step）
- Agent 间通信（通过 Artifact 传递，无直接消息）

`CapabilityAgentExecutor` (`agent_runtime/executors.py:96`) 是将现有 `CapabilityExecutor` 桥接到 `AgentRuntime` 的适配器，它需要 `PlanStep` 才能执行。

**关键代码位置**: `agent_runtime/runtime.py:16-211`, `agent_runtime/executors.py:96-113`

---

#### ToolRuntime

**工具选择机制**: **1:1 固定映射**

- `CapabilityExecutor.execute_step()` (`capability_executor.py:108-118`):
  ```python
  tool_name = cap.tools[0]  # 始终取第一个
  ```
- 每个 Capability 在注册时绑定一个或多个工具名，执行时取 `tools[0]`
- LLM 不参与工具选择（只在 `agent.py` 的旧路径中通过 function calling 选择）

**关键代码位置**: `capability_executor.py:80-196`, `capability/registry.py`

---

#### Memory

**当前状态**:

| 记忆类型 | 实现 | 存储 | 是否参与决策 |
|---------|------|------|------------|
| Short-term Memory | SessionContext（最近20个实体/工具调用） | 内存 | ✅ 目标消歧时参考 |
| Episodic Memory | Execution后记录 goal+status+draft_id | 内存 MemoryStore | ⚠️ 仅在开始前召回，不参与 Planning |
| Semantic Memory | Qdrant + "hashing" embedding（非真正语义） | Qdrant | ❌ hashing 不是语义检索 |
| Procedural Memory | 策略提取框架，数据积累不足 | 内存 | ❌ 几乎无可用数据 |
| User Profile | 通过语义记忆存偏好 | 内存 | ⚠️ recall 但不强制使用 |

**MemoryStore** (`agent_memory/store.py:13`): 纯内存实现，无持久化，重启全部丢失。

**关键问题**:
- `ASSISTANT_MEMORY_EMBEDDING_PROVIDER=hashing`（默认值）→ 语义记忆实际上不可用
- Memory 的召回在 `RuntimeAgentService._recall_memories()` 中进行，但只是填充 `ctx.memory_context`，对规划没有硬约束
- 无 Memory Consolidation（情节→语义的提炼）

**关键代码位置**: `agent_memory/store.py:13-100`, `runtime_agent_service.py:1174-1222`

---

#### Execution Runtime

**Queue**: ✅ 生产级
- 双实现：`ExecutionQueue`（内存）+ `PostgresExecutionQueue`（生产）
- 完整的 Claim/Ack/Fail/Release 语义
- 自动回收过期 Lease
- `ExecutionQueueProtocol` 接口契约

**Worker**: ✅ 生产级
- 独立进程（`assistant_worker`）
- `ExecutionQueueWorker` + `RetryBackgroundWorker` 双消费者
- 通过 PG 共享队列与 API 进程通信
- 健康检查文件定期写入

**Retry**: ✅ 安全但保守
- `RetryDecisionEngine` 的 fail-closed 安全矩阵
- 只有在 "明确未发送 + 无副作用 + 瞬态错误 + 有配额" 时才允许重试
- 基于 Evidence 的决策（`request_sent`、`side_effect_state`）

**Checkpoint**: ✅ 存在但轻量
- `StepExecution.checkpoint_data` 持久化
- 存储 bound constraints（如 draft_id、schedule_id）

**Pause/Resume**: ✅ 基础可用
- `ExecutionStatus.PAUSED` / `WAITING_APPROVAL` / `WAITING_HUMAN`
- `HumanInteractionManager` 管理 Approval/Clarification/Input
- `WAITING_ASYNC` 支持长任务（Creator Agent 异步回调）

**评价**: Execution Runtime 是整个系统最成熟的部分，达到了生产级水平。

**关键代码位置**: `execution_queue.py:104-589`, `worker.py:62-624`, `retry_decision.py:76-218`

---

## 第二部分：与 nanobot 对比

### 2.1 nanobot 核心设计（基于实际架构文档）

```
┌──────────────────────────────────────────────┐
│                 AgentLoop                     │
│  (turn 状态机: RESTORE→COMPACT→COMMAND→      │
│   BUILD→RUN→SAVE→RESPOND→DONE)               │
│                                               │
│  ┌─────────────┐  ┌──────────────────────┐   │
│  │ ContextBuilder│  │   AgentRunner        │   │
│  │ (identity +   │  │   (provider/tool     │   │
│  │  memory +     │  │    loop, max 40      │   │
│  │  skills +     │  │    iterations)       │   │
│  │  history)     │  │                      │   │
│  └─────────────┘  └──────────────────────┘   │
│                                               │
│  ┌──────────────────────────────────────┐    │
│  │  ContextGovernor                     │    │
│  │  (token budgeting, history truncation)│    │
│  └──────────────────────────────────────┘    │
│                                               │
│  ┌─────────┐ ┌────────┐ ┌────────────────┐  │
│  │ Memory  │ │ Skills │ │ SubagentManager │  │
│  │ (MEMORY │ │(SKILL. │ │ (隔离AgentRunner)│  │
│  │  .md)   │ │ md)    │ │                │  │
│  └─────────┘ └────────┘ └────────────────┘  │
└──────────────────────────────────────────────┘
```

### 2.2 关键差异

| 维度 | GreenBook 当前 | nanobot |
|------|--------------|---------|
| **Agent Loop** | ❌ 无独立 Agent Loop。`ExecutionWorker.run()` 是 workflow 步进器 | ✅ `AgentLoop` 是显式状态机，`AgentRunner` 是 provider/tool 循环 |
| **规划方式** | Template-based（11个固定模板，if-else 选择） | Model-driven（LLM 自主决定下一步做什么） |
| **任务拆解** | LLM `resolve_graph` 一次性输出完整图 + 句中拆分 | 不做预设拆解，Agent 在 loop 中逐步推理 |
| **Context 管理** | 简单硬截断（`*_MAX_CHARS` 环境变量） | `ContextGovernor` — token 预算、历史截断、大工具结果落盘 |
| **Memory** | 三种类型（episodic/semantic/procedural），但 in-memory | `MEMORY.md` 文件 + `MemoryStore`，workspace 级别的持久化 |
| **Skill 系统** | ❌ 无 Skill 概念。Agent 是固定代码类 | ✅ `SKILL.md` 格式，always-active/on-demand 两种 |
| **工具系统** | 1:1 固定映射（capability → tool[0]） | 工具注册表，LLM 自由选择调用 |
| **Subagent** | ❌ 无 | ✅ `SubagentManager`，隔离的 `AgentRunner` + 受限工具集 |
| **Session** | In-memory `conversation_store={}` | Workspace 目录持久化 |
| **反思** | ❌ 无 Reflection | ⚠️ 有 Goal Sustenance（`/goal` 持续注入提示），但无显式 Reflection |

### 2.3 核心设计哲学差异

**GreenBook**：**Workflow-First**
- 预设业务流程（搜索→分析→创作→发布）
- 模板化步骤
- 可靠性优先（Retry/Evidence/Checkpoint）
- 适合：已知业务场景的自动化

**nanobot**：**Agent-First**
- 无预设流程
- Agent 自主决定每一步
- 灵活性优先
- 适合：开放域任务

**Operator / Claude Code**：**Goal-First**
- 用户给目标
- Agent 理解、规划、执行、反思
- 工具是最小化接口
- 关键：简单但灵活

---

## 第三部分：重点问题分析

### 3.1 意图识别

**案例**: 用户说"把刚才那个改成晚上10点"

**当前链路**:

```
用户消息
  │
  ├─ L1 (_quick_intent):
  │   "改成" ∈ _REVISE_WORDS → asks_revise=True
  │   但 "发布时间" ∈ _SCHEDULE_NOUNS → 反转: asks_revise=False, asks_schedule=True
  │   → relation=MODIFY_TASK, category=PUBLISH_CONTENT, target_hint="recent"
  │
  ├─ L2 (_needs_l2_v2):
  │   "改成" 不匹配时间变更正则 → score=0 (不加分)
  │   → 不触发 L2 (score < 2)
  │
  └─ 结果: 走 L1 → TaskIntent(relation=MODIFY_TASK, goal_category=PUBLISH_CONTENT)
```

**Badcase 分析**:

1. "改成" 匹配 `_REVISE_WORDS`，需要 `_SCHEDULE_NOUNS` 特殊处理来反转
2. 如果没有 "发布时间" 关键词，单纯 "把刚才那个改成晚上10点" → L1 会识别为 `IMPROVE_CONTENT` 而非 `PUBLISH_CONTENT`
3. "刚才那个" 消歧依赖 `_extract_hint()` 的简单逻辑（取 `existing_tasks[0]`）
4. L2 的 `_needs_l2_v2` 打分规则可能漏掉边界 case（如 "再晚一点发" 无时间变更关键词）

**设计建议**: 需要独立的 Command Interpreter 层

当前的意图识别被分散在三个地方：
- `agent.py:77-91` — 旧路径的关键词匹配
- `understanding.py:481-585` — L1 关键词匹配
- `understanding.py:791-917` — L2 LLM 理解

应该统一为一个 **Command Interpreter**，输入用户消息 + 会话上下文，输出结构化 Command：

```
CommandInterpreter
  ├─ 路由: 简单查询 / 单步操作 / 复合任务 / 控制命令(pause/resume/cancel)
  ├─ 参数提取: 时间、目标引用、审批要求
  └─ 输出: UnifiedCommand { type, params, confidence }
```

---

### 3.2 任务拆解

**当前 TaskGraph 的优势**:
- DAG 依赖清晰，可视化
- 支持多目标并行
- 拓扑排序保证执行顺序
- Artifact 流转追踪

**当前 TaskGraph 的缺点**:
- 图结构来自 LLM 一次性输出，无增量构建
- 执行中无法修改图（Replan）
- 图构建与执行是分离的两个阶段
- 模板驱动的 Plan 生成限制了灵活性

**nanobot 的做法**:
- 不做预设拆解
- Agent 在 loop 中逐步决定下一步
- 每次迭代：观察当前状态 → 推理 → 选择动作 → 执行 → 观察结果
- 灵活性极高，但缺乏全局视图

**混合架构建议**:

```
简单任务 → Agent Loop (Observe → Reason → Act → Reflect)
  例: "帮我搜一下Java微服务的文章"
  不需要 TaskGraph，Agent 直接 tool calling

复杂任务 → TaskGraph + Agent Loop
  例: "分析Java趋势→生成指南→定时发布→整理Redis重点"
  1. Command Interpreter 识别为复合任务
  2. Agent 在 loop 中生成 TaskGraph（而非一次性 LLM 输出）
  3. TaskGraph 支持动态修改（Replan on failure）
  4. 每个 Node 由 Agent Loop 独立执行
```

---

### 3.3 多任务执行

**当前模型**:

```
Conversation
  └─ Task 1 (READY)          ← "分析Java趋势"
  │    └─ Goal 1.1
  │    └─ Goal 1.2
  │    └─ Execution e1 (COMPLETED)
  │
  └─ Task 2 (READY)          ← "生成学习指南"
  │    └─ Goal 2.1
  │    └─ Execution e2 (RUNNING)
  │
  └─ Task 3 (READY)          ← "整理Redis重点"
       └─ Goal 3.1
       └─ Execution e3 (PENDING)
```

**问题**: 不支持 "A 执行中，用户新增 B，暂停 A，执行 B，继续 A"

当前限制：
- `ExecutionWorker.run()` 是同步步进，一个 Execution 必须跑完或挂起（WAITING_APPROVAL/WAITING_ASYNC）
- 没有 Execution 级别的抢占式暂停（只能等当前 Step 完成或 Worker 主动 check `RuntimeGuard`）
- `RuntimeGuard` 只检查 PAUSED 状态，没有 "用户主动中断" 的机制

**需要的改动**:
1. Execution 增加 `INTERRUPTED` 状态 — 用户可主动中断正在执行的 Task
2. Conversation 级调度器 — 决定哪个 Task 的 Execution 获得执行权
3. 用户优先级覆盖 — "先做这个" 可以抢占当前执行

---

### 3.4 长任务

**案例链路**: 搜索资料 → 分析 → 创作 → 审核 → 发布

**当前支持**:
- ✅ 步骤间状态持久化（Checkpoint）
- ✅ 异步长任务（Creator Agent 的 AsyncTaskHandle）
- ✅ 失败重试（Evidence-based Retry）
- ✅ 审批暂停（WAITING_APPROVAL → Resume）

**缺少**:
- ❌ 动态规划：Creator 生成草稿后，如果质量校验失败，无法自动触发重新生成
- ❌ 失败重新规划：如果 "搜索" 步骤返回空结果，无法自动调整搜索策略或跳过
- ❌ 依赖管理：多步骤间的 Artifact 传递是硬编码的（constraints 中的 draft_id/schedule_id），不够通用

---

## 第四部分：优化路线

### P0 — 必须优化（短期，1-2 个 Phase）

#### P0-1: 统一意图识别 → Command Interpreter

**问题**: 三套独立的意图识别逻辑（`agent.py` 关键词 + `understanding.py` L1 + `understanding.py` L2）

**方案**:
- 退役 `agent.py` 的关键词匹配
- L1 降级为性能优化（仅处理确定的简单查询）
- 其他全部走 L2 LLM（IntentSpec + Repair）
- 新增 `CommandInterpreter` 层统一入口

**修改模块**: `agent.py`, `understanding.py`, `intent_spec_provider.py`

**为什么现在做**: 三条路径的行为不一致，每次新增场景需要三处同步

---

#### P0-2: 实现 Agent Loop

**问题**: 当前没有 Agent Loop，执行是 workflow 步进而非 Agent 自主推理

**方案**:

```
class AgentLoop:
    """Observe → Reason → Act → Reflect"""

    async def run(self, context, goal):
        while not self._is_terminal():
            # 1. Observe: 收集当前状态
            state = await self._observe(context)

            # 2. Reason: LLM 推理下一步
            action = await self._reason(state, goal, self.history)

            # 3. Act: 执行动作
            result = await self._act(action, context)

            # 4. Reflect: 评估结果
            reflection = await self._reflect(action, result)

            # 5. Update context
            self._update_context(result, reflection)
```

**与现有 ExecutionWorker 的关系**: Agent Loop 是 ExecutionWorker 的上层。
- 简单任务：Agent Loop 直接 tool calling（不走 TaskGraph/Planner/Worker）
- 复杂任务：Agent Loop 决定需要 TaskGraph，然后委托给 Worker 执行

**修改模块**: 新增 `agent_runtime/loop.py`, 修改 `runtime_agent_service.py`

**为什么现在做**: 这是 Operator 类 Agent 的核心差异。没有 Agent Loop，GreenBook 就是一个 Workflow 引擎。

---

#### P0-3: Memory 持久化 + 真正语义检索

**问题**: MemoryStore 纯内存 + "hashing" embedding 不可用

**方案**:
1. `MemoryStore` 增加 PostgreSQL 持久化实现
2. 切换到 OpenAI text-embedding-3-small 或本地模型
3. 增加 Memory Consolidation（定期将 episodic 提炼为 semantic）

**修改模块**: `agent_memory/store.py`, `agent_memory/manager.py`, `runtime_agent_service.py`

**为什么现在做**: 当前 Memory 系统框架完整但数据质量不足，影响多轮任务连贯性

---

### P1 — 架构增强（中期，2-3 个 Phase）

#### P1-1: Hybrid Planning（混合规划）

**问题**: 当前只有模板规划，缺少 Agent 自主规划

**方案**:
- 保留模板用于已知场景（快速、可靠）
- Agent Loop 支持自由规划（LLM 驱动的 step-by-step）
- Planner 统一入口：先查模板，无匹配时 fallback 到 Agent Loop

```
Planner.plan(goal, context):
    if template := TemplateMatcher.match(goal):
        return template.instantiate(goal)
    else:
        return AgentLoop.plan(goal, context)  # LLM 逐步推理
```

**修改模块**: `orchestration/orchestrator.py`, 新增 `agent_runtime/loop.py`

---

#### P1-2: Skill System

**问题**: 当前 Agent 是固定代码类（SearchAgent/CreatorAgent/PublishAgent），无法扩展

**方案**:
- Agent 保持为抽象执行单元
- 引入 Skill = Agent 的能力配置（类似 nanobot 的 SKILL.md）
- Skill 包含：提示词、可用工具集、输入/输出契约、使用场景

```
Skill = {
    name: "content-creation",
    description: "Create and publish content",
    prompt: "...",
    tools: ["content.create_draft", "publication.schedule"],
    input_artifacts: ["SEARCH_RESULT", "ANALYSIS_REPORT"],
    output_artifacts: ["DRAFT", "SCHEDULE"],
}
```

**修改模块**: 新增 `skills/` 模块，修改 `agent_registry.py`

---

#### P1-3: Conversation 持久化 & Context Window 管理

**问题**: `conversation_store = {}`（重启丢失），无 Context Window 智能管理

**方案**:
1. Conversation 持久化到 PG（消息历史、SessionContext）
2. 实现 `ContextGovernor`：token 预算、历史截断、大工具结果落盘
3. 参考 nanobot 的 `context_governance.py`

**修改模块**: `context.py`, `conversation_runtime_adapter.py`, 新增 `context/governor.py`

---

### P2 — 高级能力（长期，2-3 个 Phase）

#### P2-1: Reflection & Replan

**方案**:
- 每个 Execution 完成后，Agent Loop 进入 Reflection 阶段
- 评估：目标是否达成？是否有更好的方式？
- Replan：如果 Step 失败，Agent 决定下一步（替代当前的 SKIP_DOWNSTREAM）

---

#### P2-2: Subagent & 委派

**方案**:
- 参考 nanobot 的 `SubagentManager`
- 父 Agent 可以将子任务委派给隔离的 Subagent
- Subagent 有受限的工具集和独立的执行上下文

---

#### P2-3: Event-Driven Agent

**方案**:
- Agent 不只是被动响应消息，可以主动触发
- 基于事件驱动：Task 完成事件 → 触发下一个 Agent
- 基于定时：Scheduled Agent（如每天分析社区趋势）

---

#### P2-4: Autonomous Operation

**方案**:
- Goal Sustenance：参考 nanobot 的 `/goal` 机制
- Agent 持续工作直到目标达成
- 自我中断和恢复

---

## 第五部分：推荐修改顺序

```
Phase 17: Command Interpreter + 退役 agent.py
  ├─ 统一意图识别入口
  ├─ 退役 agent.py 的 L1 关键词匹配
  └─ 新增 CommandInterpreter 层

Phase 18: Agent Loop
  ├─ 新增 agent_runtime/loop.py
  ├─ 实现 Observe→Reason→Act→Reflect 循环
  └─ 简单任务走 Agent Loop，复杂任务委托 Worker

Phase 19: Memory 升级
  ├─ MemoryStore 持久化到 PG
  ├─ Embedding 切换到真实模型
  └─ Memory Consolidation

Phase 20: Hybrid Planning + Replan
  ├─ Planner 支持模板 + Agent 自主规划双模式
  ├─ 支持执行中 Replan（失败时重新规划下游）
  └─ 动态步骤插入/删除/重排

Phase 21: Skill System
  ├─ Agent → Skill 抽象
  ├─ SKILL.md 格式配置
  └─ Skill Marketplace

Phase 22: Context & Conversation 升级
  ├─ Conversation 持久化
  ├─ ContextGovernor (token 预算)
  └─ Session 管理

Phase 23: Subagent + Event-Driven
  ├─ SubagentManager
  ├─ Event-Driven Agent
  └─ Autonomous Operation (Goal Sustenance)
```

---

## 最终评价

### 当前架构的优势

1. **Execution Runtime 成熟度高**：Queue/Worker/Retry/Checkpoint/Evidence 体系完整且安全
2. **类型安全**：Pydantic 驱动的 Contract 体系（ToolContract、IntentSpec、AgentMetadata）
3. **务实的渐进式架构**：双路径共存期间有清晰边界，不盲目追求 "完美重构"
4. **安全设计优先**：Evidence-based Retry（fail-closed）、AuthContext from JWT only、Idempotency-Key
5. **Artifact 系统**：Agent 间产物流转设计合理，Schema Contract 校验

### 当前架构的核心问题

1. **不是 Agent，是 Workflow Engine**：核心执行模型是模板驱动的 DAG 步进，而非 Agent 自主推理
2. **意图识别分散**：三套独立逻辑增加维护成本和 bug 风险
3. **Memory 不可用**：默认 "hashing" embedding 使语义记忆形同虚设
4. **Session 不持久**：`conversation_store = {}` 意味着进程重启丢失所有会话状态

### 与 Operator 类 Agent 的本质差距

GreenBook 在 **执行可靠性** 上做得很好（Queue/Retry/Evidence），但在 **Agent 自主性** 上缺失：

> Workflow Engine 的哲学是 "我知道怎么做，我按预设流程执行"
> Agent 的哲学是 "我不知道怎么做，但我知道如何一步步推理出怎么做"

GreenBook 当前处于前者。要实现 Operator 类 Agent，需要在保留执行可靠性的基础上，增加 Agent Loop（自主推理）和 Hybrid Planning（模板+自由规划）。

### 距离企业级 Agent Platform 还缺什么

1. Agent Loop（自主推理循环） — **P0**
2. 真正的 Semantic Memory — **P0**
3. Session/Context 持久化 — **P1**
4. Skill 系统（可扩展能力） — **P1**
5. Dynamic Planning + Replan — **P1**
6. Subagent（任务委派） — **P2**
7. Reflection（自我评估） — **P2**
8. 多模态输入 — **P2**
9. 水平扩展 & 高可用 — **P2**
10. 安全合规（RBAC、审计报告） — **P2**

---

> **审计完成时间**: 2026-08-11
> **分析文件数**: ~40 个核心 Python 文件
> **参考架构**: nanobot (HKUDS/nanobot)、Claude Computer Use / OpenAI Operator 设计理念
