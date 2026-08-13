> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime Cleanup Candidates

> Phase 7.3-B/C。本文记录清理候选及删除条件。
> 本次不删除任何文件。DELETE 只表示满足条件后的候选状态。

## 1. ACTIVE 保留

| 路径 | 作用 | 引用方 | 是否 ACTIVE | 处理建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/task/intent_models.py` | IntentSpec schema | Understanding、Planner Evaluation、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/task/understanding.py` | L1/L2 Understanding 和 Direct IntentSpec | API、RuntimeAgentService、测试 | 是 | KEEP；历史方法仅走 adapter |
| `packages/assistant_core/greenbook_assistant_core/task/intent_validator.py` | IntentSpec 确定性校验 | Understanding、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/orchestration/` | PlanningContext、TaskPlan、Planner | RuntimeAgentService、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/planning/` | ExecutablePlan 和计划校验 | RuntimeAgentService、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/execution/` | PlanExecution、Worker、状态、恢复、事件、持久化 | API、RuntimeAgentService、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/capability/` | Capability Registry 和映射 | Planner、CapabilityExecutor | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/execution/runtime/` | ToolRuntime、调用上下文和 ledger | CapabilityExecutor、MCP | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/artifact/` | 跨步骤 Artifact | Worker、测试 | 是 | KEEP |
| `packages/assistant_core/greenbook_assistant_core/observability/` | Trace 和执行观测 | Worker、ToolRuntime、Evaluation | 是 | KEEP |
| `packages/evaluation/greenbook_evaluation/` | Intent、Planner、Execution Evaluation | 测试和评测运行器 | 是 | KEEP |

## 2. Intent Compatibility

| 路径 | 作用 | 引用方 | 是否 ACTIVE | 处理建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/adapter.py` | 历史 Draft/Elements 的唯一 adapter 边界 | `task/understanding.py` 的 legacy methods | 否，兼容边界 | KEEP，禁止扩展 |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py` | IntentDraft 和 IntentCompiler 实现 | adapter、shim、compat tests | 否 | ARCHIVE |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py` | IntentElements 和 IntentSpecBuilder 实现 | adapter、shim、compat tests | 否 | ARCHIVE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py` | 旧 import re-export shim | compat tests、外部历史 import | 否，兼容入口 | KEEP，迁移窗口结束后 DELETE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py` | 旧 import re-export shim | compat tests、外部历史 import | 否，兼容入口 | KEEP，迁移窗口结束后 DELETE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_compat.py` | IntentSpec 到 TaskIntent 投影 | `understanding.py`、loss evaluation | 否，兼容 adapter | KEEP，长期迁移边界 |
| `packages/assistant_core/greenbook_assistant_core/task/models.py` 中 `TaskIntent` | L1 结果和旧消费者模型 | L1、Resolver、RuntimeContext、Planner 兼容输入、测试 | 部分 ACTIVE | KEEP；逐步减少新 Runtime 读取 |
| `tests/compat/intent/test_intent_draft.py` | Draft 回归和旧行为测试 | pytest compat suite | 否 | KEEP，直到 shim 下线 |
| `tests/compat/intent/test_intent_elements.py` | Elements 回归和旧行为测试 | pytest compat suite | 否 | KEEP，直到 shim 下线 |

### 删除条件

以下文件当前都不满足删除条件，因为存在测试或兼容引用：

- `task/intent_draft.py`：仍验证旧 import shim；
- `task/intent_elements.py`：仍验证旧 import shim；
- `compatibility/intent/intent_draft.py`：adapter 仍使用；
- `compatibility/intent/intent_elements.py`：adapter 仍使用；
- `intent_compat.py`：L2 到旧 TaskIntent 的兼容投影仍被使用。

## 3. Legacy Runtime

| 路径 | 作用 | 引用方 | 是否 ACTIVE | 处理建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/agent.py` | 旧单 Agent tool-calling loop | `LegacyAgentService`、integration/revision tests | 否，fallback | MIGRATE 后 ARCHIVE |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | Legacy Agent API wrapper | `main.py`、`AssistantService`、routes | 否，fallback/default 可能触发 | MIGRATE |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py` | Legacy/Runtime 路由 | `AssistantService`、router tests | 迁移控制面 | KEEP 短期，Legacy 分支后 DELETE |
| `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py` | 新旧路径选择和 fallback | API wiring | 是，但含 Legacy 边界 | MIGRATE 为 Runtime-first service |
| `tests/unit/test_runtime_router.py` | Legacy/Runtime 路由行为测试 | pytest unit | 否 | KEEP 至 Legacy 分支移除 |
| `tests/integration/test_assistant_runtime_contracts.py` | 旧 Agent/MCP/API contract | pytest integration | 部分 | MIGRATE/拆分为 compat |
| `tests/unit/test_revision_orchestration.py` | 旧 Agent revision orchestration | pytest unit | 否 | MIGRATE 到 compat 后评估 ARCHIVE |
| `community-assistant-agent/` | 独立历史 Agent 工程 | 自身测试、配置、文档 | 待确认 | ARCHIVE；部署确认后 DELETE candidate |

### 删除条件

Legacy Runtime 文件只有在以下条件全部满足后才可标记 DELETE：

1. API 默认不再创建或执行 Legacy Agent；
2. `AssistantService` 不再需要 Legacy fallback；
3. 没有生产 import `agent.py` 或 `LegacyAgentService`；
4. 旧 integration/revision tests 已迁移到兼容测试或删除；
5. Runtime 全量回归和生产流量验证完成；
6. Legacy API 回滚方案已失效并正式下线。

当前条件未满足，因此本阶段不删除 Legacy Runtime。

## 4. Legacy Run

| 路径 | 作用 | 引用方 | 是否 ACTIVE | 处理建议 |
|---|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中 `RunRepository` | 旧 assistant turn persistence | `api/routes.py` | 否，API 兼容 | MIGRATE 为 ConversationRun adapter |
| `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中 `assistant_runs` 表定义 | 旧回合数据表 | `RunRepository`、旧 routes | 否，旧 API | MIGRATE，保留数据迁移路径 |
| `apps/assistant_api/greenbook_assistant_api/api/routes.py` 中 `RunResponse` | 旧 run HTTP response | `/runs/{run_id}` API、集成测试 | 否，旧 API | KEEP 兼容期，之后 ARCHIVE |
| `apps/assistant_api/greenbook_assistant_api/api/routes.py` 中 `_InMemoryRunRepo` | 无 DB 时的旧 API fallback | `routes.py` | 否，测试/开发 | MIGRATE，之后 DELETE candidate |
| `apps/assistant_api/greenbook_assistant_api/models/runtime_context.py` 中 `run_id` | 旧 turn context id | Legacy、GroupExecutor、旧 API | 否，兼容字段 | KEEP 兼容期，禁止新语义 |
| `packages/assistant_core/greenbook_assistant_core/context.py` 中 `run_id` | Legacy Agent/Tool audit id | `agent.py`、MCP/clients | 否，旧 audit | MIGRATE 到 execution_id/turn_id |
| `packages/security/greenbook_security/approval.py` 中 `run_id` | 旧 approval 关联键 | Legacy API/approval tests | 否，兼容 | MIGRATE 后 ARCHIVE |

`PlanExecution`、`ExecutionRepository` 和 execution persistence 不属于清理候选，不能删除或替换。

### 删除条件

旧 Run 文件只有在以下条件全部满足后才可删除：

- API 已改为 `execution_id` 或明确的 `conversation_id`；
- 历史数据已完成迁移/保留策略；
- 旧 `/runs` contract 已下线；
- approval、tool audit 和 tests 不再依赖 `run_id`；
- 没有生产代码读取 `assistant_runs`。

当前不满足。

## 5. Creator 相关代码

| 路径 | 作用 | 引用方 | 是否 ACTIVE | 处理建议 |
|---|---|---|---|---|
| `packages/creator_client/` | Creator HTTP client | API、MCP、integration tests | 是 | KEEP |
| `services/greenbook_mcp/` 中 Creator workflows | MCP 到 Creator 的业务适配 | Capability/ToolRuntime | 是 | KEEP |
| `services/creator_agent/` | workspace 注册的 Creator service | workspace manifest、部署配置 | 待确认 | KEEP，先完成 contract audit |
| `creator-agent/` | 独立完整 Creator Agent 工程 | 自身配置、测试、文档 | 待确认 | MIGRATE/ARCHIVE，确认部署后再决定 DELETE |
| `tests/integration/test_assistant_runtime_contracts.py` Creator tests | CreatorClient contract | pytest integration | 是 | KEEP |

不得因为名称相似而删除 `packages/creator_client` 或 MCP workflows。它们属于当前业务工具边界。

## 6. 删除执行审查

本阶段候选审查结果：

```text
满足全部删除条件的文件：0
```

因此本阶段：

- 删除文件：无；
- 保留文件：所有当前代码文件；
- 新增 adapter：`compatibility/intent/adapter.py`；
- 变更业务逻辑：无。

## 7. Import 检查目标

允许的正式边界：

```text
understanding.py
    -> compatibility.intent.adapter
        -> historical IntentDraft / IntentElements
```

禁止的正式边界：

```text
understanding.py / Planner / Worker / Runtime
    -X-> compatibility.intent.intent_draft
    -X-> compatibility.intent.intent_elements
    -X-> task.intent_draft implementation
    -X-> task.intent_elements implementation
```

当前兼容测试可以继续直接使用旧 shim，以验证旧 import 不破坏。

