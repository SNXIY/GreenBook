> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2 Fast Track

## Phase 4.5 — Canonical Runtime Cutover (completed)

### Completed

- Canonical API composition is `Command → GoalTree → TaskManager → AgentLoop`.
- `GoalCompiler` now emits graph/plan contracts without an `IntentSpec` graph
  projection.
- Multi-step, side-effecting, long-running, and recoverable Agent decisions
  cross `QueueExecutionSubmissionService → RuntimeAgentService.submit_plan()`
  and enter `ExecutionQueue`.
- `ToolPolicyGate` is enforced from `ToolMetadata`; `PlanValidator` consumes
  exported ToolMetadata policy when the runtime catalog is available.
- Explicit `ExecutionControlCommand` replaces the mixed natural-language
  `TaskCommand` contract.
- Legacy `/runs` control/event endpoints were removed; canonical Execution
  control/event endpoints remain.

### Deleted / retired

- IntentSpecProvider, TaskUnderstanding, intent_compat, old TaskCommand,
  TaskGraphBuilder, legacy multi-task decomposition, duplicate resolver
  implementations, and retired private-API tests.
- `ConversationTaskGraph` remains as a graph contract; only its builder and
  IntentSpec semantic field were removed.

### Added / modified

- `execution/submission.py`: typed queue submission boundary.
- `task/graph_models.py`: graph model and validation only.
- `conversation/control.py`: explicit execution/approval control contract.
- `tests/unit/test_phase4_5_canonical_runtime.py`.
- `docs/migration/PHASE4_5_CANONICAL_RUNTIME_CUTOVER.md`.

### Verification snapshot

- `tests/unit`: 484 passed.
- Phase 4.5 queue cutover tests: 2 passed.
- Key E2E/runtime/evaluator subset: 494 passed.
- Full `pytest -q`: 657 passed, 2 skipped, 3 warnings.
- `pytest --collect-only -q`: 659 tests collected.
- Changed-module Ruff: passed.
- Assistant core/API compileall: passed.

### Remaining risk

- `RuntimeAgentService.execute()` and the old orchestration template compiler
  remain only for explicit resolved-context execution compatibility. They are
  not constructed by the default Conversation Runtime and are listed with
  concrete callers and removal conditions in the Phase 4.5 document.
- `task/registry.py` no longer persists `assistant_task_intents`; canonical
  TaskRepository/TaskManager owns the Task lifecycle.

> The older Phase 1-4 sections below are retained as historical progress notes;
> the Phase 4.5 snapshot above is the current architecture status.

## Phase 4 — Canonical TaskManager & Dynamic Planning (in progress)

### 已完成

- 建立 `task/TaskRepository` Protocol、内存实现，以及连接既有 SQL
  `TaskRegistry` 的适配器。
- 建立 `TaskManager`，统一 Task 创建、GoalTree 绑定、追加/修改、暂停、
  恢复、取消、完成、失败、执行绑定、优先级抢占和 active Task 查询。
- 增加 CREATED、PLANNING、RUNNING、WAITING_HUMAN、WAITING_EXTERNAL、
  PAUSED 等确定性生命周期；保留旧 `IN_PROGRESS` 仅作投影兼容。
- 增加 GoalTree、Task、TaskPlan、ExecutablePlan 的版本字段，以及
  `TaskPlanRevision` replan 审计记录。
- 建立 `DynamicPlanner` 与 typed `PlanningDecision`，AgentLoop 可在
  Reflect 后应用插入、删除、重排、重试参数、询问用户或终止决策。
- 建立 `ToolPolicyGate`，将 ToolMetadata 的 permission、approval、side
  effect、retry、cost/timeout 约束接入 AgentAction 执行前门禁。
- 增加 `ExecutionSubmissionService` typed boundary；CREATE_TASK 可通过
  TaskManager → GoalCompiler → submission boundary 进入现有可靠 Runtime。
- 默认 API composition 已将 canonical TaskManager 连接到现有 TaskProvider
  SQL persistence；未修改 execution/worker/queue/checkpoint/ledger。

### 删除

- 本阶段没有删除仍有真实生产调用方的 Intent/TaskCommand/TaskGraph
  模块；它们已被明确限制为 control、recovery 或 compatibility fallback。

### 新增

- `task/repository.py`
- `task/manager.py`
- `planning/dynamic.py`
- `toolruntime/policy.py`
- `execution/submission.py`
- `db/migrations/007_task_manager_dynamic_planning.sql`
- `tests/unit/test_task_manager.py`
- `tests/unit/test_dynamic_planner.py`
- `tests/unit/test_tool_policy_gate.py`
- `docs/migration/PHASE4_TASK_MANAGER_DYNAMIC_PLANNING.md`

### 修改

- Task/Goal/Plan/AgentState/AgentLoop/ToolMetadata 模型增加 canonical
  生命周期、策略和版本契约。
- AgentLoop 支持 DynamicPlanner、ToolPolicyGate、TaskManager 和
  ExecutionSubmissionService 注入。
- GoalDecomposer 不再把风险、审批和副作用策略作为 LLM 的规划输入；
  这些决策统一回到 ToolMetadata policy gate。
- API 默认 runtime 创建 canonical Task，并通过 submission boundary 复用
  现有 RuntimeAgentService/Execution Runtime。

### 测试

- Phase 4 新增 TaskManager、DynamicPlanner、ToolPolicyGate 测试，并覆盖
  单/多 Goal、独立 Task、抢占恢复、replan、policy deny/approval/queue。
- 新增测试与 AgentLoop 回归合计：`79 passed`。
- 既有执行/运行时聚焦回归：`74 passed`。
- assistant core、contracts、assistant API `compileall`：通过。
- `tests/unit` 全量收集仍被 5 个既有已退役私有 API import 阻断；本阶段新增
  测试没有收集失败。
- 使用项目 `.venv\Scripts\ruff.exe` 对 Phase 4 及其接入文件检查：通过；裸
  `ruff` 命令未加入 PATH。

### 当前风险

- `_skip_multi` recovery 仍经由 IntentSpec/TaskGraph projection；下一步需
  改为 queue-native GoalCompiler submission 后才能删除旧 Intent。
- CapabilityRegistry 的旧 policy 字段仍被 legacy PlanValidator/Executor
  读取，AgentAction 新路径已经以 ToolMetadata 为唯一 policy source。
- 新 Task 字段已增加 `007_task_manager_dynamic_planning.sql` 正式迁移；启动时
  additive DDL 仅作为旧环境的兼容保护。

### 下一步

- 迁移剩余 TaskProvider/TaskCommand/IntentSpec/TaskGraphBuilder 调用方。
- 把多步骤和副作用 Plan 的 submission 完整接到 ExecutionQueue，再删除
  fallback；保持 Worker、Retry、Checkpoint、Ledger 和 MCP 不变。
- 在 CI 安装 Ruff 并完成本阶段文件的 lint 检查。

## Phase 3.5 Architecture Cleanup (completed)

### 已完成

- 删除 `agent.py`、`agent_runtime/` 和 `orchestration/agent_registry.py`，默认生产路径只保留 `agent/AgentLoop`。
- 删除重复 Intent、Resolver、Resource 和 fake Agent wrapper；`execution/`、`artifact/`、Worker、Queue、Checkpoint、Ledger、ToolRuntime、MCP 保留。
- 统一目标解析到 `command/target.py`，结果为 `Resolved`、`Ambiguous` 或 `NotFound`。
- 将 `task/resolver.py` 重命名为 `task/target_evidence.py`，明确其仅服务 TaskProvider 兼容投影。
- 移除 PlanStep/ConversationGoalNode 的 fake Agent owner 字段；Artifact 历史 provenance 字段保留但不再生成 SearchAgent/CreatorAgent 等运行时 owner。
- `TaskOrchestrator` 保留为 Goal/TaskPlan 编译兼容层；templates 明确降级为 fallback。

### 删除

- 旧 `agent.py`、`agent_runtime/*`、AgentRegistry。
- `conversation/target_resolver.py`、`task/reference_resolver.py`、`task/decomposer.py`、`resource/*`。
- 重复 Intent draft/elements 及其 compatibility 实现。
- 只验证已退休架构的旧 Agent、resolver、decomposer、group executor 测试。

### 新增 / 迁移

- `docs/migration/PHASE3_5_ARCHITECTURE_CLEANUP.md`
- `task/target_evidence.py` 与 `TaskTargetEvidenceProvider`。
- canonical target candidate contract 在 control/approval services 中统一使用。

### 修改

- `RuntimeContainer` 不再创建 AgentRegistry。
- `ExecutionWorker` 直接持有 `CapabilityExecutor`，不再经过 fake Agent executor wrapper。
- API message route 始终进入 ConversationRuntimeAdapter；旧 direct-tool routing 不再参与默认路径。
- 兼容保留 `TaskCommand`、IntentSpecProvider、TaskGraphBuilder、TaskOrchestrator 和 templates，均标记为有调用方的 fallback。

### 测试

- Phase 3.5 受影响回归：`119 passed`。
- compileall：通过。
- 本阶段变更文件 Ruff：通过。
- 全仓 Ruff 仍有 1452 条既有 lint/import 基线问题；本阶段变更范围单独通过。
- API `main` 导入：通过。
- 全量收集仍有 6 个基线阻塞：缺少 `yaml`，以及已退休的 `_run_projection_fields`、`_conversation_target_task`、`_append_schedule_confirmation`、`_close_request_db_session` 私有 API 导入。

### 当前风险

- TaskCommand/Intent compatibility 仍有少量生产 fallback 调用方；下一阶段迁移 TaskManager 后删除。
- Capability catalog 仍保存部分旧语义字段；ToolMetadata 已是 Agent 发现入口，但 policy drift 的最终清理尚未完成。
- Artifact 历史 Agent provenance 列需要独立 schema migration，暂不删除。

### 下一步

- 建立 canonical TaskManager/TaskRepository，排空 IntentSpecProvider、TaskGraphBuilder 和 TaskCommand fallback。
- 让 ToolMetadata 成为 risk/permission/approval/side-effect/retry/cost 的唯一 policy source。
- 在新的边界上继续 Dynamic Planning 和 Memory，不回到旧 Agent/Intent 路径。

## Phase 3 — Agent Intelligence Layer

### Completed

- Added `agent/` AgentState, AgentAction, ToolSelector, AgentLoop, and
  reflection contracts.
- Connected canonical Command + GoalTree to AgentLoop in the default
  conversation composition.
- TOOL_CALL now uses ToolMetadata + ToolRuntime; CREATE_TASK uses GoalCompiler
  and the existing Execution Runtime callback.
- Removed positional multi-tool selection from CapabilityExecutor; explicit
  runtime selection is required for multi-tool capabilities.
- Kept legacy `agent.py` available only through a compatibility export and did
  not add a second business Agent family.

### Added

- `packages/assistant_core/greenbook_assistant_core/agent/`
- `tests/unit/test_agent_loop.py`
- `docs/migration/PHASE3_AGENT_LOOP.md`

### Tests

- AgentLoop minimum tests: `3 passed`.
- AgentLoop/Goal/Command/ToolMetadata/CapabilityExecutor/ExecutionWorker
  selected regression: `32 passed`.
- Phase 15–18 and assistant runtime selected regression: `52 passed`.
- ArgumentBinder and temporal binding regression: `23 passed`.
- Ruff and compileall: passed.

### Current Risk

- Structured Reason/Selection/Reflection requires an LLM response compatible
  with the declared JSON Schemas.
- Existing full-suite collection and dirty-baseline failures remain documented
  in the Phase 2 report.

### Next

- Add policy gates for permission, approval, side effects, and cost.
- Route CREATE_TASK submission through the canonical queue dispatch adapter.

## Phase 2 — Goal Decomposition

### Completed

- Added Pydantic `Goal`, `GoalTree`, and `TaskNode` contracts.
- Added LLM structured-output `GoalDecomposer`; no keyword or fixed-template
  decomposition is used.
- Added `GoalCompiler` for `GoalTree -> ConversationTaskGraph` and
  `GoalTree -> existing TaskPlan` compatibility compilation.
- Added Planner GoalTree entry point and wired the default conversation runtime
  composition to reuse the existing graph execution path.
- Downgraded `orchestration/templates.py` to a legacy/recovery fallback.

### Added

- `packages/assistant_core/greenbook_assistant_core/goal/`
- `tests/unit/test_goal_decomposer.py`
- `docs/migration/PHASE2_GOAL_DECOMPOSITION.md`

### Tests

- Goal decomposition and Planner boundary tests: `4 passed`.
- Goal/Command/task-graph/runtime regression selection: `63 passed`.
- Full suite: 5 pre-existing collection errors; excluding them,
  `887 passed, 54 failed, 2 skipped`.

### Current Risk

- Legacy Intent, graph, and template callers remain reachable while migration
  continues. Existing full-suite collection and dirty-baseline failures from
  Phase 1 remain documented below.

### Next

- Drain remaining graph-producing callers into Goal Runtime, then remove only
  duplicate compatibility implementations whose callers are gone.

## 已完成

- 建立 `packages/assistant_core/greenbook_assistant_core/command/` canonical boundary。
- 接入 LLM structured output、统一 target facade、旧 Intent/Conversation adapter。
- 清理 `agent.py` 的关键词路由与工具过滤。
- 建立 `ToolMetadata` 与 metadata-only `ToolRegistry`，MCP 可导出统一 metadata。
- 默认 API Runtime composition 注入 `CommandInterpreter`。

## 删除

- `agent.py`：`_turn_intents`、`_turn_routing_hint`、`_turn_tool_filter` 及其 marker/time helper。
- 未删除 Reliable Execution Layer 与 MCP 执行实现。

## 新增

- `command/__init__.py`
- `command/models.py`
- `command/interpreter.py`
- `command/target.py`
- `command/adapter.py`
- `toolruntime/__init__.py`
- `toolruntime/metadata.py`
- `toolruntime/registry.py`
- `compatibility/intent/provider.py`
- `tests/unit/test_command_runtime.py`
- `docs/migration/FAST_TRACK_CLEANUP.md`

## 修改

- `agent.py`：降级为 legacy direct-tool compatibility entry。
- `conversation_runtime_adapter.py`：接入 command runtime、canonical adapter 与 target facade。
- `main.py`：为默认 Runtime composition 注入 `CommandInterpreter`。
- shared contracts/MCP registry：增加 ToolMetadata discovery view，不改真实执行。
- `conversation.target_resolver`：共享 canonical resolution status。

## 测试

- Command Runtime 最小测试：3 passed。
- Tool contract infrastructure：4 passed。
- Phase18-A conversation runtime：11 passed。
- Legacy assistant runtime contracts：9 passed。
- Resolver/continuous runtime 定向回归：38 passed。
- Command/adapter/metadata 最终定向回归：55 passed。
- 本轮新增/触及路径 Ruff：通过。
- 全量测试（排除 5 个收集阻塞项）：883 passed、54 failed、2 skipped；失败集中在当前工作区既有 Runtime/API 基线与旧关键词路由断言。
- 全量收集仍有 5 个既有阻塞：缺少 `yaml`，以及旧 API 符号 `_run_projection_fields`、`_conversation_target_task`、`_close_request_db_session` 不存在。

## 当前风险

- `IntentSpecProvider`、`task/resolver.py`、`task/reference_resolver.py` 仍保留兼容实现，尚未物理归档。
- 多目标/多任务 graph 仍需要下一轮把 graph structured output 完整纳入 Command Runtime。
- 一些旧测试临时目录有 Windows 权限警告；不影响代码断言。
- 根 workspace 的 `uv` 目前因既有 `tool.uv.sources` 配置缺失无法生成 metadata，本轮测试使用现有 `.venv` 直接运行。
- 旧 `agent.py` 关键词路由测试会按本轮目标失败；该行为已被明确删除，不恢复兼容垃圾逻辑。

## 下一步

- 将 graph/decomposition 收敛为 Command Runtime 的批量 structured output。
- 迁移剩余 resolver 调用到 `command/target.py` 后删除旧 resolver 实现。
- 将工具发现、权限、成本与 side-effect metadata 接入 Agent Intelligence prompt/selector。
- 完成 legacy message route 下线与默认路径审计。
