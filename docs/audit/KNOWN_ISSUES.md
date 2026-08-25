# GreenBook 现有缺点清单（实测审计）

> 依据：真实端到端测试（账号 10001000000）+ 代码审查 + 与 nanobot-main 的多任务机制对比。
> 时间：2026-08-15。状态标记：✅已修 / 🔴待修（阻塞）/ 🟠待修（重要）/ 🟡待修（改善）。
> 本文是**只读审计**，供其他 agent 修复时参考；每一节给出现象、根因、影响与建议修复方向。

---

## P0 — 多任务/多目标可靠性（核心）

### P0-1 🔴 AgentLoop 一次只决策一个动作，多任务迭代数 = 任务数 × 能力数

**现象**：案例一（3 篇帖子）turn 1 需要 349s 才完成；早期版本直接 `AGENT_MAX_ITERATIONS`（24 迭代耗尽，179s FAILED）。单任务则 40-60s 正常。

**根因**：`packages/agent_core/.../agent/loop.py` 的 Observe→Reason→Act 循环**每迭代只产出 1 个 `AgentAction`**（`reason()` 返回单个 action）。多任务时每个任务的每个能力（SEARCH→GET_POST_DETAIL→ANALYZE→GENERATE→SCHEDULE）都要一次 LLM 决策 + 一次异步提交 + 一次观察续跑。对比 **nanobot**：一次 LLM 响应可返回 **N 个 tool_calls**，runner 用 `asyncio.gather` **并行执行**（`nanobot/agent/runner.py::_execute_tools`），多任务一轮完成。

**影响**：多任务慢（3 篇 6 分钟）、token 消耗大、迭代预算紧张时部分任务丢失（曾实测只完成 2/3）。

**建议**：
- 改造 `reason()` 支持**批量动作输出**（structured output 的 actions 数组），`act()` 并行执行独立动作、串行执行依赖动作（参考 nanobot `_partition_tool_batches`）。
- 或**确定性推进已编译 plan**：无歧义的能力链（SEARCH→…→SCHEDULE）由队列 worker 按序执行，loop 只在分支/失败/歧义点介入，减少每步 LLM 决策。

### P0-2 🔴 任务级执行不完整 + 续跑链路脆弱（多轮实测复现）

**现象**（修复前，多轮真实复现）：
- 第一个任务只执行 SEARCH 就停（debug 日志：`GET_POST_DETAIL` 从 iter=2 到 iter=24 反复调 `community.search_public_posts`，Dynamic Planner 退化循环）；
- 第二个任务的 ANALYZE 提交队列被 Worker 拒绝（`WRONG_EXECUTION_SEMANTICS: Reasoning-backed capability reached Worker`）；
- run 报 COMPLETED 但任务只完成一部分。

**根因链**（均已定位）：
1. `SEARCH_RESULT` 的 resource_refs 没进 facts 投影（`context/projection.py::_compact_step` 只保留单个 resource_id、`_compact_resource_ref` 丢 `kind`）→ post_ids 丢失 → GET_POST_DETAIL 无 post_id → 模型无法调 `get_post`；
2. loop 的 `_facts_by_goal_from_state` / `_execution_read_evidence` 不给模型暴露 post_ids（模型看不到可读的帖子）；
3. reasoning 能力（`is_llm_step`，如 ANALYZE）被 `_incremental_plan` 当普通 execution 提交 → Worker 拒绝；
4. `_run_task_deltas` 串行跑每个任务 loop。

**已修** ✅（本轮）：`_compact_step`/`_compact_resource_ref` 保留 resource_refs+kind；`_facts_from_execution_states`（adapter+loop）提取 post_ids；`_execution_read_evidence` 暴露 post_ids 给模型；`submit_plan` 拦截 reasoning（`REASONING_STEP_NOT_SUBMITTABLE`）；task_delta 并行（`asyncio.gather`）；`_merge_mutation_results` 并行状态合并。

**待观察** 🟠：修复后**未在干净环境做全量回归**（测试环境被多进程/僵尸端口干扰）。需用户重启 agent 后跑 6 案例，确认：第一个任务的 GET_POST_DETAIL→ANALYZE→GENERATE→SCHEDULE 完整、三个任务草稿+调度全部落库。

### P0-3 🔴 run 终态收敛与异步 execution 的时序不一致

**现象**：run 报 `COMPLETED` 后，execution 仍在队列执行并创建草稿（实测：turn 1 于 07:48 结束，任务2 草稿 07:43 才落库——早于 run 结束；另一轮 run 07:36 完成但 generate execution 07:43 才完成）。

**根因**：`_merge_mutation_results`/`_merge_parallel_runtime_results` 只反映**已提交/已完成的 execution 快照**，队列里尚未完成的工作不在其视野；`_reconcile_agent_run_status` 依赖 `owned_messages` 判定收敛，但消息 ACKED 与 execution 完成、observation 写回之间有时差。

**影响**：前端看到 run 完成但任务仍在推进；会话历史投影与真实业务结果错位；后续 turn 基于不完整事实决策。

**建议**：
- run 收敛前校验**该 run 所有 task 的 execution 是否全部 terminal**（查 execution 表，而非只看队列消息）；
- 或为多任务引入**任务级完成事件**（每个 task 的最后一个 execution 完成才允许 run terminal）。

---

## P1 — 对话/引用/调度正确性

### P1-1 🟠 轮次引用消解不稳定（"Java 那篇" → MUTATION_TARGET_REQUIRED）

**现象**：案例一第二轮"Java 那篇别明天早上发了，改成明天下午 4 点"多次返回 `MUTATION_TARGET_REQUIRED`（WAITING_USER），即使 turn 1 已创建 3 个任务（PLANNING 状态，`get_resolvable_tasks` 含 PLANNING）。单任务引用（案例二/四）此前通过过，说明是模型行为不稳定 + 多任务上下文干扰。

**根因**（部分确认）：`needs_target_resolution` 由模型输出的 target_reference 缺失触发；`_resolve_delta_target` 依赖 label 匹配，但模型常不输出引用。turn 1 失败时任务停在 PLANNING 加剧了级联。

**建议**：
- 加强 `_COMMAND_SYSTEM_PROMPT` 的引用引导（已做过一轮，需针对"多任务列表"场景再强化，把候选任务的标题/goal 显式给模型）；
- 增加**确定性兜底**：消息含"那篇/这篇/刚"且同会话只有 1 个同主题任务时直接解析；多个候选时用 `_normalized_label` 的精确匹配优先（已有部分）；
- 增加集成测试覆盖多任务 turn 2 引用。

### P1-2 🟠 调度时间链路过长，时区易错（已修一处，链路仍脆弱）

**现象**：请求"后天晚上8点"→ agent 解析 `2026-08-17T20:00:00+08:00`（正确）→ Java 侧 scheduled_publications 曾存 `08-17 04:00`（差 8-16 小时）。

**根因**：`argument_binder.py::_resolve_schedule_time` 把**已是 ISO 的 run_at 又交给自然语言解析器**，ISO 被 `_parse_explicit_date`+clock 当本地时间解析（Z 结尾被误当 +08）。**已修** ✅：Z 透传、显式偏移转 UTC。

**残余风险** 🟡：`time_parser.format_local_schedule_time` 与 Java `timezone` 列仅用于展示；Java 侧调度执行（`ScheduledPublicationService` 的 `findDue(Instant.now())`）依赖 run_at 的绝对正确性，建议加一个**端到端时间断言测试**（中文时间→Java 落库→读回）。

### P1-3 🟠 共享 context 导致并行任务间 facts 相互干扰

**现象**：多任务并行时，一个任务的 observation/execution_states 会进入其他任务的 context（`_build_context_snapshot` 是会话级），曾观察到任务完成状态串扰（任务1 的 goal 看到任务2/3 的 draft 而误判完成）。

**已缓解** ✅：facts 按 goal_id 分组 + `_goal_tree_finished_ok` 严格守卫（含生产能力 goal 必须有 draft/schedule/post）。

**残余风险** 🟡：分组依赖 goal_id 的正确性（曾出现 goal_id 漂移的中间态）；建议在 `_run_task_deltas` 的 `_resume_one` 里对 context 做**深拷贝隔离**（per-task context），并写并发测试验证两个任务同一时刻互不串扰。

---

## P2 — 工程/运维/测试

### P2-1 🟠 本机 8094 端口多 uvicorn 进程 + 僵尸端口（Windows）

**现象**：实测发现 4+ 个 `uvicorn apps.agent_api...` 进程并存，`netstat` 显示多个 LISTENING 但 `Get-Process` 找不到对应 PID（僵尸端口）；进程被杀后端口不释放（`Errno 10048`）；`--reload` 的代码热更新不可靠（旧进程继续服务旧代码）。

**影响**：**这是多轮测试结果不可复现的直接原因**（修复代码未加载、行为漂移）。

**建议**：
- 启动脚本增加**端口占用检测与单实例守护**（启动前 `netstat` 检查，已有则提示而非硬启）；
- `--reload` 改为显式 `--reload-dir` 限定 watch 范围；
- 提供 `scripts/stop-agent.ps1` 一键按命令行特征清理。

### P2-2 🟡 .venv 与 .venv-v2 双环境 + PYTHONPATH 依赖脚本

**现象**：`uv run` 用 `.venv`（editable 装工作区包），`start-agent.ps1` 用 `.venv-v2`（靠脚本显式设 PYTHONPATH）；直接用 `.venv-v2\python -m uvicorn` 会 `ModuleNotFoundError: greenbook_agent_core`。

**建议**：统一为单一环境；或把 PYTHONPATH 写入 `.venv-v2\pyvenv.cfg`/pth 文件，消除对启动脚本的隐式依赖。

### P2-3 🟡 测试覆盖缺口（重要场景无自动化）

| 缺口 | 影响 |
| --- | --- |
| task_delta 多任务端到端（3 任务并行 + 续跑完整落库） | 多任务回归只能靠人工 |
| turn 2 引用消解（多任务下"那篇/这篇"） | 本轮 4 次真实测试均失败 |
| run 收敛时序（run terminal vs execution 完成） | 状态投影错位无回归 |
| 调度时间端到端（中文时间 → Java 落库 → 读回） | 时区回归无保护 |
| 多任务并行 facts 隔离（两任务并发互不串扰） | 共享 context 回归无保护 |
| AgentPanel follow-up 提示（前端） | 无 UI 测试 |

**建议**：优先补 1/2/4/5（后端集成测试，mock LLM + 真实 repository）。

### P2-4 🟡 历史文档与现状严重不符

**现象**：`docs/migration/`、`docs/project-understanding/`、`docs/architecture/` 大量文档仍描述 creator-agent 服务、旧拓扑（CreatorClient、/creator-api、8092 等）；`docs/development/*` 已更新但引用链断裂。

**影响**：新开发者（或修复 agent）被误导。

**建议**：在 `docs/architecture/` 下标注"当前权威文档"（`SERVICE_COMMUNICATION.md`、`CURRENT_ARCHITECTURE.md`），历史文档统一加"ARCHIVED"横幅；逐步清理。

### P2-5 🟡 环境变量残留

**现象**：用户 `.env` 仍有 `GREENBOOK_CREATOR_*`（代码不再读取，但 `rotate-dev-secrets.ps1`、`check-runtime-*.ps1` 等已清理引用后，残留变量易误导）；`GREENBOOK_AGENT_MAX_ITERATIONS` 刚引入（默认 24），与 loop.py 内部默认 8 不一致（`AgentLoop` 默认 8，main.py 覆盖为 24）——**配置来源分散**。

**建议**：配置统一从 `.env`/环境读取并集中校验（一个 `agent_config.py`），消除硬编码默认值的多份拷贝。

### P2-6 🟡 前端 follow-up 链与恢复卡

**现象**：`AgentPanel` 已实现乐观卡、轮次中间注入、父卡提示（depth-1）；但**多层 follow-up**（A→B→C）的提示链只渲染一层；面板重开时恢复的 run 卡状态是快照（不实时刷新，依赖轮询）。

**建议**：follow-up 链渲染递归化；恢复卡接入 run 事件流而非快照轮询。

---

## 与 nanobot 的核心机制差距（长期演进参考）

| 维度 | green-book（现状） | nanobot-main | 差距影响 |
| --- | --- | --- | --- |
| 动作决策 | 每迭代 1 个 action（Observe/Reason/Act） | 一次 LLM 返回 N 个 tool_calls 并行执行 | 多任务慢、迭代预算紧 |
| 计划 | GoalTree 编译 → 增量 execution（每能力一个） | 对话内隐式编排，工具即能力 | 状态机复杂，每步都可能出错（本轮多个 bug 源于此） |
| 执行 | 异步队列 + observation 续跑 | 工具同步 await、结果回填 | 时序/一致性脆弱（run 收敛、续跑断点） |
| 预算 | 每 run 迭代预算（默认 24），与任务数无关 | max_iterations 单 loop，模型自编排 | 多任务易耗尽、部分任务丢失 |
| 上下文 | 会话级共享（facts 按 goal_id 分组） | 单对话上下文自然隔离 | 并行任务 facts 串扰风险 |
| 恢复 | Execution checkpoint + resume_context | 每轮 checkpoint（awaiting_tools/tools_completed） | green-book 更重但更细 |

**结论**：green-book 的"智能编排 + 可靠执行"分层在单任务/单目标下表现好（真实测试 40-60s 完成）；**多任务/多目标/交替执行**是当前最弱场景，根因是**动作粒度（单 action）+ 异步链路过长**。参考 nanobot 的**批量动作决策**和**plan 内确定性推进**是主要演进方向，同时保留 green-book 的持久化/幂等/checkpoint 优势（nanobot 无同等持久化深度）。

---

## 快速修复清单（给接手 agent）

1. 跑一次干净全量 6 案例（先清 8094 多进程/僵尸端口），确认 P0-2 修复是否真正收敛；
2. 修 P0-3：run 收敛改为校验"该 run 所有 task 的 execution 全 terminal"；
3. 修 P1-1：多任务 turn 2 引用兜底 + 集成测试；
4. 补 P2-3 的 4 个集成测试；
5. 统一 agent 配置来源（P2-5）与启动单实例守护（P2-1）。
