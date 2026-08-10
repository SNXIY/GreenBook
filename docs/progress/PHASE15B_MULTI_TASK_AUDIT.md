# Phase15-B Multi-Task / Multi-Goal Audit and Implementation

日期：2026-08-10

## 结论

原有代码具备部分基础，但还不是完整的 Conversation 多任务 Runtime：

| 能力 | 审计结果 | 处理 |
|---|---|---|
| Conversation 内多个 Task | `TaskRegistry` 可存储多个 Task，但生产消息入口每轮只调用一次 adapter | 已接入保守拆分和逐 Task dispatch |
| 单轮多个 Task | `TaskDecomposer`/`GroupExecutor` 存在但未接入消息路径，且中文分隔规则不可靠 | 已接入 `split_task_segments`；每个分段独立理解、建 Task、进入原有 Runtime |
| Task 多 Goal | `Task` 只有单 goal 字段 | 新增 `TaskGoal`，并让 `TaskOrchestrator.generate_goal_plan()` 绑定现有 DAG |
| Task / Execution 边界 | Execution 已独立，但 Task 没有关联索引 | 新增 `TaskExecutionRef`，只做 projection，不改变 ExecutionState |
| 跨轮 UPDATE | TaskProvider 已有 scoped target，但没有会话级历史投影 | 新增 Goal/Execution/Resource projection 和持久化字段 |
| 跨轮 CANCEL | 原有 Task CANCEL 容易与 schedule cancel 混淆 | 新增 `CANCEL_SCHEDULE` 历史标记；不取消其他 Task |
| 弱引用 | 原 `TaskReferenceResolver` 依赖旧字符串规则，序数需要 `TaskGroup`，不能稳定使用会话 Task 索引 | 新增结构化 resolver：标签、资源、序数、取消历史、标题修改历史、歧义返回 |
| QUERY + ACTION | `QUERY_TASK/DIRECT` 会进入创建或 Runtime 路径 | Query 分流为无 Task、无 Execution 的只读结果 |
| Artifact 传递 | Planner 已有 `SEARCH_RESULT -> ANALYSIS_REPORT -> DRAFT` 引用 | 复用现有 `ArtifactRef`/ArtifactStore；未建立第二套 artifact 系统 |

## 领域边界

```text
Conversation
  ├── Task A (长期业务目标)
  │    ├── Goal A1 ...
  │    └── Goal A2 ...
  └── Task B
       └── Goal B1 ...

Goal -> existing TaskPlan step -> Execution -> ArtifactRef/resource
```

`TaskGoal` 是语义目标，`PlanStep` 是一次计划中的可执行节点，`Execution` 仍由
现有 Runtime/Queue/Worker 管理。`CREATE_DRAFT` 作为语义 Goal 映射到现有
`GENERATE_CONTENT` 的 DRAFT artifact，不新增 ToolRuntime capability。

## 本阶段修改

- `task/models.py`：TaskGoal、TaskExecutionRef、TaskResourceRef，以及 Task projection 字段。
- `task/registry.py`：projection 的 PostgreSQL 字段和幂等 additive schema upgrade。
- `task/multi_task.py`：多任务分段、IntentDelta、ConversationTaskIndex、结构化 TargetResolver。
- `orchestration/models.py`、`orchestration/orchestrator.py`：多 Goal 计划绑定已有 DAG。
- `conversation_runtime_adapter.py`：生产 Runtime 入口支持多个独立 Task；每个 child 独立调用原 Runtime，Query 不创建执行。
- `task_provider.py`：持久化 Task 会话 projection。
- `routes.py`：多 execution/task ID 兼容投影。
- `tests/unit/test_phase15b_multi_task.py`：拆分、目标解析、取消历史、multi-goal DAG、adapter 集成回归。

## 不在本阶段修改

没有修改 ExecutionStateManager、ExecutionQueue、Assistant Worker、ToolRuntime 基础可靠性、Planner/Intent 核心业务逻辑、Java 业务模型、Creator 创作逻辑；没有 Kafka、Docker Compose 或 Legacy Cleanup。

## 仍需关注

1. 当前自然语言分段器只处理明确的“第一/第二”、数字序号和明确的“然后查询”结构；没有把任意复杂中文话语交给规则强拆。
2. Query 的执行结果目前是只读 presentation boundary；若要访问实时外部数据，应增加独立 read-only query handler，不能复用写 Execution。
3. Task projection 写失败不会让已完成 Runtime 失败，下一轮会从 Task store 重建；生产环境仍应监控 projection 写失败。
4. 现有旧测试文件包含历史编码损坏文本，本阶段没有进行全仓编码清理。
