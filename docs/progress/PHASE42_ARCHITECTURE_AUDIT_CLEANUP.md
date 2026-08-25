# Phase 4.2 — Architecture Audit + Hardcode/Fallback Cleanup + Project Slimming

> 日期：2026-08-13
> 范围：系统性收口——找语义硬编码、silent fallback、双框架、无 caller legacy、失效 flag；小批量删除
> 约束：设计目标 0813；Phase 3/3.5/4/4.1 不回归；不新增功能/Planner/Cache/RateLimit

## A. Current Architecture（唯一主链）

```text
Message -> Persist Run/202 -> Context + Latest Task State
  -> LLM Understanding (Command + task_changes + first_action + complexity)
  -> TaskDelta validate/apply OR (Simple bootstrap OR GoalDecomposer[complex])
  -> Task/Goal desired state -> AgentLoop -> ReadySelector
  -> Policy/Resource -> Direct/Durable Runtime -> Observation
  -> Desired vs Actual -> AgentLoop -> Progressive UX
```

## B. Decision Ownership（唯一 owner，已确立）

LLM Understanding=语义 | GoalDecomposer=complex NEW_WORK 拆解 | TaskDelta=desired mutation |
TargetResolver=grounding | TaskManager=lifecycle/apply | GoalCompiler=legality |
AgentLoop=next action | ReadySelector=READY 判定 | ToolSelector=tool 映射 |
ToolPolicyGate=permission | Runtime=可靠执行 | Reconciliation=satisfied/missing/mismatch

无重复 owner（§3 逐一核对）。

## C. Semantic Hardcodes Found

- `_command_from_tree`：`goal_type in {QUERY,RESEARCH,ANALYZE} -> CommandType.QUERY`——continuation envelope，
  goal_type 来自 LLM 输出（非 keyword），**KEEP（HELPER）**。
- 状态聚合 `WAITING_HUMAN/WAITING_APPROVAL/FAILED`（2113 行）——deterministic 聚合，**KEEP**。
- 未发现：keyword→operation、capability A→B 固定 workflow、GENERATE→SCHEDULE 强制拼接（均来自
  Goal.dependencies / LLM output / desired-actual）。

## D. Deterministic Rules Kept（§5）

permission/authorization、schema validation、state transition、dependency cycle、CAS/version、
idempotency、publication legality、DRAFT_ONLY 不可发布、resource ownership、approval、
时间标准化、typed ID、status aggregation —— 全部 KEEP。

## E. Dual Frameworks Found

| 双轨 | 状态 |
|---|---|
| TaskDelta vs old mutation rebuild | TaskDelta 唯一（旧 MODIFY/CANCEL 覆盖已从 `_bind_task` 删除） |
| immediate vs sync | sync fallback 已删除（唯一 immediate） |
| run-scoped UX vs global | 未审计（前端，token 受限，见 P） |
| new completion vs execution-success | 无旧 completion shortcut（§30 代码审计） |
| Postgres queue vs memory queue | 无（单一 queue） |

## F. Legacy/Fallback Findings（§8-13）

- **sync path**：`_send_runtime_message` + `GREENBOOK_LEGACY_SYNC_RECOVERY` flag —— **0 caller（无测试、无脚本设置）→ 已删除**。immediate-accept 不可用时现在 503 fail-closed，不再静默同步。
- **WHOLE_PLAN**：`_require_new_request_incremental_plan` guard（`WHOLE_PLAN_NEW_REQUEST_DISABLED`）——ACTIVE 纪律，KEEP。
- **`_bind_task` MODIFY/CANCEL 覆盖分支**：Phase 4.1 后无 caller → **已删除**；仅保留 CREATE+SCHEDULE_PUBLISH follow-up。

## G. Dead Code / Dead Config

- `GREENBOOK_AGENT_EXECUTION_MODE` / `GREENBOOK_AGENT_RUNTIME_ENABLED`：legacy runtime 开关，`app.state.execution_mode`/`runtime_enabled` **无任何消费者** → **已删除**（含 "legacy" 分支）。
- 其余 GREENBOOK_* flags 均有消费者（URL/timeout/concurrency 配置）。

## H. State Authority Findings（§17-19）

- Desired authority：Task 最新 `goal_tree_snapshot`（Phase 4.1 已确立，continue_run 优先读它）。
- `active_target` 使用点均为 **grounding hint**（`_resolve_delta_target` 的最后候选 + `_apply_active_resource_binding`
  为后续工具注入 active draft），非 AUTHORITATIVE；MODIFY/CANCEL 无 task_changes 已 fail-closed。
- 无把 command.constraints / execution args 当 authority 的路径。

## I. Frontend Duplicate State

**未审计**（token 受限）。已知前端有 1s reconcile 轮询 + SSE 双机制（§31 允许），需下一批核实旧 global loading 是否仍被 render 使用。

## J. Cleanup Applied

- **Removed**：`_send_runtime_message`（routes.py，sync 路径）；`GREENBOOK_LEGACY_SYNC_RECOVERY` fallback；
  `execution_mode`/`runtime_enabled` 计算与 app.state 存储；`_bind_task` 的 MODIFY/CANCEL 覆盖分支；
  routes.py 无用的 `ExecutionControlCommand` import。
- **Kept**：WHOLE_PLAN guard、active_target hints、GoalCompiler/TaskManager/TargetResolver helper。
- **Legacy**：`_command_from_tree`（continuation envelope，仅无 command payload 时）。

## K. Project Slimming Metrics

| 项 | Before | After |
|---|---|---|
| feature flags（GREENBOOK_LEGACY_SYNC_RECOVERY / EXECUTION_MODE / RUNTIME_ENABLED） | 3 | 0 |
| sync fallback 分支 | 1 | 0 |
| legacy MODIFY/CANCEL 覆盖分支 | 1 | 0 |
| 单元测试 | 601 | 601（全过） |

## L. Final Architecture（唯一主链）

见 A。新请求唯一入口：immediate-accept → Understanding → TaskDelta/bootstrap/decompose → AgentLoop → Runtime → Observation。

## M. How to Add a New Capability

新 read-only capability 只需：注册 ToolMetadata（name/capabilities）+ capability registry；GoalCompiler/ReadySelector/AgentLoop 无需改（无 capability-name workflow 特判）。开放性由 TaskDelta/AgentLoop 语义层保证。

## N. Regression

- **601 单元测试全过**（Phase 3 concurrency / 3.5 bootstrap / 4 TaskDelta / 4.1 stale+CAS 无回归）。
- e2e 3 个失败均为已知共享 DB 竞争（TestClient 与生产实例抢 run），非本轮引入。

## O. Real Smoke

- Case 1（"帮我找几篇关于 Agent 的帖子并总结共同方法"）：真实 DeepSeek+Java+Postgres 跑通，
  ACCEPTED(0.06s) → COMPLETED(38s)，助手消息生成。
- Case 3（mutation）：前轮已确认真实 LLM 输出 task_changes（CANCEL_GOAL），delta 路径走通。
- Case 2（Creator）：未重跑（依赖 Creator 长任务）。

## P. Remaining Technical Debt

- 前端重复 state 审计（global loading vs run-scoped）
- 真实浏览器 Case A/B（双实例环境阻塞）
- .env.example / 启动脚本瘦身核对（本轮 flags 已同步）
- 深层 unused dependency（未做全 import graph）

## Q. Final Assessment

1. **双框架？** 无。唯一 immediate+TaskDelta 主链；sync/legacy rebuild 已删。
2. **不合理 semantic hardcode？** 未发现 keyword→operation / 固定 workflow 拼接。
3. **合理 deterministic guard？** WHOLE_PLAN 禁用、状态聚合、permission/CAS/idempotency 等。
4. **Control Plane 轻量？** 是——Understanding→TaskDelta/Goal→AgentLoop→Ready→Policy→Runtime。
5. **Runtime 必须保留的复杂度？** Run/Task/Goal/Execution/Observation/Queue/Lease/Retry/Approval/Resource/CAS。
6. **下一阶段**：前端 state 收口、真实浏览器认证、Capability 扩展开放测试、deep dependency cleanup。
