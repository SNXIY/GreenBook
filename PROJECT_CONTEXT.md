# GreenBook Agent Runtime — Project Context

> 日期: 2026-08-07
> 测试基线: **542 passed, 0 failed**
> 架构: Task-oriented Agent Runtime (Phase 6.x)

---

# 1. 当前整体架构

```
routes.py (HTTP 层)
    │
    ▼
AssistantService
    ├── LegacyAgentService (agent.py — 旧路径, fallback)
    └── RuntimeAgentService (新路径)
          │
          ├── TaskDecomposer → TaskGroup → GroupExecutor (并行, DAG)
          ├── TaskUnderstanding (L1 + L2)
          ├── TaskResolver + TaskReferenceResolver
          ├── ResourceResolver
          ├── CapabilityMapper → Orchestrator → PlanValidator
          ├── ExecutionWorker → CapabilityExecutor → ToolRuntime
          ├── ArtifactStore
          ├── HumanInteractionManager (APPROVAL / CLARIFICATION / INPUT)
          ├── MemoryManager (EPISODIC / SEMANTIC / PROCEDURAL)
          └── TraceCollector + AgentTrace
```

## 关键分包

```
packages/assistant_core/greenbook_assistant_core/
    agent.py            — 旧 agent (保留, fallback)
    task/               — TaskIntent, TaskUnderstanding, TaskResolver,
                          TaskDecomposer (SubTaskContext, TaskGroup, TaskDependency),
                          TaskReferenceResolver
    resource/           — ResourceRequest, ResourceTarget, ResourceResolver
    capability/         — Capability, CapabilityRegistry, CapabilityMapper
    orchestration/      — PlanStep, TaskPlan, PlanTemplate, TaskOrchestrator
    planning/           — ExecutablePlan, PlanValidator
    execution/          — PlanExecution, StepExecution, ExecutionWorker,
                          CapabilityExecutor, StepScheduler
    execution/runtime/  — ToolRuntime, ToolInvocationContext, ToolExecutionLedger
    execution/group_scheduler.py — DAG 并行调度
    artifact/           — Artifact, ArtifactStore
    observability/      — Trace, TraceEvent, TraceCollector, AgentTrace
    human/              — HumanInteractionRequest/Response, HumanInteractionManager
    agent_memory/       — MemoryRecord, MemoryStore, MemoryManager,
                          ProceduralMemoryExtractor, StrategyRetriever
    db/                 — PostgreSQL repositories (conversation, message, run, approval)

apps/assistant_api/greenbook_assistant_api/
    models/             — RuntimeContext, RuntimeResult
    services/           — AssistantService, LegacyAgentService,
                          RuntimeAgentService, GroupExecutor
    api/                — routes.py (HTTP routes), tool_helpers.py
```

---

# 2. 已完成 Phase (6.0 ~ 6.7)

| Phase | 内容 | 关键交付 |
|-------|------|---------|
| 6.0.1 | TaskDecomposer | SubTaskContext, 拆分/合并/引用检测 |
| 6.1 | GroupExecutor | DAG 串行执行, 依赖解析, 结果聚合 |
| 6.2.1 | Group Trace | GROUP_CREATED, SUB_TASK_*, GROUP_COMPLETED |
| 6.2.2-A | TaskReferenceResolver | 时间引用("昨天"), 序数引用("第一篇"), 歧义检测 |
| 6.2.2-B | ReferenceResolver 集成 | 接入 _execute_single, clarification pause |
| 6.3 | Evaluation Platform | EvalCase, EvalRunner, MetricsCalculator, 2 datasets (30 cases) |
| 6.4 | DAG Parallel | GroupScheduler, asyncio.gather, Semaphore |
| 6.5 | Human-in-the-Loop | HumanInteractionManager, APPROVAL/CLARIFICATION/INPUT 统一 |
| 6.6 | Agent Memory | Episodic/Semantic/Procedural, Write + Recall |
| 6.7 | Complex Ops Enhancement | L1: +"运营", +ANALYZE, +conditional pattern |

---

# 3. 核心模块职责

| 模块 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `TaskUnderstanding` | 自然语言→结构化意图 | user_message + existing_tasks | TaskIntent |
| `TaskDecomposer` | 复合消息→SubTask列表 | user_message + TaskUnderstanding | list[SubTaskContext] |
| `TaskResolver` | hint→Task匹配 | intent.hint + tasks | ResolvedTaskTarget |
| `TaskReferenceResolver` | 时间/序数引用→Task | hint + tasks + group | ReferenceResolution |
| `ResourceResolver` | 资源操作→资源ID | ResourceRequest[] + tasks | ResourceTarget[] |
| `Orchestrator` | requirements→CapabilityDAG | goal_category + requirements | TaskPlan (template) |
| `PlanValidator` | Plan→ExecutablePlan | TaskPlan + CapabilityRegistry | ExecutablePlan |
| `ExecutionWorker` | DAG执行 | ExecutablePlan | PlanExecution (status) |
| `CapabilityExecutor` | Capability→Tool调用 | PlanStep + registry | ExecutionResult |
| `ToolRuntime` | Tool调用+idempotency+ledger | ToolInvocationContext | InvocationResult |
| `ArtifactStore` | Artifact CRUD + 跨Step传递 | ExecutionResult | Artifact |
| `GroupExecutor` | TaskGroup 并行/串行执行 | TaskGroup + RuntimeContext | RuntimeResult |
| `HumanInteractionManager` | 暂停/恢复/过期 | InteractionRequest | InteractionResponse |
| `MemoryManager` | 记忆写入/召回 | MemoryRecord/Query | MemoryRecord[] |
| `GroupScheduler` | DAG拓扑排序+批次 | TaskGroup | list[ExecutionBatch] |
| `TraceCollector` | 事件收集+时间线查询 | TraceEvent | timeline |

---

# 4. 当前 TaskUnderstanding 问题

## 4.1 L1 关键词覆盖不足

| 缺失信号 | 示例 | 影响 |
|---------|------|------|
| "运营"、"策划" | "帮我运营一个专题" | ❌ 已修复 (6.7) |
| "分析"、"总结" | "分析原因" | ❌ 已修复 (6.7) |
| 条件检测 | "有则修改，无则创建" | ❌ 已修复 (6.7) |
| "整理"、"编排"、"规划" | "规划内容发布" | ⚠️ 部分覆盖 |
| "对比"、"比较" | "对比两个方案" | ❌ 未覆盖 |

## 4.2 L2 (LLM) 调用策略问题

| 问题 | 现状 |
|------|------|
| `_needs_l2()` 太保守 | 只对 AMBIGUOUS_VERBS + composite markers 触发 |
| 长度>80 + "要求" → 应触发 L2 | 未实现 |
| L2 结果可能被 L1 fallback 覆盖 | L2 失败时回退到 L1, 可能丢失语义理解 |

## 4.3 复合任务识别

| 场景 | 期望 | 现状 |
|------|------|------|
| 编号列表 "1.xxx 2.xxx" | 拆分 | ⚠️ 部分 (NUMBERED_PATTERN 已存在) |
| "运营" 类复杂目标 | COMPOSITE | ⚠️ 刚修复, 需验证 |
| "如果X则Y" 条件 | 条件分支 | ❌ 未实现 |

---

# 5. TaskUnderstanding 2.0 目标

## 5.1 短期 (Phase 7.0)

1. **L2 触发增强**: 长度>80 + "要求/需求/目标" → escalate to L2
2. **L1 覆盖补全**: "规划"、"编排"、"对比"、"评估"
3. **L2 Prompt 优化**: 增加运营类复合任务 few-shot
4. **Intent Confidence 分级**: L1(0.85) vs L2(0.70) → prefer L2 for complex

## 5.2 中期 (Phase 7.1+)

1. **L2 缓存**: 相似消息复用上次 L2 结果
2. **Conversation-level context**: L2 prompt 注入历史 Task summary
3. **Multi-intent per message**: 一个消息支持多个 goal_category (composite)

## 5.3 长期

1. **Embedding-based Intent Classification**: 替代关键词 L1
2. **Few-shot learning from Memory**: 从 Procedural Memory 中学习意图模式

---

# 6. 禁止破坏的接口

以下模块/接口的签名和行为**必须保持不变**：

```
agent.py                    — CommunityOperationsAssistant.run()
LegacyAgentService          — execute(ctx) → RuntimeResult
MCP (全部 16 tools)         — execute_tool(tool_name, auth, session, ...)
Java Backend API            — 全部 REST 接口
Creator Agent API           — create_task / wait_for_completion / get_artifact

RuntimeContext              — 字段只能新增, 不能删除/重命名
RuntimeResult               — status 值向后兼容

Worker._execute_one_step()  — 内部调用链不变
ToolRuntime.invoke()        — 签名不变
CapabilityExecutor          — 支持 tool_handler + invoke_fn 双模式
```

### 允许扩展的接口

```
TaskIntent                 — 可新增字段 (如 resource_requests 的添加)
RuntimeContext              — 可新增字段 (如 memory_context, resolved_resources)
ExecutionStatus             — 可新增枚举值 (如 WAITING_HUMAN)
EventType                   — 可新增事件类型
```

---

# 7. 测试基线

```
总测试数: 542
通过率: 100%

分类:
  tests/unit/       — 单元测试 (~400+)
  tests/integration/ — 集成测试 (~5)
  tests/evaluation/  — 评测框架测试 (36)
  tests/e2e/         — 端到端测试 (11)
  tests/contract/    — 契约测试 (~8)

关键测试文件:
  test_runtime_agent_service.py  — RuntimeAgentService 执行
  test_group_executor.py         — GroupExecutor DAG 执行
  test_parallel_group.py         — DAG 并行执行
  test_multiturn_pipeline.py     — 多轮任务
  test_phase56_resource_binding.py — ResourceResolver
  test_task_decomposer.py        — TaskDecomposer
  test_human_interaction.py      — HumanInteraction 基础设施
  test_agent_memory.py           — Memory 基础设施
  test_long_term_content_revision.py — 3 轮 E2E
  test_java_topic_operation_workflow.py — 7 阶段运营 E2E
```

---

# 8. 已知限制

| 限制 | 说明 |
|------|------|
| L1 关键词依赖 | 中文表达变化多, 关键词覆盖不完整 |
| L2 未默认启用 | ASSISTANT_RUNTIME_MODE=off 默认走旧 agent.py |
| Memory 仅在 Runtime 路径 | 旧 agent.py 路径不写 Memory |
| 无跨 Conversation Memory | Memory 只在 session 内, 未跨会话 |
| 无向量搜索 | Memory 搜索使用关键词, 无语义向量 |
| GroupExecutor 无嵌套分组 | TaskGroup 内不能再有 TaskGroup |
| 旧 agent.py 与 Runtime 双轨 | 部分逻辑重复 (tool_handler, event 发射) |
