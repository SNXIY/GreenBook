# GreenBook Decision Ownership Audit

生成时间：2026-08-13
审计方式：全源码证据收集（git grep / 逐文件精读 / 测试现状核对），结论均附 `文件:行号`。

## 1. Before — 各组件当前决策权现状

| Component | Current Input | Current Output | 决定"下一步做什么"? | 改变 capability/operation? | 自动选 publish/schedule? | 绕过 GoalTree/GoalCompiler? | 持有 global target 状态? |
|---|---|---|---|---|---|---|---|
| GoalDecomposer | 已解析 Command + context + capability 描述符 | GoalTree（含 publication_intent/temporal_constraint） | 否（决定 WHAT goals） | 否（只产出语义能力清单） | 否（prompt 禁止选工具/执行方式） | 否 | 否 |
| GoalCompiler | GoalTree + Command | PlanGraph / TaskPlan | 否（确定性编译） | **否——fail-closed 校验**（`_validate_goal_semantics` compiler.py:403、`_validate_step_semantics` :438） | 否——只做别名归一化（publish_at→run_at，compiler.py:287-294,530-551）与 ISO 时间提取（`_extract_datetime` :593，注释明确"contract adapter, not intent routing"） | 否 | 否 |
| AgentLoop | Command + GoalTree + Observation | AgentAction（TOOL_CALL/CREATE_TASK/UPDATE_PLAN/FINISH/ASK_USER） | **是（唯一正常轮次决策者）** | 否（工具名 hint 仍经 ToolSelector 校验，loop.py:535-541） | 否 | 否 | 否（仅 state 内 current_task） |
| DynamicPlanner | GoalTree + 失败 evidence + 候选工具 | PlanningDecision（INSERT/REMOVE/REORDER/RETRY/ALT_TOOL/ASK_HUMAN…） | **部分（仅失败恢复路径，loop.py:282-365 触发）** | **⚠️ 残余点：INSERT_STEP 可插入任意 capability 的 TaskNode（apply dynamic.py:366-370，无语义约束）** | 否（prompt 禁止重释用户意图，dynamic.py:500-524；ALT_TOOL 限候选集、RETRY 限 scope 变更） | 否 | 否 |
| ToolSelector | Goal + Observation + ToolMetadata catalog | SelectedTool | 否（WHICH tool） | 否——fail-closed（`TOOL_NOT_IN_CATALOG`，selector.py:68-71,103-106） | **否（无 operation fallback）** | 否 | 否 |
| TaskManager | task 状态请求 | Task 生命周期变更 | 否（纯状态机，manager.py:3-5 注释 "never calls a tool or worker"） | 否 | 否 | 否 | 否（`get_active_tasks` 是过滤不是决策） |
| ConversationRuntimeAdapter | 消息 + context | RuntimeResult | 否（适配已确定的 action/plan） | **否——有 coverage 守卫**（`_require_plan_goal_coverage` :841-863；submit_tool 仅单 goal 绑定 goal_id :529-533，多 goal 被拒） | 否（CONTROL 特判 L198-207 与 active-draft 注入 L684-730 是注释明确的有意特判，且被 coverage 兜底） | **否（上轮 bug 已修复）** | 否 |
| ExecutionInput builder | TaskPlan | ExecutionInput（队列请求） | 否（纯封装，input.py 无 fallback） | 否 | 否 | 否 | 否 |
| ToolRuntime | InvocationContext | InvocationResult | 否（只执行） | 否 | 否 | 否 | 否 |
| ToolPolicyGate | ToolMetadata + scopes + approval | SYNC/QUEUE/WAITING_HUMAN/DENY（policy.py:55-137） | 否（WHETHER allowed） | 否（只定执行模式） | 否 | 否 | 否 |
| TargetResolver | Command + context | TargetCandidate（RESOLVED/AMBIGUOUS/NOT_FOUND） | 否（WHICH 业务对象） | 否（只解析对象，不推导动作） | 否 | 否 | 否（ACTIVE 绑定为上下文读取） |

### Current Decision Graph（真实调用关系）

```
GoalDecomposer ──(GoalTree)──▶ GoalCompiler ──(TaskPlan)──▶ RuntimeAdapter.submit_plan
       │                            ▲                            │
       │                            │ INSERT/REMOVE/REORDER      │ PLAN_GOAL_COVERAGE 守卫
       ▼                            │ (失败恢复)                 ▼
   AgentLoop ──(action)─────────────┴──────────────────▶ ExecutionInput ──▶ Queue/Worker
       │        │
       │ TOOL_CALL（单 goal）          CREATE_TASK（多 goal/副作用）
       ▼                              │
   ToolSelector ──(SelectedTool)──▶ submit_tool（coverage 守卫）▶ ToolRuntime
       │
   ToolPolicyGate（模式）──▶ ToolRuntime
```

### 2. Conflict — 重复 authority 清单

| # | 位置 | 冲突 | 严重度 |
|---|---|---|---|
| 1 | `DynamicPlanner.apply` INSERT_STEP（dynamic.py:366-370） | **planner 可插入任意 capability 的 TaskNode**（如向 DRAFT_ONLY 树插入 SCHEDULE_PUBLISH），绕过 GoalCompiler 的 `_validate_step_semantics` 单调性校验（compiler.py:438-491）——因为 replan 后的树会重新编译（loop.py:330-337 → 后续 CREATE_TASK），但编译时树中已有新节点，校验对象是"树内所有 goal 的语义"，而插入节点携带的是**树外引入的 capability**，可能绕过 goal 级 intent 检查 | 中（仅失败恢复路径可达，但违反"semantic action 只能变得更具体"不变量） |
| 2 | `RuntimeAdapter.submit_tool`（conversation_runtime_adapter.py:473-549） | 单工具直提为一步计划（plan_source="AGENT_TOOL_SUBMISSION"）——**设计内**（单 goal 路径），已有 coverage 守卫 + 单 goal 限制。不再构成绕过 | ✅ 已收敛（上轮修复） |
| 3 | AgentLoop reason vs DynamicPlanner | 每轮决策唯一性：DynamicPlanner 仅在 `planner_trigger`（EMPTY/FAILED/ok=False）时介入（loop.py:282-287），**不参与正常轮次**；且 planner 决策经 AgentLoop 强制执行并受目录校验（`_set_preferred_tool` loop.py:393-408） | ✅ 已收敛（方案 B 成立：DynamicPlanner 是 AgentLoop 的失败恢复内部实现） |
| 4 | `conversation_runtime_adapter.py:198-207` CONTROL→MODIFY 改写 | 特判 PUBLISH_NOW 不是 CONTROL——注释明确的有意语义，不是重复决策 | ✅ 保留 |

**结论**：真正需要收敛的重复 decision owner 只有 **#1（INSERT_STEP capability 无约束）**。

### 3. After — 目标职责表（现状已基本达到）

```
GoalDecomposer   = WHAT goals（产出含 publication_intent 的语义 GoalTree）
AgentLoop        = WHAT next action（唯一正常轮次决策者）
GoalCompiler     = HOW to structure deterministic dependencies（+ fail-closed 语义单调性校验）
DynamicPlanner   = AgentLoop 的失败恢复内部实现（INSERT_STEP 受语义单调性约束）
ToolSelector     = WHICH tool（fail-closed，无 operation fallback）
ToolPolicyGate   = WHETHER allowed（SYNC/QUEUE/WAITING_HUMAN/DENY）
Runtime/Worker   = HOW reliably execute（保留全部 durable 能力）
TaskManager      = WHICH existing logical task this turn belongs to（纯状态机）
TargetResolver   = WHICH business object user refers to（不推导动作）
```

## 4. 本轮最小收敛改动

| # | 改动 | 文件 | 理由 |
|---|---|---|---|
| 1 | `DynamicPlanner.apply()` 的 INSERT_STEP 增加 publication 语义单调性校验（与 GoalCompiler `_validate_step_semantics` 规则一致）：DRAFT_ONLY 树不得插入 SCHEDULE_PUBLISH/PUBLISH_NOW；SCHEDULED 树不得插入 PUBLISH_NOW；IMMEDIATE 树不得插入 SCHEDULE_PUBLISH | `packages/agent_core/greenbook_agent_core/planning/dynamic.py` | 关闭唯一残余的"树外引入业务能力"路径 |
| 2 | 新增 `tests/unit/test_semantic_action_monotonicity.py`：GoalCompiler 允许/禁止矩阵 + INSERT_STEP 单调性 | `tests/unit/` | 固化 §24/25/26 语义转换规则 |

## 5. Deferred Work（明确不做）

- **Observation-driven continuation 大规模改造**：当前 AgentLoop 在 queue 接受后停机（loop.py:239-256），步骤级失败在 Worker 内重试/replan（worker.py `_request_replan`）。"每个步骤后回到 AgentLoop 再决策"需要 Worker→AgentLoop 的新回调边界，涉及执行运行时契约变更——独立排期，本轮不做。
- **Approval 作为 Observation 回流 AgentLoop**：当前 WAITING_APPROVAL 暂停/恢复路径（runtime_agent_service `_pause_for_approval`/`resume_human_interaction`）已存在且可靠，不改变其实现。
- **run_id/execution_id API 收敛**：见 `GREENBOOK_DIRECTORY_NAMING_AUDIT.md` §20.8，独立议题。
- **语义转换测试的 AgentLoop 集成级用例**（§27-28 的 Observation-driven / Tool-failure continuation）：已有 e2e 覆盖（test_golden_flows.py），不在本轮新增。

## 6. 验证

- 新增测试：`tests/unit/test_semantic_action_monotonicity.py`
- 回归：`tests/unit/test_dynamic_planner.py`、`tests/unit/test_multi_goal_semantic_isolation.py`、`tests/unit/test_plan_validation.py`、`tests/unit/test_goal_decomposer.py`、`tests/unit/test_tool_policy_gate.py`、`tests/unit/test_phase7_agent_intelligence.py`
