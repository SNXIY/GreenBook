> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2 Fast Track Cleanup

> Phase 3.5 supersedes the earlier compatibility inventory in this file. The
> authoritative deletion, migration, caller classification, and remaining
> risks are in `PHASE3_5_ARCHITECTURE_CLEANUP.md`.

## Phase 3 Update

- Added the `agent/` Intelligence Layer boundary.
- Kept legacy direct-tool behavior behind a compatibility export; it is not the
  default Command/Goal production route.
- Removed positional selection from multi-tool `CapabilityExecutor` plans.
- No `SearchAgent2`, `CreatorAgent2`, or `PublishAgent2` was added.
- Execution, Artifact, Worker, Queue, Checkpoint, Ledger, ToolRuntime, and MCP
  assets remain in place.

## Phase 2 Update

- Added `goal/` as the new Goal Understanding boundary.
- `GoalDecomposer` is the structured-output source for nested Goals and
  explicit dependencies.
- `GoalCompiler` adapts GoalTree to the existing ConversationTaskGraph and
  TaskPlan contracts.
- No files under `execution/`, `artifact/`, `worker/`, `queue/`,
  `checkpoint/`, `ledger/`, `toolruntime/`, or `mcp/` were deleted or changed
  for Goal Runtime behavior.
- `TaskGraphBuilder`, Intent compatibility, and orchestration templates remain
  fallback paths until their callers are migrated.

## 1. 删除文件 / 代码

- 删除 `packages/assistant_core/greenbook_assistant_core/agent.py` 中的 `_turn_intents`、`_turn_routing_hint`、`_turn_tool_filter`、时间/关键词标记逻辑。
- 删除 `agent.py` 在请求前按用户中文文本筛选工具的默认路径。
- 本轮没有删除 `execution/`、`artifact/`、`worker/`、`queue/`、`checkpoint/`、`ledger/` 或 MCP handler 实现。

## 2. 迁移文件 / 入口

- 新增 `greenbook_assistant_core/command/`：`models.py`、`interpreter.py`、`target.py`、`adapter.py`、`__init__.py`。
- `CommandInterpreter` 使用 LLM JSON Schema structured output 生成唯一的 `Command` 对象。
- `TargetResolver` 成为统一解析 facade，公开 `Resolved`、`Ambiguous`、`NotFound` 三态结果。
- 旧 `conversation` command/resolver 与 `IntentSpecProvider` 通过 `command/adapter.py` 保留兼容投影，供现有 Planner/Runtime 合同消化。
- 新增 `ToolMetadata`、metadata-only `ToolRegistry`，并从 MCP registry 导出 metadata 视图；实际 handler 执行路径不变。
- 默认 API Runtime composition 注入 `CommandInterpreter`；`agent.py` 只作为旧 direct-tool route 的兼容入口。

## 3. 保留原因

- 旧 Intent/Task/Resource contracts 仍被现有测试、TaskProvider、Planner 和执行编译器引用；直接删除会破坏可靠执行层。
- `task/resolver.py`、`task/reference_resolver.py`、`conversation/target_resolver.py` 暂作为兼容 backend，后续在调用面收敛后再物理删除。
- `ToolContract`、MCP `ToolDefinition` 和真实工具 handler 仍是执行与权限校验的既有资产，本轮只增加描述层。
- `orchestration/templates.py` 仍被可靠 Planner 使用，不属于本轮可安全删除的重复路由代码。

## 4. 架构变化

```text
user input
    -> LLM structured output
    -> Command
    -> command.target facade (Resolved | Ambiguous | NotFound)
    -> compatibility adapter -> existing IntentSpec/Planner contracts
    -> Reliable Execution Layer
```

生产路径的语义入口从 `agent.py` 的文本路由移动到 `command/`；执行、工件、队列、检查点、账本、worker 与 MCP 仍保持边界不变。
