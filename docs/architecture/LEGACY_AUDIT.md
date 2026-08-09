# GreenBook Agent Runtime Legacy Dependency Audit

> Phase 7.2-A。本文是只读架构审计报告。审计期间不删除、不移动、不修改代码。

## 1. 审计结论

当前 GreenBook Runtime 的正式主路径是：

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
  -> MCP / Java / Creator
```

审计发现四类历史依赖仍存在：

1. `TaskIntent` 是 L1 和旧下游仍使用的兼容模型，不是可删除代码。
2. `IntentDraft`、`IntentElements` 已不属于 Direct IntentSpec 主路径，但仍由 legacy methods、shim 和兼容测试引用。
3. `LegacyAgentService` 仍由 API 入口无条件创建，且默认 `ASSISTANT_RUNTIME_MODE=off` 时仍可能执行。
4. 旧 `RunRepository`/`assistant_runs` 仍服务 API 的旧回合接口，不能与 `PlanExecution` 混为同一状态源，也不能立即删除。

当前没有可以不经部署验证就安全删除的生产目录。最明确的清理方向是：先隔离 compatibility，再收敛 API 默认路径，最后归档 Legacy 服务。

## 2. ACTIVE 当前使用路径

### 2.1 Intent

| 路径 | 角色 | 被谁引用 | 生产路径 | 建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/task/intent_models.py` | `IntentSpec`、动作/资源/条件/约束模型 | `understanding.py`、Planner Evaluation、Runtime 相关代码 | 是 | **KEEP** |
| `packages/assistant_core/greenbook_assistant_core/task/understanding.py` | L1/L2 理解入口、Direct IntentSpec、Validator、Repair | `apps/assistant_api/.../main.py`、RuntimeAgentService、测试 | 是 | **KEEP**，后续拆分 legacy methods |
| `packages/assistant_core/greenbook_assistant_core/task/intent_validator.py` | IntentSpec 确定性校验 | `understanding.py`、单测、评测 | 是 | **KEEP** |
| `packages/assistant_core/greenbook_assistant_core/task/intent_preprocessor.py` | 生成结构提示，不生成意图 | `understanding.py` | 是 | **KEEP** |
| `packages/assistant_core/greenbook_assistant_core/task/intent_compat.py` | `IntentSpec -> TaskIntent` 兼容投影 | `understanding.py` 中 L2 返回路径 | 是，兼容边界 | **KEEP**，标记 COMPATIBILITY |
| `packages/assistant_core/greenbook_assistant_core/task/models.py` | `TaskIntent`、Task 生命周期模型 | L1、Resolver、RuntimeContext、旧 Planner 输入、Evaluation | 是，兼容和 L1 | **KEEP**，逐步降级为 legacy projection |

当前 Understanding 的正式调用应理解为 Direct IntentSpec。`TaskIntent` 仍是对外/旧 Runtime 兼容结果，不能删除。

### 2.2 Execution

| 路径 | 角色 | 被谁引用 | 生产路径 | 建议 |
|---|---|---|---|---|
| `execution/models.py` | `PlanExecution`、`StepExecution`、状态模型 | StateManager、Worker、Repository、API、Persistence、Evaluation | 是 | **KEEP** |
| `execution/state_manager.py` | 唯一状态迁移入口 | Worker、RuntimeManager、RecoveryService、API | 是 | **KEEP** |
| `execution/repository.py` | 内存 execution repository | StateManager、Worker、Runtime API、测试 | 是，默认/测试 | **KEEP** |
| `execution/postgres_repository.py` | PostgreSQL execution persistence | Persistence tests、部署适配 | 是，持久化 adapter | **KEEP** |
| `execution/worker.py` | TaskPlan step 执行 | RuntimeAgentService、测试 | 是 | **KEEP** |
| `execution/runtime_manager.py` | 用户控制 execution | Runtime API、事件流测试 | 是 | **KEEP** |
| `execution/runtime_guard.py` | 执行前状态闸门 | Worker、Runtime tests | 是 | **KEEP** |
| `execution/checkpoint.py` | 恢复快照 | RuntimeManager、RetryManager、RecoveryService | 是 | **KEEP** |
| `execution/recovery.py` | error-code retry policy | Worker、RetryManager、RecoveryService | 是 | **KEEP** |
| `execution/events.py` | execution/step/retry 事件模型 | StateManager、Worker、EventStore、API、Evaluation | 是 | **KEEP** |
| `execution/lease.py` | Worker lease | Persistence/recovery 相关代码 | 是，基础设施 | **KEEP** |

执行 Runtime 没有发现第二套替代 `PlanExecution` 的新 Run 模型。重复问题主要发生在旧 API 的 `Run` 记录，而不是 `execution/` 核心模型内部。

## 3. COMPATIBILITY 保留原因

### 3.1 IntentDraft

路径：

- `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py`
- `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py`

角色：早期 free-form Draft 和 `IntentCompiler`。

被谁引用：

- `TaskUnderstanding._llm_understand_draft()` 的 lazy import；
- `task.intent_draft` 旧路径 shim；
- `tests/compat/intent/test_intent_draft.py`。

生产路径：不是 Direct IntentSpec 正式主路径；属于兼容路径。

建议：**MIGRATE** 到 compatibility namespace，保留 shim；稳定后 **ARCHIVE**。当前不应立即 DELETE。

### 3.2 IntentElements

路径：

- `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py`
- `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py`

角色：早期 `{verb, object}` 元素抽取和 `IntentSpecBuilder`。

被谁引用：

- `TaskUnderstanding._llm_understand_elements()` 的 lazy import；
- `task.intent_elements` 旧路径 shim；
- `tests/compat/intent/test_intent_elements.py`。

生产路径：不是 Direct IntentSpec 正式主路径。

建议：**MIGRATE** 已完成；继续作为 compatibility 保留，后续 **ARCHIVE**。

### 3.3 TaskIntent

路径：

- `task/models.py`
- `task/intent_compat.py`

角色：L1 快速理解结果、旧 Runtime 输入和 IntentSpec 的兼容投影。

被谁引用：

- `TaskUnderstanding` L1；
- `RuntimeContext`；
- `TaskResolver`、`ReferenceResolver`、`ResourceResolver`；
- RuntimeAgentService 和部分 Planner 输入；
- legacy evaluation metrics。

生产路径：是，但定位应是 L1/compatibility，而不是 richer intent 的唯一模型。

建议：**KEEP**，长期目标是缩小新 Runtime 对它的依赖，不能当前删除。

### 3.4 intent_compat

路径：[task/intent_compat.py](D:/agent/green-book/packages/assistant_core/greenbook_assistant_core/task/intent_compat.py)

角色：将 `IntentSpec` 投影成旧 `TaskIntent`。

被谁引用：`understanding.py` 的 L2 结果兼容路径。

建议：**KEEP**，明确标记为 compatibility adapter。只有当 Planner、Resolver、API 和旧测试全部直接消费 IntentSpec 后，才可评估归档。

## 4. LEGACY Runtime 审计

| 路径 | 角色 | 被谁引用 | 生产路径 | 建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/agent.py` | 旧单 Agent tool-calling loop | `LegacyAgentService`、integration tests、revision tests | 是，fallback/default 配置可触发 | **MIGRATE**，之后 ARCHIVE |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | 旧 Agent API service wrapper | `main.py`、`AssistantService`、部分 routes | 是，fallback | **MIGRATE** |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py` | Legacy/Runtime 路由器 | `AssistantService`、router tests | 是，迁移控制面 | **KEEP** 短期；Runtime 收敛后 ARCHIVE Legacy 分支 |
| `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py` | 旧/新路径选择和 fallback | API wiring | 是 | **MIGRATE** 为 Runtime-only service，保留兼容 adapter |
| `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中 `RunRepository` | 旧回合记录 repository | `api/routes.py`、旧 run/approval API | 是 | **MIGRATE** 命名，暂不删除 |
| `api/routes.py` 中 `RunResponse` | 旧 API response | runs 查询、取消、中断、集成测试 | 是 | **MIGRATE** 为 ConversationRun 兼容 API |
| `assistant_runs` 表定义 | 旧 assistant turn persistence | `RunRepository` | 是 | **MIGRATE**，与 execution 表明确分层 |
| `_InMemoryRunRepo` | 无数据库时的旧 API fallback | `routes.py` | 测试/开发路径 | **MIGRATE** 或后续 ARCHIVE |

## 5. Creator 相关历史代码

### 5.1 `packages/creator_client`

角色：Assistant/MCP 调用 Creator 服务的 HTTP client。

被谁引用：

- `apps/assistant_api/main.py`；
- `services/greenbook_mcp/server.py`；
- integration tests。

生产路径：是。

建议：**KEEP**。

### 5.2 `services/creator_agent`

角色：workspace 注册的 Creator Agent 服务包。

被谁引用：workspace 配置和 package manifest；当前源码直接引用较少。

生产路径：配置上是，实际部署关系需要进一步确认。

建议：**KEEP**，先完成 deployment/contract audit。

### 5.3 根目录 `creator-agent`

角色：完整的独立 Creator Agent 工程，包含自身 Runtime、Worker、Memory、Retrieval、Evaluation 和 migrations。

被谁引用：主要是自身工程配置、文档和独立测试；当前 Assistant Core 通过 `CreatorClient` 访问外部服务，而不是直接 import 其内部模块。

生产路径：相邻服务路径，是否为当前部署实现尚未由本仓库 wiring 单独确认。

建议：**MIGRATE** 到独立 service 边界；如果确认由 `services/creator_agent` 替代，则 **ARCHIVE**。

## 6. 旧 Run 与 `assistant_runs` 审计

旧 Run 不是新的 Execution Runtime，但目前仍是 API 层使用的持久化对象：

```text
旧 API turn
  -> run_id
  -> RunRepository
  -> assistant_runs
```

正式 Runtime 是：

```text
TaskPlan
  -> execution_id
  -> PlanExecution
  -> execution / execution_step / event / checkpoint
```

两者当前是并行概念，不能直接删除旧 Run。推荐后续将旧 Run 重新命名为 `ConversationRun`，只保存对话回合、响应摘要、trace 和兼容 approval 信息。

## 7. DEAD CODE 候选

以下是候选，不代表现在可以删除：

| 路径/代码 | 死代码依据 | 当前阻碍 | 建议 |
|---|---|---|---|
| `task/understanding.py` 中 `_llm_understand_draft()` | 仅历史实现和测试语义，未被正式 L2 调用 | 仍是公开实例方法，可能有外部调用 | **DELETE_CANDIDATE**，先移入 compatibility adapter |
| `task/understanding.py` 中 `_llm_understand_elements()` | Direct 主路径不调用 | 仍保留测试和方法入口 | **DELETE_CANDIDATE**，先完成调用监控 |
| `task/understanding.py` 的 `_L2_DRAFT_SYSTEM` | 只服务 Draft 旧方法 | 方法仍存在 | **ARCHIVE** 随 Draft 实现迁移 |
| `task/understanding.py` 的 `_L2_ELEMENTS_SYSTEM` | 只服务 Elements 旧方法 | 方法仍存在 | **ARCHIVE** 随 Elements 实现迁移 |
| `IntentCompiler` | 正式路径不使用 | 旧 import shim 和兼容测试仍使用 | **DELETE_CANDIDATE** |
| `IntentSpecBuilder` | 正式路径不使用 | 旧 import shim 和兼容测试仍使用 | **DELETE_CANDIDATE** |
| `RuntimeRouter` 的 Legacy 分支 | Runtime-only 后可移除 | 当前默认配置和 fallback 仍使用 | **DELETE_CANDIDATE**，必须最后处理 |
| `CommunityOperationsAssistant` | 新 Runtime 已覆盖主要任务流 | 旧 API、integration 和 revision tests 仍直接引用 | **ARCHIVE**，不能当前删除 |
| `community-assistant-agent/` | 独立历史 Agent 实现 | 是否仍部署需外部确认 | **DELETE_CANDIDATE**，先归档 |
| 根目录 `creator-agent/` | 与 workspace Creator service 存在边界重叠 | 部署和 API source 未最终确认 | **DELETE_CANDIDATE**，先做 contract audit |

## 8. 文件级处理建议汇总

| 路径 | 角色 | 被谁引用 | 是否生产路径 | 建议 |
|---|---|---|---|---|
| `task/intent_models.py` | 正式 IntentSpec Schema | Understanding、Planner Evaluation、测试 | 是 | **KEEP** |
| `task/models.py` | TaskIntent 和 Task 模型 | L1、Resolver、RuntimeContext、旧 API | 是/兼容 | **KEEP** |
| `task/intent_compat.py` | IntentSpec 到 TaskIntent adapter | Understanding | 是/兼容 | **KEEP** |
| `compatibility/intent/intent_draft.py` | 历史 Draft 实现 | legacy method、shim、compat tests | 否 | **ARCHIVE** |
| `compatibility/intent/intent_elements.py` | 历史 Elements 实现 | legacy method、shim、compat tests | 否 | **ARCHIVE** |
| `task/intent_draft.py` | 旧 import shim | 外部旧 import、compat tests | 兼容 | **KEEP**，直到迁移窗口结束 |
| `task/intent_elements.py` | 旧 import shim | 外部旧 import、compat tests | 兼容 | **KEEP**，直到迁移窗口结束 |
| `task/understanding.py` | Understanding 正式入口和历史方法 | API、Runtime、测试 | 是 | **MIGRATE** 内部 legacy methods，主入口 KEEP |
| `agent.py` | Legacy Agent loop | LegacyAgentService、integration tests | fallback | **ARCHIVE** |
| `legacy_agent_service.py` | Legacy API adapter | main、AssistantService、routes | fallback | **MIGRATE** 后 ARCHIVE |
| `runtime_router.py` | 新旧路径路由 | AssistantService、tests | 是，迁移控制面 | **KEEP** 短期 |
| `db/repositories.py:RunRepository` | 旧 turn persistence | routes、旧 API | 是 | **MIGRATE** 命名和边界 |
| `api/routes.py:RunResponse` | 旧 run HTTP contract | 客户端、集成测试 | 是 | **KEEP** 兼容期 |
| `assistant_runs` | 旧 turn 数据表 | RunRepository | 是 | **MIGRATE**，不与 execution 表合并 |
| `packages/creator_client` | Creator HTTP client | API、MCP | 是 | **KEEP** |
| `services/creator_agent` | workspace Creator service | workspace/deployment | 待确认 | **KEEP**，先审计 |
| `creator-agent/` | 独立 Creator Agent 工程 | 自身配置、文档、测试 | 待确认 | **MIGRATE** 或 ARCHIVE |

## 9. 推荐处理顺序

1. 保留当前 `PlanExecution`、Execution Runtime、Planner 和 Direct IntentSpec 不动。
2. 将本报告与 `ACTIVE_ARCHITECTURE.md` 作为唯一当前架构入口。
3. 为 Legacy Agent 和旧 Run 增加明确的 compatibility 标识和 owner。
4. 对 Draft/Elements 旧方法增加调用监控，确认无外部运行时调用。
5. 将 legacy tests 与 active tests 分离。
6. 审计两个 Creator 实现的部署、API、migration 和 CI 引用。
7. 将历史实现迁移到 archive 或独立仓库。
8. 最后才评估删除候选，并执行全量回归和生产回滚验证。

## 10. 测试基线

本报告生成前未修改业务代码。报告生成后应运行：

```text
pytest tests/unit
pytest tests/evaluation
```

