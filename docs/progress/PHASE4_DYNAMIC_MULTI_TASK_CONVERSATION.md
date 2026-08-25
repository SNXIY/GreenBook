# Phase 4 — Dynamic Multi-Task Conversation + TaskDelta + Cleanup

> 日期：2026-08-13
> 范围：会话中动态追加/修改/取消任务；TaskDelta 结构化变更；不做整棵 GoalTree 重建
> 约束：设计目标 0813；Phase 3/3.5 不回归；in-flight Execution 不可变；latest desired state 收敛

## 0. 审计结论（§83.1）

- **IntentDelta 不存在**：grep `IntentDelta` 无结果（已被 Phase 9 迁移清理），不存在"IntentDelta vs TaskDelta 两套并存"问题。
- **TaskManager 已有完整 lifecycle**：`create_task / bind_goal_tree / append_goal / modify_task / cancel_task / pause / resume / wait_for_human`，带 version + revisions。**直接复用**，不重写。
- **Task 持久化整棵 `goal_tree_snapshot`**（GoalTree JSON），`bind_goal_tree` 覆盖并 bump goal_tree_version/plan_version。
- **当前 MODIFY/CANCEL 缺口**（§9 违反）：`_bind_task` 对 MODIFY/CANCEL 用**新的完整 GoalTree 覆盖**已有 task——即每句修改都重建 GoalTree（经过 GoalDecomposer LLM）。无增量概念、无多变化一条消息。
- **continue_run 从 observation 快照恢复 GoalTree**（不读 task 最新快照）→ 动态 ADD_GOAL 后需触发新 AgentLoop run 用最新 GoalTree。

## 1. 复现：运行中修改请求当前行为

真实请求（"Redis 那个不用了，顺便查 RAG"）：
- **修复前**：真实 DeepSeek 已输出 `task_changes`（CANCEL_GOAL target Redis），但 MODIFY 命令在 delta 分支前被 `requires_target → AMBIGUOUS_TARGET` 拦截，返回澄清（错误路径）。
- **修复后**：delta 分支移到 target 校验之前，mutation 走 delta apply → AgentLoop 推进（遇证据不足时 fail-closed WAITING_USER，正确）。

## 2. 实现：最小 structured TaskDelta

### 模型（command/models.py）
- `TaskDeltaOperation`：CREATE_TASK / ADD_GOAL / UPDATE_GOAL / CANCEL_GOAL / CANCEL_TASK / CONTINUE_TASK / NO_CHANGE / ASK_USER（纯状态变更，不含工具能力）
- `TaskDelta`：operation + change_id + target_reference + desired_changes + dependency_reference + source_reference + needs_target_resolution
- `StructuredCommandOutput` / `Command` 加 `task_changes: list[TaskDelta]`（复用第一轮 Understanding，**不新增 LLM call**）

### apply 层（conversation_runtime_adapter.py）
- `_run_task_deltas`：对每个 delta deterministic validate + apply（TaskManager / GoalTree patch），任一无法安全 grounding → fail-closed ASK_USER；消息内按 change_id 幂等去重。
- `_delta_create_task` / `_resolve_delta_target`（label/id/kind/active 定位，多候选 → ASK_USER）
- `_append_delta_goal` / `_patch_delta_goal` / `_cancel_delta_goal`：GoalTree 精确 patch（不重建）
- mutation 后对受影响 task 用**最新 goal_tree_snapshot** 调 `_run_agent_loop`（existing_task 复用，不覆盖重建）

## 3. Cleanup 报告（§84 格式）

### Removed
无新增删除。审计确认：
- IntentDelta 不存在（无重复框架）
- `_bind_task` 的 MODIFY/CANCEL 全量覆盖路径**保留**为 LEGACY_RECOVERY_ONLY（无 task_changes 的旧消息仍可用），但新消息经 Understanding 输出 task_changes 走 delta 路径。

### Reused / Evolved
- TaskDelta 复用了第一轮 Understanding（StructuredCommandOutput.task_changes），没有第二套 Delta 模型、没有第二个 Understanding LLM。
- TaskManager lifecycle 方法原样复用（create/append/cancel/bind）。

### Helper Only
- TaskManager：lifecycle / deterministic apply（§27）
- TargetResolver：grounding（reference → task/goal），不决定 operation
- GoalCompiler：schema/dependency validation
- ReadySelector：mutation 后重新评估 READY work

### Legacy Recovery Only
- `_bind_task` 的旧 MODIFY/CANCEL 覆盖路径（无 task_changes 兼容）

### Active New Request Path
```text
Message -> Persist Run(202) -> Context -> LLM Understanding
  ├─ task_changes 非空 -> _run_task_deltas (validate -> apply -> AgentLoop 最新 GoalTree)
  └─ 新工作 (Phase 3.5) -> bootstrap / GoalDecomposer -> AgentLoop
```

## 4. LLM Call Count（§85）

一条 mutation 消息（"Redis 那个不用了，Java 那篇改明天下午发，顺便查 RAG"）：

| 阶段 | LLM calls |
|---|---|
| Understanding（输出 Command + task_changes） | **1** |
| TaskDelta parse / validate / apply | 0（deterministic） |
| mutation 后 AgentLoop 推进（每受影响 task 一次 reason） | 每 task 1（正常 continuation） |

理想：**1 个 Understanding LLM** + apply + 正常 AgentLoop continuation。没有 Interpret→Mutation LLM→Goal LLM→first reason 四连。

## 5. 测试

新增 `tests/unit/test_phase4_task_delta.py`（10 个）：
- delta schema / operation 不含工具能力
- ADD_GOAL 追加 + dependency、UPDATE_GOAL patch desired（temporal）、CANCEL_GOAL 移除、root 不可取消
- TaskManager cancel / bind 持久化 snapshot + version bump
- Command 端到端 roundtrip task_changes
- 消息内 change_id 幂等去重

**回归**：593 单元测试全过（Phase 3 concurrency + Phase 3.5 bootstrap 不回归）。

## 6. 真实联调证据（§80）

- 真实 Java 登录 → 202 ACCEPTED(0.06s)
- 真实 DeepSeek 对 mutation 消息输出 `task_changes`（CANCEL_GOAL target Redis…），确认 LLM 语义走通
- mutation 修复前 AMBIGUOUS_TARGET（bug，已修复）；修复后走 delta 路径，AgentLoop 推进（证据不足时 fail-closed）
- 环境限制：8094 双实例（SSE 跨实例）、mutation 目标 task grounding 依赖首个请求产生的 task 结构，完整 Case A/B 业务收敛需人工浏览器 + 稳定多 task 场景验证

## 7. 设计目标 0813 符合性

- 不新增 Planner/Workflow DSL/Scheduler（TaskDelta 只是状态变更结构）
- in-flight Execution 不可变：UPDATE_GOAL 只改 Goal desired（temporal/description），不碰 execution
- latest desired state：_patch_delta_goal 更新 Goal.temporal_constraint；AgentLoop 基于 desired/actual 差距决策
- stale observation：mutation 后新 AgentLoop run 用最新 GoalTree（不做 old observation 驱动的错误 completion）
- sibling 隔离：delta 按 task 独立 apply，失败不影响其他

## 8. Deferred

- stale observation 的完整收敛测试（§63）
- 跨消息幂等（同 message 重放，依赖 run 级 idempotency + revisions 审计）
- version/CAS 显式谓词（当前复用 TaskManager _persist 顺序递增）
- 浏览器人工验证 Case A/B（需双实例清理后）
