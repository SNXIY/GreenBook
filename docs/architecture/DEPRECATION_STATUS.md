# GreenBook Agent Runtime Deprecation Status

> Phase 7.3-A。本文记录 Legacy 和 Compatibility 的当前边界。
> 本阶段不删除文件，不改变业务逻辑，不修改 Planner、Worker 或 Execution Runtime。

## ACTIVE

当前正式生产路径：

```text
User Message
  -> TaskUnderstanding
  -> Direct IntentSpec
  -> IntentValidator / Targeted Repair
  -> PlanningContext
  -> Planner / TaskOrchestrator
  -> TaskPlan
  -> PlanExecution
  -> ExecutionStateManager
  -> ExecutionWorker
  -> CapabilityExecutor / ToolRuntime
  -> MCP / Java Backend / Creator
```

核心 ACTIVE 模块：

- `task/intent_models.py`：IntentSpec schema 和枚举；
- `task/understanding.py`：L1/L2 理解入口，Direct IntentSpec 是正式 L2 路径；
- `task/intent_validator.py`：结构化校验；
- `task/intent_preprocessor.py`：结构提示生成；
- `orchestration/` 和 `planning/`：PlanningContext、TaskPlan、Planner；
- `execution/`：PlanExecution、StateManager、Worker、Guard、Retry、Checkpoint、Event、Persistence；
- `capability/`：Capability Registry 和 CapabilityExecutor；
- `execution/runtime/`：ToolRuntime、InvocationContext、Ledger；
- `artifact/`、`human/`、`observability/`、`evaluation/`：运行支撑能力。

ACTIVE 路径不得新增对 Draft、Elements、Legacy Agent 或旧 Run implementation 的直接依赖。

## COMPATIBILITY

### IntentDraft

文件：

- `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py`
- `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py`

原因：

- 旧测试和外部调用可能仍使用 `task.intent_draft`；
- 需要保持历史 import 可用；
- 早期 Draft 到 IntentSpec 的行为仍可用于兼容回归。

迁移目标：

```text
IntentDraft -> IntentSpec
```

状态：Deprecated。Do not extend。

建议：保留 shim，停止新增功能；确认无外部调用后迁移到 ARCHIVE。

### IntentElements

文件：

- `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py`
- `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py`

原因：

- 历史测试仍覆盖 `ActionMention`、`ConditionMention` 和 Builder 行为；
- 旧 import 需要在迁移窗口内保持兼容。

迁移目标：

```text
IntentElements -> IntentSpec
```

状态：Deprecated。Do not extend。

建议：保留 shim，停止新增功能；确认无外部调用后 ARCHIVE。

### TaskIntent

文件：

- `packages/assistant_core/greenbook_assistant_core/task/models.py`
- `apps/assistant_api/greenbook_assistant_api/models/runtime_context.py`

原因：

- L1 仍然直接产生 TaskIntent；
- Resolver、ReferenceResolver、ResourceResolver 和部分旧 Planner wiring 仍读取 TaskIntent；
- API/旧 Runtime 仍需要兼容字段。

迁移目标：

```text
TaskIntent -> PlanningContext(IntentSpec-first)
```

状态：Compatibility，但不是可删除模块。

建议：保持字段语义不变，逐步减少新 Runtime 对 TaskIntent 的读取。

### intent_compat

文件：

`packages/assistant_core/greenbook_assistant_core/task/intent_compat.py`

原因：

- 将 IntentSpec 投影为旧 TaskIntent；
- 支持旧 API、Resolver 和评测中的 legacy projection；
- 避免一次性迁移破坏既有调用方。

迁移目标：

```text
legacy TaskIntent consumer -> direct IntentSpec consumer
```

状态：Deprecated compatibility adapter。Do not extend。

建议：只允许维护兼容映射，不在其中增加新的意图语义。

## LEGACY

### Legacy Agent

文件：

- `packages/assistant_core/greenbook_assistant_core/agent.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`

引用位置：

- `apps/assistant_api/.../main.py` 无条件创建 `LegacyAgentService`；
- `AssistantService` 保留 Legacy fallback；
- `ASSISTANT_RUNTIME_MODE=off` 时 Legacy 仍可能成为默认执行路径；
- integration、revision 和 RuntimeRouter 测试仍直接覆盖旧行为。

删除条件：

1. Runtime 默认路径切换为 ACTIVE；
2. Legacy fallback 关闭并经过生产流量观察；
3. 旧 API run/approval/event contract 已有明确替代方案；
4. 直接导入 `CommunityOperationsAssistant` 的测试已迁移到 compatibility；
5. 完成全量 unit、integration、contract、evaluation、e2e 回归；
6. 已准备回滚方案。

状态：LEGACY。建议 MIGRATE 后 ARCHIVE，当前不可删除。

### LegacyRunHistoryRepository / assistant_runs

文件：

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中的 `LegacyRunHistoryRepository`；
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` 中 `RunResponse` 和 `_InMemoryRunRepo`；
- `assistant_runs` 表定义。

`LegacyRunHistoryRepository` 只负责 Legacy history/projection metadata，不
负责 Runtime status、events、progress、checkpoint 或 retry state。旧
`RunRepository` 名称保留为 compatibility alias，并标记为 deprecated；新
代码必须使用 `LegacyRunHistoryRepository`。

引用位置：

- API 的 run 查询、列表、取消和中断接口；
- 旧 approval 和 conversation persistence；
- integration、contract 测试；
- `run_id` 仍出现在 RuntimeContext、旧工具 audit 和客户端 header。

删除条件：

1. 明确 `ConversationRun` 与 `PlanExecution` 的 API 分层；
2. 所有 execution 状态查询改用 `execution_id` 和 PlanExecution；
3. 旧 run API 完成版本迁移或下线；
4. 数据表迁移和历史数据保留策略完成；
5. 旧 `run_id` header/字段不再是业务状态关联键。

状态：LEGACY persistence surface，但当前仍是生产 API 依赖。建议 MIGRATE，不可立即删除。

### `run_id`

`run_id` 当前不是单一语义：

- 旧 API conversation turn id；
- Legacy Agent execution id；
- 工具调用 audit header `X-Agent-Run-Id`；
- 部分测试 fixture id。

删除条件：

- 全部执行状态统一到 `execution_id`；
- 工具审计完成 `execution_id` 字段迁移；
- 旧 API contract 完成版本升级；
- 兼容期结束并完成数据迁移。

建议：保留为兼容字段，不在新 Runtime 中扩展其语义。

## Import Boundary

### YES：允许

生产代码允许通过明确的 compatibility adapter 使用历史类型：

```text
Active consumer
    -> compatibility adapter
    -> legacy representation
```

允许的边界包括：

- `IntentSpec -> intent_compat.to_task_intent() -> TaskIntent`；
- 旧外部 import -> `task.intent_draft` shim；
- 旧外部 import -> `task.intent_elements` shim；
- Legacy API -> 明确的 Legacy service adapter；
- 旧 Run API -> ConversationRun/Execution 映射 adapter。

### NO：禁止

正式生产主路径禁止：

- `TaskUnderstanding` 顶层 import `IntentDraft` 或 `IntentElements`；
- Direct L2 调用 Draft/Compiler 或 Elements/Builder；
- Planner 直接使用 Draft/Elements；
- Worker 或 Execution Runtime 直接读取 Legacy Agent 状态；
- 新 API 用 `run_id` 代替 `execution_id` 查询 PlanExecution；
- 新业务逻辑直接 import `community-assistant-agent`；
- 新功能扩展 Deprecated compatibility implementation。

### 当前边界例外

`task/understanding.py` 中仍存在两个历史方法：

- `_llm_understand_draft()`；
- `_llm_understand_elements()`。

它们使用 lazy import，未被 Direct IntentSpec 主路径调用，但仍是生产类中的 legacy method。最终目标是将它们移入独立 compatibility adapter；在完成前，将其视为受控过渡例外，而不是 ACTIVE 依赖。

## 风险

| 风险 | 影响 | 当前状态 | 缓解措施 |
|---|---|---|---|
| API 默认仍可能走 Legacy | 新旧行为不一致 | 高 | 先切换 Runtime mode，再观察和回归 |
| `TaskIntent` 语义继续扩张 | IntentSpec/TaskIntent 再次分叉 | 中 | Deprecated 标记，禁止新增字段承载新语义 |
| `run_id` 与 `execution_id` 混淆 | 状态查询或审计关联错误 | 高 | 新 API 强制 execution_id，旧 run 单独命名 |
| shim 被新代码直接使用 | 历史实现无法退出 | 中 | import lint/check，新增代码禁止使用旧路径 |
| 两套 Creator Agent 未明确 owner | 部署和版本来源不一致 | 中 | 完成 contract、CI、Docker 和 health audit |
| compatibility 实现继续增加功能 | 迁移窗口无限延长 | 中 | Deprecated docstring、code owner 和变更门禁 |

## 后续删除门槛

任何 LEGACY 文件进入删除流程前，必须满足：

1. 全仓库 import 扫描无生产直接依赖；
2. compatibility adapter 已覆盖外部 contract；
3. unit、integration、contract、evaluation、e2e 全量通过；
4. 数据库和 API 迁移完成；
5. 生产流量已验证无回滚需求；
6. 有明确的 owner、迁移记录和回滚方案。
