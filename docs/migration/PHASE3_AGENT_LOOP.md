> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# Phase3 Agent Loop

## 目标

建立真正的 Agent Intelligence Layer，让 GreenBook 从预先决定执行路径的
Planner-first 流程升级为 Goal-driven Community Agent Runtime：

```text
Command -> GoalTree -> AgentLoop(Observe/Reason/Act/Reflect)
                   -> ToolSelector -> ToolRuntime
                   -> GoalCompiler -> existing Execution Runtime
```

AgentLoop 只决定下一步。Worker、Queue、Retry、Checkpoint、Ledger、Artifact
和 MCP 的可靠执行边界继续由现有 Runtime 拥有。

## 架构变化

- `AgentState` 保存 Goal、当前 Task、对话上下文、ToolMetadata、观察、历史、
  memory snapshot 和 iteration。
- `AgentLoop.run()` 真实执行 Observe、LLM structured Reason、Act、LLM
  structured Reflect 的有界循环。
- `TOOL_CALL` 通过注入的 ToolRuntime，并使用现有 Ledger/幂等/超时边界。
- `CREATE_TASK` 通过 GoalCompiler 生成现有 `ConversationTaskGraph` 和
  `TaskPlan`，再交给现有 Execution Runtime callback。
- `UPDATE_PLAN` 只接受结构化 GoalTree patch，并重新编译验证。
- `ASK_USER` 返回 `WAITING_HUMAN`，不自行猜测目标。
- `ToolSelector` 只消费 ToolMetadata Catalog，以名称校验和结构化输出选择
  工具；不使用 `tool[0]`，不包含 capability-to-tool 固定映射。
- 旧 `agent.py` 通过兼容导出保留给 legacy direct-tool 调用，默认会话路径
  不再依赖其智能判断。

## 新增文件

- `packages/assistant_core/greenbook_assistant_core/agent/__init__.py`
- `packages/assistant_core/greenbook_assistant_core/agent/state.py`
- `packages/assistant_core/greenbook_assistant_core/agent/actions.py`
- `packages/assistant_core/greenbook_assistant_core/agent/selector.py`
- `packages/assistant_core/greenbook_assistant_core/agent/loop.py`
- `tests/unit/test_agent_loop.py`
- `docs/migration/PHASE3_AGENT_LOOP.md`

## 删除文件

没有删除 Reliable Execution Layer 文件。`CapabilityExecutor` 的多工具路径
不再静默取 positional tool；单工具 legacy plan 保持兼容，多个工具必须由
AgentLoop/ToolSelector 提供显式选择。

没有新增 SearchAgent2、CreatorAgent2 或 PublishAgent2。

## 测试结果

- `tests/unit/test_agent_loop.py`: 3 passed。
  - metadata 选择 search tool
  - search 后创建下一步 Task
  - 工具失败后 UPDATE_PLAN 重新规划
- AgentLoop、Goal/Command Runtime、ToolMetadata、CapabilityExecutor 和
  ExecutionWorker 定向回归：32 passed。
- Phase 15–18 与 assistant runtime 兼容回归：52 passed。
- ArgumentBinder 及时间参数绑定回归：23 passed。
- Ruff 与 compileall 通过。

## 当前风险

- AgentLoop 的默认生产路径需要 LLM 返回符合三类 JSON Schema 的 Reason、
  Tool Selection、Reflection；模型或网关不支持 structured output 时会失败，
  不会偷偷退回关键词路由。
- 直接 `TOOL_CALL` 使用 ToolRuntime；需要创建持久 Task/Execution 的动作应
  由 Agent 选择 `CREATE_TASK`，才能进入现有 Worker/Queue 生命周期。
- 旧 Intent/TaskGraph/template 兼容入口仍可达，后续需要逐步排空调用方。
- 当前工作区仍有 Phase 1 记录的全量测试收集错误和 dirty-baseline failures。

## 下一阶段

- 把 Tool permission、approval、cost 和 side-effect 反射接入 AgentAction
  policy gate。
- 为 AgentState 增加持久化 checkpoint adapter，但不改变现有 checkpoint
  source of truth。
- 将 CREATE_TASK 的 Execution callback 接入 queue dispatch 的统一提交接口。
- 继续清理旧 routing/resolver，同时保留可靠执行资产。
