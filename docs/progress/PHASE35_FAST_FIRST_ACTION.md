# Phase 3.5 — Fast First Action + Adaptive Control Plane + Cleanup

> 日期：2026-08-13
> 范围：缩短 first semantic action 延迟；Simple/Complex 自适应控制面；清理被替代的旧路径
> 约束：设计目标 0813；不新增 Planner/Workflow DSL；不绕过 ToolPolicy/Approval；Durable Runtime 不回归

## A. Real Badcase Before Trace（真实复现基线，2026-08-13）

真实请求："帮我找几篇关于 Agent 的帖子并总结共同方法"

本地 harness 复现（真实 DeepSeek + 真实 PostgreSQL + 真实 ConversationRuntimeAdapter，
`scripts/dev/phase35_llm_path_measure.py`，与生产实例 :8094 隔离）：

```text
execute 开始                              [0.000s]
Context build #1 (conversation+tasks+memory)   [0→1.0s]
LLM #1 CommandInterpreter.interpret       [1.000s, 耗时 1.359s]
Context build #2 (current_command)        [2.36→2.95s]
LLM #2 GoalDecomposer.decompose           [2.953s, 耗时 4.766s]
Context build #3 (AgentLoop._refresh)     [7.7→8.9s]
LLM #3 AgentLoop.reason                   [8.906s, 耗时 2.109s]
SEMANTIC_ACTION_SELECTED SEARCH_COMMUNITY [10.734s]
LLM #4 reflect                            [11.094s, 1.796s]  (first action 之后)
```

结果：`first_capability=SEARCH_COMMUNITY`，status FAILED（Java 无 token，属 harness 预期；
不影响 first action 前延迟测量）。

### 指标

| 指标 | Before |
|---|---|
| first action 前串行 LLM calls | **3** |
| LLM #1 interpret 耗时 | 1.36s |
| LLM #2 decompose 耗时 | **4.77s**（最大头） |
| LLM #3 reason 耗时 | 2.11s |
| context build 次数 | 3 |
| **TTFA**（SEMANTIC_ACTION_SELECTED - accepted） | **10.734s** |

与生产真实 trace 一致（用户报告约 10.4s）。

## B. First-Action Critical Path Before

```text
message accepted (POST 202, ~63ms)
  -> _build_context_snapshot            [deterministic: conversation/tasks/executions/preferences/memory]
  -> LLM #1 CommandInterpreter.interpret  -> Command
  -> _build_context_snapshot #2         [重复构建, 带 current_command]
  -> LLM #2 GoalDecomposer.decompose     -> GoalTree
  -> AgentLoop.run:
       _refresh_context                 [第 3 次 context build]
       observe                          [deterministic]
       LLM #3 reason                     -> AgentAction
  -> SEMANTIC_ACTION_SELECTED           [真实事件, first action 时刻]
```

## C. Duplicate Decision Ownership Found

| 模块 | 决定什么 | 输入 | 与前一模块的重复 |
|---|---|---|---|
| CommandInterpreter (LLM#1) | command type、goal、target/references、entities、constraints、semantic_operation、scope、risk、ambiguity、needs_clarification、required_capabilities、confidence | raw message + context + capability catalog | — |
| GoalDecomposer (LLM#2) | GoalTree 结构（root/children/dependencies/per-goal target/temporal/publication）、TaskNodes | Command（结构化）+ context + catalog | **不重读 raw message**，但对 single-goal 请求基本是"把 Command 的 goal 包一层 GoalTree"，重复表达用户目标；Simple 请求无新增信息 |
| AgentLoop.reason (LLM#3) | next AgentAction（TOOL_CALL/ASK_USER/FINISH/CREATE_TASK） | 完整 Command + GoalTree + Observation + tool metadata | prompt 已明示 "Do not reinterpret the raw user message"，但第一次 iteration 仍需重新读完整 command+goal_tree 选择第一个动作；当 Command 已含 required_capabilities 时，第一个动作是确定性可推导的 |

其他发现：

- Context 构建 3 次（adapter 2 次 + AgentLoop._refresh_context 1 次），每次全量
  （conversation + tasks + executions + preferences + memory recall）。
- `ToolSelector.select` 支持 `requested_tool` 提示：提供时**完全跳过 LLM**（确定性 catalog 校验）。
- GoalTree 是纯 Pydantic，可从 Command 确定性构造单 Goal 树（无需 LLM）。
- first action 前的 LLM 调用不包含 reflect（reflect 在 act 之后，不影响 TTFA）。

## D. Adaptive Control Plane Design（目标）

```text
Message
  -> Persist Run -> AgentRunner
  -> Build Relevant Context
  -> LLM Understanding (CommandInterpreter 扩展):
       Command + request_complexity + first_action (+ needs_decomposition)
  -> Simple:  deterministic GoalTree(Command) + validated first_action -> Act
  -> Complex: GoalDecomposer (保留) -> GoalTree -> AgentLoop
  -> Observation -> AgentLoop reason（后续 continuation 不变）
```

- Simple 判定来自 LLM structured output（不建 keyword router）。
- first_action 是 bootstrap hint：过 schema validation、goal ownership、
  capability validation、ToolPolicyGate、resource guard、approval 后执行。
- first_action 必须与 Goal State 一致（semantic monotonicity）。
- 后续 continuation 仍走 Observation -> AgentLoop。

目标 LLM calls before first action: 3 -> 1（最多 2）。

## E/F. Simple Path / Complex Path（已实现）

Simple 路径（request_complexity=SIMPLE 且 first_action 有效）：
```text
Message -> Context -> LLM Understanding (Command + first_action + complexity)
  -> _bootstrap_action 校验:
       complexity==SIMPLE
       first_action ∈ required_capabilities   (semantic monotonicity)
       唯一 catalog 工具声明该 capability
  -> 通过: _bootstrap_goal_tree (deterministic GoalTree, source=COMMAND_BOOTSTRAP)
           AgentLoop 首轮 bootstrap_action 直接 act (跳过 reason)
  -> 任一校验失败: 升级完整路径 (GoalDecomposer + AgentLoop reason)
```
Complex 路径（COMPLEX / 校验失败）：完整 GoalDecomposer + AgentLoop，未改动。

## G. LLM Call Count Before/After（真实 DeepSeek 测量）

| 阶段 | Before | After (SIMPLE) |
|---|---|---|
| first action 前 LLM calls | 3 | **1** |
| interpret | 1.36s | 2.31s（含新字段） |
| decompose | 4.77s | **跳过** |
| reason (首轮) | 2.11s | **跳过** |

## H. Real TTFA Before/After

| 指标 | Before | After | 变化 |
|---|---|---|---|
| TTFA（harness 真实 LLM） | 10.734s | **4.594s** | -57% |
| 端到端真实链路（生产） | - | 202(0.05s) → RUNNING(1.1s) → COMPLETED(35.2s) | 完整可用 |

## I. Context Slimming

未做主动 slimming（§19-20）：Simple 路径跳过了 decompose 的第二次 context build 和
AgentLoop 首轮 _refresh_context 中的重复构建。首次 context build 保留（历史/目标/memory 需求）。
本轮不改 context 内容，避免损害多轮引用能力。

## J/K/L. Runtime Safety / Concurrency / Progressive UX

- first_action 仅 bootstrap hint：过 semantic monotonicity（∈ required_capabilities）、
  唯一工具映射、ToolPolicyGate、goal ownership 后才执行；非法值升级复杂路径（§10/11/41）。
- Phase 3 并发：583 单测全过（含 phase3 相关），未触碰 runner/worker/execution 状态机。
- Progressive UX：SEMANTIC_ACTION_SELECTED 在真实 act 前由 AgentLoop activity_callback 发布；
  bootstrap 路径走同一发布点，无 fake progress（§16/42/43）。
- 后续 continuation：Observation -> AgentLoop reason 不变（§40/45）。

## M. Cleanup（§60 格式）

- **Removed**：无。审计确认 agent_core 内无 DEAD keyword-router/fallback（Phase 9 迁移已清理）。
- **Complex-only**：GoalDecomposer（Simple 请求不再强制走；保留给 COMPLEX + 升级 fallback）。
- **Legacy Recovery Only**：无新增。
- **Active**：一条主链（adaptive control plane：Simple/Complex 是同一架构的复杂度升级）。

## N. New Request Main Path

```text
Message -> Persist Run(202) -> AgentRunner -> Context -> LLM Understanding
  -> Simple: [deterministic GoalTree + bootstrap action] -> Validate -> Act
  -> Complex: GoalDecomposer -> GoalTree -> AgentLoop reason
  -> Observation -> AgentLoop reason (continuation, 统一)
```

## O/P. Real Evidence

- 真实 DeepSeek + 真实 PostgreSQL + 真实 adapter（harness）：TTFA 10.734→4.594s，3→1 LLM calls
- 生产实例端到端：Java 登录 → 202 → runner → COMPLETED(35.2s) → 助手消息生成
- 浏览器人工验证：需要（见下）

## 已知问题：8094 双实例（SSE 跨实例不可用）

:8094 有两个重复 agent_api 实例（PID 41552=.venv-v2 旧环境、39856=anaconda 当前环境），
SSE 事件流（进程内 event_store）可能被分发到非处理实例，导致前端 SSE 事件缺失。
前端已有 1s reconcile 轮询 fallback（DB 一致），体验降级可用。
建议：清理重复实例后，浏览器可完整看到实时活动流。

## 浏览器验证步骤（用户执行）

1. 访问 http://127.0.0.1:5173 ，用 13592298973 登录
2. 发送"帮我找几篇关于 Agent 的帖子并总结共同方法"
3. 预期：约 4-5s 内出现"正在查找相关内容…"，随后真实搜索结果与总结逐步出现
4. 若事件流不出现（双实例），页面 1s 轮询仍会逐步呈现结果

## R. Deferred

Phase 4 Dynamic Multi-Task、Specialist Multi-Agent、更深 evaluation/slimming。
