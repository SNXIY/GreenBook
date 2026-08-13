> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime Final Cleanup Report

本报告是 Phase 7.5 的只读清理分析。依据：

- `docs/architecture/ACTIVE_ARCHITECTURE.md`
- `docs/architecture/LEGACY_AUDIT.md`
- `docs/architecture/DEPRECATION_STATUS.md`
- `docs/architecture/CREATOR_BOUNDARY.md`

本阶段不删除、不移动文件，也不改变业务逻辑。`DELETE CANDIDATE` 表示需要在后续变更中按条件验证后处理，不表示本次已删除。

## 1. KEEP

### 核心 Runtime

| 文件/目录 | 原因 |
| --- | --- |
| `packages/assistant_core/greenbook_assistant_core/task/` | IntentSpec、理解、校验、兼容映射和资源/引用解析；是 Understanding 边界 |
| `packages/assistant_core/greenbook_assistant_core/planning/` | PlanningContext、Planner、TaskPlan 和计划校验；是正式 Planner 边界 |
| `packages/assistant_core/greenbook_assistant_core/orchestration/` | 将理解结果接入规划和任务编排 |
| `packages/assistant_core/greenbook_assistant_core/execution/` | PlanExecution、StepExecution、ExecutionStateManager、Worker、Guard、Retry、Checkpoint、Event、Persistence 和 Lease；PlanExecution 是唯一 execution source of truth |
| `packages/assistant_core/greenbook_assistant_core/capability/` | Capability registry 和 capability executor，连接 Planner 与 ToolRuntime |
| `packages/assistant_core/greenbook_assistant_core/execution/runtime/` | ToolRuntime、调用上下文和外部工具执行隔离 |
| `packages/assistant_core/greenbook_assistant_core/artifact/`、`human/`、`agent_memory/`、`resource/`、`observability/` | 当前 Runtime 的 artifact、人工审批、记忆、资源绑定和可观测性基础设施 |
| `apps/assistant_api/` | Assistant API、Runtime API、Legacy compatibility wiring 和服务入口；其中 Legacy 仍有生产/API 引用，不能整体删除 |
| `apps/assistant_worker/` | Worker 应用入口和后台任务边界 |
| `packages/contracts/`、`packages/security/` | 跨服务契约和安全策略，属于生产集成基础设施 |
| `packages/java_client/` | Java backend/community 数据和发布能力的正式客户端边界 |
| `packages/creator_client/` | Creator HTTP client；是 Assistant 与 Creator Agent 的正式协议边界 |
| `services/greenbook_mcp/` | 当前 capability 到外部工具、Creator 和 Java 的 MCP 适配层 |
| `packages/evaluation/` | Intent、Planner、Execution、metrics 和 badcase 评估基础设施；不是临时测试代码 |

### Compatibility 与数据安全

| 文件/目录 | 原因 |
| --- | --- |
| `packages/assistant_core/greenbook_assistant_core/compatibility/` | 明确的历史 Intent adapter 边界，保留旧 import 和迁移能力 |
| `task/intent_draft.py`、`task/intent_elements.py` shim | 旧 import 兼容；在外部调用和 compat 测试迁移完成前不能删除 |
| `task/intent_compat.py`、`TaskIntent` | L1、旧 Resolver、API 和部分旧 Planner wiring 仍依赖 legacy projection |
| `db/RunRepository`、`assistant_runs` 相关 API | 当前 API、approval、conversation persistence 和 contract 测试仍引用，不能因为 PlanExecution 已存在就直接删除 |
| `creator-agent/` | 独立 Creator 工程，当前 CreatorClient 通过 HTTP 间接调用；部署 owner 未确认前保留 |
| `services/creator_agent/` | workspace 注册的 Creator 服务目录；与根目录 Creator 工程的部署关系未完成核验 |

### 测试

| 目录 | 原因 |
| --- | --- |
| `tests/unit/` | 覆盖当前 Task、Planning、Execution、Runtime、Capability 和 API 行为 |
| `tests/evaluation/` | 覆盖 Intent dataset、指标、badcase 和 evaluation runner |
| `tests/compat/` | 专门验证 IntentDraft/IntentElements 等兼容边界，应保留直到迁移窗口关闭 |
| `tests/contract/` | Java、API、用户隔离和外部协议回归 |
| `tests/integration/` | Assistant、Creator、ToolRuntime 和跨模块协议回归 |
| `tests/e2e/` | 真实业务流程、审批、Creator unavailable 和长期内容流程回归 |

## 2. ARCHIVE

归档表示从当前 ACTIVE 入口移出，但仍保留历史记录或迁移参考。归档前应同步更新 CI、workspace、Docker 和文档入口。

| 文件/目录 | 原因 | 迁移目标 |
| --- | --- | --- |
| `docs/reports/greenbook-agent-phase*.md` | 已完成的阶段设计、诊断和实施记录，不应继续作为当前架构入口 | `docs/architecture/` 中的 ACTIVE 文档；历史报告保留在 `docs/archive/phase-reports/` |
| `docs/reports/greenbook-agent-runtime-*-plan.md`、`*-review.md`、`*-gap-analysis.md` | 迭代期方案和评审文档，可能与最终实现不一致 | 保留审计价值后归档，当前规范以 architecture 文档为准 |
| `docs/drafts/` | 草稿性质说明，不应与正式技术文档混用 | `docs/archive/drafts/` 或外部知识库 |
| `community-assistant-agent/` 中仅服务旧 turn pipeline 的 Creator/tool 实现 | Legacy Agent，不属于 GreenBook ACTIVE Runtime | capability/MCP contract、`CreatorClient` 和 `PlanExecution` |
| `community-assistant-agent/` 的旧编排/worker 入口 | 与 `PlanningContext -> Planner -> TaskPlan -> PlanExecution -> Worker` 重叠 | 旧 API 完成迁移后归档整个 legacy surface |
| `scripts/run_p0_e2e.py` 及其专用测试 | P0 多服务 harness，属于历史验收工具，不是 Runtime 生产入口 | 独立 `tools/legacy-e2e/` 或 CI 专用目录，确认仍需后再移动 |

`docs/ACCEPTANCE.md`、`docs/INTEGRATION.md`、`docs/COMMUNITY_ASSISTANT.md` 目前仍包含部署、协议和验收信息，暂不归档；应在后续整理中标注其权威范围，而不是直接删除。

## 3. DELETE CANDIDATE

以下对象满足“与源码或仓库运行无关”的初步特征，但删除前仍需要一次独立确认。本阶段不执行删除。

| 文件/目录 | 原因 | 引用情况 | 风险 |
| --- | --- | --- | --- |
| 各目录下的 `__pycache__/` 和 `*.pyc` | Python 运行生成物，不是源码或测试资产 | 无源代码 import；由解释器生成 | 低；应由 `.gitignore` 和清理脚本统一处理 |
| 根目录 `(base)_PS_D__agent_green-book__._scripts_start-ass_d87fb13c.json` | 疑似命令/会话临时产物，不属于项目架构 | 未发现生产 import | 中；需确认是否为本地诊断证据 |
| `scripts/debug_intent_parse.py`、`scripts/debug_llm_output.py` | 临时 Intent/LLM 调试脚本，不是正式 evaluation runner | 未发现 ACTIVE 代码引用 | 中；可能仍用于人工诊断，删除前应确认无人使用 |
| `scripts/Import-GreenBookEnv.ps1`、仅本地环境辅助脚本 | 可能是个人开发环境工具，不属于 Runtime 核心 | 需检查 README/CI/开发文档引用 | 中；删除可能影响本地启动流程 |
| 无法由 `pyproject.toml`、CI、Docker 或文档到达的旧示例目录 | 与当前工程无关的示例代码 | 本次扫描未将目录级结论视为已证实 | 高；必须先做构建和部署引用检查 |

不建议当前删除：`LegacyAgentService`、`RunRepository`、`TaskIntent`、`intent_draft`、`intent_elements`、`creator-agent` 或 `services/creator_agent`。这些对象都有现存 API、兼容、部署或测试风险，属于迁移/归档候选而不是满足条件的删除项。

## 4. Duplicate Implementation

### 重复 Agent

- `packages/assistant_core/greenbook_assistant_core/agent.py` 与 `apps/assistant_api/.../services/legacy_agent_service.py` 构成旧 Agent 包装链。
- `apps/assistant_api/.../services/runtime_agent_service.py` 是当前 Runtime 服务入口。
- 两条路径目前由 `assistant_service.py`、`runtime_router.py` 和 API 配置共同保留，不能直接合并或删除；目标是让 ACTIVE Runtime 成为默认，并把 Legacy 变成显式兼容入口。

### 重复 Runtime

- 旧 `RunRepository` / `assistant_runs` / `run_id` 生命周期。
- 新 `PlanExecution` / `ExecutionStateManager` / `execution_id` 生命周期。
- 两套模型语义不完全相同，当前存在 API 和 approval 兼容依赖。最终目标是让执行状态统一到 PlanExecution，旧 run 仅作为版本化 API adapter 或历史数据映射。

### 重复 Creator

- 根目录 `creator-agent/`。
- `services/creator_agent/`。
- `community-assistant-agent` 内的旧 Creator/content creation 工具。

正式边界应只有 `Capability -> MCP -> CreatorClient -> Creator Agent HTTP API`。在部署 owner、Docker、health check、CI 和 API contract 对照完成前，不可删除前两套 Creator 服务目录。

### 重复 Tool

- 当前 `assistant_core` 的 Capability/ToolRuntime 与 `services/greenbook_mcp` 的 MCP tools 是分层关系，不是简单重复：前者定义语义能力和执行边界，后者负责外部工具适配。
- `community-assistant-agent/app/tools.py` 是旧工具实现，属于迁移/归档候选。
- 若发现相同业务操作同时存在于旧 Agent 和 MCP，应以 MCP contract 为迁移目标，并保留 contract/E2E 回归。

### 重复 Evaluation

- `packages/evaluation/greenbook_evaluation/` 是可复用 evaluation library。
- `tests/evaluation/` 是该 library 的测试和 dataset 驱动回归，不是第二个 evaluator。
- `packages/assistant_core` 内部的 observability/trace 与 evaluation 输入存在数据交集，但职责不同：Trace 记录事实，Evaluation 读取事实并评分。

## 5. Documentation Cleanup

### KEEP

- `docs/architecture/ACTIVE_ARCHITECTURE.md`
- `docs/architecture/LEGACY_AUDIT.md`
- `docs/architecture/DEPRECATION_STATUS.md`
- `docs/architecture/CREATOR_BOUNDARY.md`
- 本报告 `docs/architecture/CLEANUP_FINAL_REPORT.md`
- `docs/INTEGRATION.md`、`docs/ACCEPTANCE.md`、`docs/COMMUNITY_ASSISTANT.md`，直到其中的协议/部署内容被正式文档替代
- `docs/greenbook-agent-runtime-technical-introduction.md`，作为对外/团队技术介绍入口；应与 ACTIVE 文档交叉链接

### ARCHIVE

- `docs/reports/` 中已完成的 Phase 设计、实验、诊断和实施计划
- `docs/drafts/` 中的未定稿材料
- 重复描述同一架构的旧 runtime review、migration roadmap 和 refactor plan

### DELETE CANDIDATE

当前没有足够证据把任一正式 Markdown 文档直接判定为 DELETE。应先将历史报告移动到 archive，并检查链接、CI 文档检查和外部引用；无引用、无决策记录价值的临时说明才可删除。

## 6. Test Cleanup

### 当前应保留

- `tests/unit/`：对应 ACTIVE code，包含 IntentSpec、PlanningContext、PlanExecution、Worker、Runtime Guard、Retry、Persistence、API 和 Evaluation。
- `tests/evaluation/`：对应正式 evaluation datasets、metrics、badcase 和 runner。
- `tests/compat/intent/`：对应明确的 compatibility adapter 和旧 import shim。
- `tests/contract/`、`tests/integration/`、`tests/e2e/`：覆盖 Java/Creator/MCP、Legacy API、审批、用户隔离和真实业务流程，不能按目录名视为历史测试。

### 需要后续分类审查

- 测试 docstring 中带 `Phase 6.x` 的用例不代表已废弃；应按实际 import 和断言对象判断。
- `tests/compat/intent/` 只有在所有外部兼容调用完成迁移后才可归档。
- P0 harness 相关测试应确认是否仍是发布门禁；若不是，可移入 legacy CI 测试目录。
- 使用 `run_id` 的测试不能直接删除，因为它们可能验证旧 API、审计 header 或 fixture contract；需要先完成 `execution_id` API 迁移。

### 初步结论

当前没有证据证明某个 ACTIVE 测试是“无引用测试”。优先清理生成物和临时调试脚本，再归档历史文档和已关闭的 P0 harness；业务测试应在迁移完成后通过覆盖率、import 和 CI 引用联合确认。

## 建议执行顺序

1. 先清理确认无版本价值的 `__pycache__`、`*.pyc` 和本地诊断产物。
2. 将已完成 Phase 报告和草稿迁移到明确的 archive 目录，并修复文档链接。
3. 完成 Legacy Agent、RunRepository/run_id 和 Creator 双目录的部署/API/数据引用审计。
4. 将旧 Agent 和旧工具迁移到 compatibility adapter，保留 contract、integration 和 E2E 回归。
5. 只有满足“无生产引用、无测试引用、已有替代、完成回滚和 owner 确认”的对象，才进入删除变更。

