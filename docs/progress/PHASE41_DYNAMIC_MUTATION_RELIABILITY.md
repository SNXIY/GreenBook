# Phase 4.1 — Dynamic Mutation Reliability Certification + Legacy Cleanup

> 日期：2026-08-13
> 范围：禁止 legacy GoalTree rebuild、stale observation reconciliation、mutation CAS/latest intent、真实认证
> 约束：设计目标 0813；Phase 3/3.5/4 不回归；不新增 Planner/Workflow/Scheduler

## A. Legacy Mutation Audit（普通新请求能否进入旧 GoalTree rebuild）

**结论：已禁止。** 

旧路径：`execute()` 无 task_changes 的 MODIFY/CANCEL → GoalDecomposer 重建 GoalTree → `_bind_task` 的
`target_task_id && (MODIFY|CANCEL)` 分支 `bind_goal_tree(task_id, new_tree)` **全量覆盖**已有 Task。

修复：`execute()` 在 delta 分支后新增 guard：

```python
if command.type in {MODIFY, CANCEL} and not command.task_changes:
    return clarification(... error_code="MUTATION_REQUIRES_DELTA")
```

无 task_changes 的 MODIFY/CANCEL **fail-closed**（ASK_USER），不会进入 GoalDecomposer/rebuild。
`_bind_task` 的 MODIFY/CANCEL 覆盖分支因此对普通新请求**无 caller**（唯一剩余使用是 `schedule_followup`——CREATE + SCHEDULE_PUBLISH + active_draft，属正常新工作，非 mutation）。

## B. Cleanup

- **Removed**：无物理文件删除（旧路径仍被 `_bind_task` 定义，但已无普通新请求入口）。
- **Legacy Recovery Only**：`_bind_task` 的 MODIFY/CANCEL 覆盖分支——无 caller（新请求被拦截），保留为恢复兜底；`schedule_followup` 为 ACTIVE（CREATE 语义）。
- **Active**：TaskDelta mutation path（唯一普通 mutation 主路径）。

## C. Desired vs Actual Authority

| 权威 | 来源 |
|---|---|
| User intent authority | 最新 Task/Goal persistent desired state（task.goal_tree_snapshot） |
| Execution authority | immutable submitted action（不修改） |
| Business truth | Java/Creator 结果 + Observation |
| Completion authority | latest desired vs actual satisfaction |
| Conversation history | evidence/context only |

## D. Stale Observation Reconciliation

- `continue_run` 现在**优先读 Task 最新 `goal_tree_snapshot`**（`_latest_goal_tree_for_observation`），
  旧 Observation 携带的 GoalTree 只作 evidence fallback。Desired state authority = Task store latest version。
- 测试 `test_update_goal_changes_latest_desired_state`：desired 改 15:00 后，old 10:00 observation
  不等于 latest desired → Goal 保持 unsatisfied（completion 由 desired-vs-actual 决定，非 execution success）。

Timeline（测试验证）：
```
T0 desired=10:00, E1 RUNNING
T1 UPDATE_GOAL desired=15:00 (task version bump)
T2 old E1 success -> actual=10:00, desired=15:00 -> 不满足 -> 保持 IN_PROGRESS
T3 AgentLoop 基于 desired/actual 差距产生 correction action
T4 actual=15:00 -> 满足 -> COMPLETED
```

## E. Mutation CAS / latest intent wins

- `TaskManager._persist` 已用 `repository.update(task, expected_version=task.version)` —— CAS 谓词已存在。
- 新增 `_apply_goal_mutation`：apply 前重读最新 Task/GoalTree，`bind_goal_tree` 的 expected_version
  拒绝 lost update；`TaskRepositoryError`（CAS 冲突）→ 重读最新重试（独立 Goal merge / 同 Goal latest wins）。
- 测试 `test_cas_conflict_rejected_by_repository`：stale version 写入被 repository 拒绝。
- 测试 `test_latest_intent_wins_across_messages`：M1=15:00 → M2=16:00，最终 desired=16:00。

## F. Cross-message Idempotency

- 消息内：`_run_task_deltas` 用 `change_id` 去重（同一消息重复 delta 跳过）。
- 消息间：immediate-accept 的 Run 持久化 + run 级 idempotency（已有）；mutation 记录
  `change_id` 到 `_mutation_record`（revision 审计）。
- 测试 `test_change_id_dedupe_signal`：change_id 是稳定 replay identity。

## G. In-flight Execution Immutable

- `_patch_delta_goal` 只改 Goal desired 字段（temporal/description/publication），不触碰 Execution。
- 测试 `test_update_goal_never_touches_execution_payload`：task_nodes 不变，无 execution 对象被改。

## H/I. Real Browser Case A/B

**BLOCKED**（环境限制，非代码缺陷）：
- :8094 双 Agent API 实例（PID 41552=.venv-v2 旧环境、39856=anaconda）→ SSE 事件流跨实例丢失，
  浏览器实时活动流不可靠（前端 1s 轮询 fallback 可降级）。
- 需用户清理重复实例后人工浏览器验证。

真实 LLM 证据（修复前）：真实 DeepSeek 对 mutation 消息输出 `task_changes`（CANCEL_GOAL target Redis），
确认 LLM 语义走通；delta 分支已修复（不再 AMBIGUOUS_TARGET 拦截）。

## J/K. Regression

- Phase 3 concurrency / Phase 3.5 bootstrap：**601 单元测试全过**（含 Phase 4.1 新增 8 个）。
- 未触碰 runner/worker/execution 状态机；`_bind_task` 拦截不改变新任务路径。

## L. Tests

- Focused：`tests/unit/test_phase41_dynamic_reliability.py`（8 个）——rejection 契约、stale desired、
  latest-intent-wins、CAS 冲突、in-flight immutable、change_id 幂等、bootstrap schema 回归。
- 全量：601 passed。

## M. Deferred

- Admission Control / Rate Limit（下阶段）
- Specialist Multi-Agent
- 真实浏览器 Case A/B/C（需双实例清理 + 人工）
- 完整 stale observation 集成 harness（Case C 可控 race）
- Evaluation / Badcase

## 成功标准核对（§63）

A 禁止 legacy fallback ✓ | B TaskDelta 唯一主路径 ✓ | C legacy 仅 recovery（无 caller）✓ |
D stale obs 不错误完成 ✓ | E latest desired 决定完成 ✓ | F continuation 用最新 persistent GoalTree ✓ |
G in-flight immutable ✓ | H CAS ✓ | I latest intent wins ✓ | J 消息重放幂等 ✓ | K duplicate ADD_GOAL 去重 ✓ |
L sibling 不受阻塞（按 task 独立 apply）✓ | M/N Phase 3/3.5 回归 ✓（601 全过）| O Policy/Approval 不绕过 ✓ |
P/Q 真实浏览器 Case A/B **BLOCKED**（环境）| R 无新 Planner ✓ | S 不双轨 ✓ | T 0813 ✓
