# GreenBook Community Assistant Agent

GreenBook 社区智能助手不是固定工作流聊天机器人，而是一个受社区边界治理的
Agent Runtime。LLM 负责意图理解、任务拆解和结果表达；任务状态、权限、审批、
幂等、版本与失败恢复全部由确定性代码负责。

## 核心链路

```text
User
  ↓
Adaptive Supervisor（Intent + Execution Path）
  ├─ DIRECT：一次模型调用后直接回答
  ├─ TOOL：单个只读工具 + 最终回答
  ├─ CREATOR：Creator 持久任务 + 确定性结果交付
  └─ ORCHESTRATED
       ↓
     Planner Agent ── Skill Registry
  ↓
Plan Compiler（能力图 / 参数 / Artifact 契约 / 目标覆盖）
  ↓
Supervisor / LangGraph DAG
  ↓
Policy Engine
  ↓
Tool Registry / Durable Tool Queue
  ├─ Java Community API
  ├─ Creator Agent durable HTTP task
  └─ allowlisted MCP tools
  ↓
Artifact + Blackboard + Checkpoint
  ↓
Progress Supervisor ── Continue / Replace Pending Plan / Fail
  ↓
Verifier / Replan / Final Response（仅复杂路径）
```

Java 后端始终是用户身份、帖子、评论、资源归属和发布状态机的事实源。Assistant
不直接访问业务数据库，也不能通过计划或 Prompt 绕过 Java 权限。

## 两级路由与模型降级

任务路由和模型路由是两层独立决策：

- Adaptive Supervisor 负责 `DIRECT / TOOL / CREATOR / ORCHESTRATED`，决定是否需要
  工具、Creator 或完整 Planner；
- Model Router 根据运行阶段选择 `fast / strong / judge`，决定具体模型、是否开启
  thinking、超时和故障降级链。

默认策略针对 DeepSeek V4：

| 阶段 | 模型层级 | thinking | 目的 |
| --- | --- | --- | --- |
| 路由、Intent、摘要、最终表达 | fast / V4-Flash | 关闭 | 降低首包与日常任务延迟 |
| Planner | strong / V4-Pro | 开启（high） | 保证复杂 DAG 与约束推理质量 |
| Progress、Verifier、结构化修复 | judge / V4-Pro | 关闭 | 提高契约稳定性，避免无必要长推理 |

模型请求遇到超时、网络错误、429 或可恢复 5xx 时，会切换到不同模型/模式；连续失败的
候选会进入有界 cooldown。模型 4xx 配置错误不会盲目重试。路由策略签名写入
`runtime_identity`，运行统计可从 `GET /actuator/health` 的 `model_router` 查看。

可通过环境变量替换三个模型，或使用
`ASSISTANT_MODEL_ROUTE_OVERRIDES_JSON` 将某个阶段切换到 `fast / strong / judge`，例如：

```text
ASSISTANT_MODEL_ROUTE_OVERRIDES_JSON={"answer.compose":"strong"}
```

## Agent Runtime

- Adaptive Supervisor 在第一次模型调用中同时输出结构化 Intent 和
  `DIRECT / TOOL / CREATOR / ORCHESTRATED` 执行路径。
- 执行器根据 Tool Registry 的真实风险和副作用元数据复核路径；模型不能将写操作
  降级为快路径。
- `DIRECT` 不启动 Planner 和 Verifier；`TOOL` 只允许一个只读工具；
  `CREATOR` 只允许一个 `creator.create_draft`；其余任务进入完整编排。
- Planner 根据 Intent、Agent Registry、Skill Registry 和工具契约提出动态 Task DAG；
  每个 Step 使用一个 `primary_capability`，目标所需能力保留在 Intent 层。
- `capability_graph.json` 是独立、版本化的能力本体。具体能力可以蕴含通用能力，例如
  `trend_analysis -> analysis`、`schedule_publish -> publishing`；路由和目标覆盖共享同一规则。
- 确定性的 Plan Compiler 在执行前检查工具存在性、参数契约、原子步骤、Agent 可路由性、
  目标能力覆盖以及上游 Artifact 类型；失败时把结构化诊断交给 Planner，在预算内修复。
- Supervisor 支持并行只读步骤、条件分支、阶段性进度判断和预算重规划；重规划会保留
  已完成的不可变步骤并替换尚未执行的旧计划，重复计划会被停滞检测拒绝。
- 连续只读分析链只在进入副作用步骤前评估一次进度；评估使用计划签名与完成步骤集合
  做幂等，Creator 等待恢复不会重复消耗模型预算。
- 验收采用混合 Verifier：纯分析、空证据和条件分支交给 LLM 做语义判断；包含外部写入
  且全部 Typed Artifact、非空证据与副作用回执均已验证时，由确定性代码直接验收，
  避免模型格式修复拖慢或推翻已经提交成功的操作。
- 同一用户采用 Lane 隔离：`READ` 默认最多并发 3 个，`WRITE` 默认串行；
  PostgreSQL 租约和 advisory lock 保证多 Worker 下仍遵守并发边界。
- Run、Step、Checkpoint、Approval、SideEffect 和事件流持久化到 PostgreSQL。
- Worker 使用数据库租约、心跳和 fencing，拒绝旧 Worker 的迟到结果。
- Run/RunStep 状态迁移统一按父行到子行加锁；事件序号使用 PostgreSQL transaction
  advisory lock，不占用 Run 行充当互斥锁。死锁和序列化冲突按可恢复基础设施错误从
  Checkpoint 自动重试，底层 SQL 只记录在服务日志中。
- 支持 `interrupt`、`resume`、`retry`、取消传播和定时任务。

## 跨轮目标与实体工作区

自然语言消息只用于理解语义；跨轮副作用不会依赖聊天文本猜 ID。运行时把最近的 Run
和不可变 Artifact 归约为 `ConversationWorkspace`，持续维护活动目标、可操作实体、
草稿版本、定时任务以及它们之间的关系。当前请求被解释为 `NEW_GOAL / CONTINUE /
MODIFY / CANCEL / RETRY / QUERY_STATE` 中的一种目标增量。

### TurnPlan 控制面（多 Goal / 可组合变更）

每条用户消息先进入控制面，而不是直接进入关键词剧本：

```text
Message → Adaptive Router
       → TurnPlan{ changes[], open_plan, tasks[] }
       → GoalResolver（ConversationGoal.id + focus_goal_refs）
       → TargetResolver
       → ChangeCompiler → DAG  或  open_plan → Planner
```

- **Goal 是一等公民**：Workspace / `active_goal_ref` / 实体一律使用真实
  `ConversationGoal.id`（`goal:<uuid>`），不再用 `goal:<run_id>` 冒充。
- **推迟绑定**：Run 不会在路由前默认挂到“最新 ACTIVE Goal”；先解析再 attach/create。
- **可组合 Change**：一句话「给 A 加代码并改成十分钟后发」= `CONTENT.APPEND` +
  `SCHEDULE.UPDATE`，由 `ChangeCompiler` 拼 DAG，而不是二选一关键词分支。
- **Focus Stack**：`focus_goal_refs` 记录仍在焦点中的 Goal；“刚才那个/第一个任务”
  优先落在焦点栈，而不是永远落在最新 Goal。
- **Task Bag**：同一条消息里明显独立的后续任务（如“顺便再写一篇 B”）拆成串行
  follow-up Run；同一 Goal 上的复合修改不拆包。
- **Router 提示优先**：`primary_operation` / `open_plan` / `follow_up_prompts`
  由 Adaptive Router 产出时，压过关键词安全阀；关键词只保留取消/立即发布等硬约束。
- **Goal 完结**：发布完成后 Goal 进入 `COMPLETED`，默认解析候选会收缩，降低串台。

- “改成十分钟后发布”会重新核验已有 Schedule 并原子更新时间，不创建第二个任务；
- “改成下午两点半发布”等明确中文时刻会由控制面解析为带时区的 `run_at`，避免为简单
  排期修改额外消耗 Planner 模型调用；
- “给帖子增加实战经验”会基于当前草稿生成新版本，再把已有 Schedule 迁移到新 SHA；
- “现在就发布”会先取消旧 Schedule，再核验最新草稿并进入立即发布审批；
- 同类实体不唯一且用户没有明确指代时，确定性解析器要求澄清，不让模型猜 ID；
- 身份型 Artifact Binding 选择依赖 DAG 中最近的版本，聚合型参考资料仍保留全部证据。

因此，多轮对话改变的是同一个目标的期望状态和实体关系，而不是每轮重新从零规划一条
互不相关的 Workflow。多 Goal 交织（发帖 A → 发帖 B → 改 A）走同一套解析/编译协议，
而不是为每个例句加分支。

“分析社区活跃用户及其发帖类型”不是一个伪装成万能分析接口的复合工具，而是：

```text
community.list_active_users
  ├─> community.list_posts_by_users
  └─> community.aggregate_post_topics
```

下游 `user_ids` 只能由执行器从上游 `USER_SET` Artifact 绑定，模型提供的用户 ID 会被覆盖。
`draft_id`、内容 SHA 和 Creator references 同样按类型契约绑定当前 Run 的祖先产物，而不是
根据某个固定场景或工具名猜测来源。相对定时
请求使用 `delay_seconds`，在实际创建定时任务时计算，避免长任务执行后绝对时间过期。

每个 Tool Definition 同时声明输入模型、输出模型、所需 capability、产出 Artifact 类型、
Artifact Binding、默认参数、上下文参数、风险和执行模式。Tool Adapter Runtime 统一完成
`Planner 参数 -> 上下文 -> 可信祖先 Artifact -> Pydantic 校验`，因此新增社区能力主要是注册
契约与传输适配器，不需要给 Planner 或 Supervisor 增加某句需求的关键词分支。

## Blackboard 与 Artifact

每个成功步骤都发布不可变 Artifact，记录：

- 产生它的 Run、Task、Agent 和 Tool；
- Artifact 类型、版本和 SHA-256；
- 父 Artifact 列表；
- 完整结构化内容和创建时间。

Blackboard 聚合 Intent、Task Ledger、Progress Ledger、Checkpoint、任务状态、
Artifact 和人工审批。Agent 通过产物引用协作，不覆盖其他 Agent 已发布的结果。
编译后的每一步会保存 `artifact_sources`，运行时只允许消费 DAG 祖先任务中类型匹配的
产物，避免并行分支的同类结果被错误绑定。

相关接口：

```text
GET /api/v1/assistant/runs/{run_id}/blackboard
GET /api/v1/assistant/runs/{run_id}/artifacts
GET /api/v1/assistant/runs/{run_id}/graph
```

## Skill Registry

业务技能位于 `app/skills/*/SKILL.md`。每个 Skill 声明：

- 名称和版本；
- 适用 Intent domain；
- capabilities；
- 允许使用的 tools；
- 风险和审批要求；
- 面向 Planner 的业务约束。

Skill 只指导规划，不授予权限。新增 Skill 不需要修改 Worker 的工具分发逻辑。

```text
GET /api/v1/assistant/skills
```

## Policy Engine

`app/community_policy.json` 是版本化、默认拒绝的策略清单。每次工具调用都会根据
用户角色、租户、Action、Resource、Risk 和审批状态返回：

```text
ALLOW
DENY
REQUIRE_APPROVAL
ALLOW_WITH_LIMIT
```

以下硬边界不能通过配置削弱：

- 只能调用注册工具；
- 禁止 Assistant 直接访问业务数据库；
- 禁止互联网和社区外系统；
- 社区写入必须由 Java 执行；
- 禁止绕过 Java 发布状态机。

策略结果持久化到 `assistant_policy_audits`，可以按 Run 查看：

```text
GET /api/v1/assistant/policy
GET /api/v1/assistant/runs/{run_id}/policy-audits
```

## Creator 边界与安全机制

`creator.create_draft` 与 `creator.revise_draft` 通过 Tool Registry / ToolRuntime 调用
Creator Agent 的持久化 HTTP 任务。`revise_draft` 当前是 **Assistant 兼容语义**：
仍向 Creator 提交 `CREATE_CONTENT`，由 Assistant 记录 `supersedes_draft_id` 与
Artifact `SUPERSEDES` 关系——**不是** Creator 原生 `REVISE_CONTENT`
（协议债务：`base_draft_id` / `expected_content_sha256` / `revision_instruction`）。

同一 `user + source_draft_id + expected_content_sha256` 的并发修订通过 PostgreSQL
advisory lock + SideEffect/`IdempotencyRecord` revision claim 串行化：第二个请求在
Creator submit 前返回 `CONFLICT`。Artifact 以 `provenance_key`（operation_key）幂等落库。

该链路保留类型契约、任务版本、Checkpoint、取消和轮询能力。MCP 用于可插拔工具，
不为了协议形式替换核心创作任务链路。

**内容审核 Agent 不在本项目范围内。** Assistant 不构造、不依赖、不调用审核服务。
通用安全能力仍然保留：Policy Gate、RiskLevel、Approval/HITL、Capability、
输入输出校验与草稿归属/hash 绑定。独立审核产品（若存在）由其他入口处理。

## Durable Tool Queue

动态发现的 MCP 工具使用 PostgreSQL Tool Job Queue：

- 请求哈希和幂等键；
- Worker 租约和迟到结果拒绝；
- 有预算的指数退避；
- 瞬时错误重试；
- 非瞬时错误或重试耗尽进入 Dead Letter；
- 用户可查看并人工重试自己 Run 下的 Dead Letter。

```text
GET  /api/v1/assistant/runs/{run_id}/tool-jobs
POST /api/v1/assistant/tool-jobs/{job_id}/retry
```

Creator 的长任务已经由 Creator 自己的 durable runtime 管理，因此不重复包进
Tool Job Queue。

## 五层记忆与会话状态

Assistant 将记忆分为：

1. 当前 Run 的工作状态；
2. 有界的最近对话上下文；
3. 从 Run / Artifact 事件归约出的 Conversation Workspace；
4. PostgreSQL 中的情景任务记忆；
5. Qdrant 中可重建的语义索引。

历史记忆是不可信证据，不能授予权限，也不能证明历史副作用已经在当前任务执行。
敏感请求和普通即时问答不会自动沉淀为长期任务记忆。

### Conversation Workspace 与跨轮协作

消息历史负责保留“用户说过什么”，Conversation Workspace 负责保留“当前共同在做什么”。
它在每个新 Run 开始时，从同一用户、租户、会话内的持久化 Run 和不可变 Artifact 归约出：

- 当前活跃目标、最近目标与失败恢复点；
- 草稿、帖子、评论、定时任务、分析结果等类型化实体；
- 当前焦点和尚未闭环的动作，例如“草稿已创建但未发布”；
- 每个实体对应的来源 Run、Artifact、版本指纹和业务状态。

Adaptive Router 同时输出 `turn_relation`（新目标、继续、修改、撤销、重试或状态查询）和
`referenced_entities`。引用必须来自 Workspace 候选；存在多个候选时必须向用户消歧，不能猜 ID。
Workspace 只解决连续性，不授予权限：任何历史对象进入写操作前，仍须通过注册 Tool 重新向
Java 校验归属、权限、业务状态和内容 SHA，再经过审批、幂等与 Java 状态机。

“创建草稿 -> 下一轮发布”只是这一机制的一种表现。同一套状态协议也服务于“把上一份改短”、
“取消刚才的定时任务”、“重试失败的分析”、“参考前面的分析继续创作”等多轮协作。只有唯一、
明确且低歧义的跟进才允许使用确定性快路径；其余请求由语义路由和 Planner 处理。

## 本地启动

先在仓库根目录启动中间件、Java 和 Creator：

```powershell
.\scripts\dev-up.ps1
.\scripts\start-be.ps1
.\scripts\start-creator.ps1
```

然后启动 Assistant：

```powershell
.\scripts\start-assistant.ps1
```

默认地址：

```text
http://127.0.0.1:8094
GET /actuator/health
```

默认 `ASSISTANT_PROCESS_ROLE=all`，同一进程运行 API、Run Worker、Tool Worker
和 Scheduler Worker。部署时可拆分：

```text
api
run-worker
tool-worker
scheduler-worker
```

## 数据库迁移

启动脚本会自动执行：

```powershell
python -m app.migrations
```

当前 Alembic head 为 `010_adaptive_execution`。Assistant 与 Creator 可以共用
`mindflow_creator` PostgreSQL 数据库，但使用独立表前缀和 Alembic 版本表。

## 测试与评估

Canonical regression（跨阶段报告请只比较此入口的数量，勿与 ad-hoc 筛选混比）：

```powershell
.\scripts\test-regression.ps1
# 等价: pytest -m "regression and not external" -q
```

`test_runtime_smoke_phase38` 需要本机 Postgres/Redis，标记为 `external`：

```powershell
pytest -m "regression and external" -q
```

全量仓库测试：

```powershell
uv sync --dev
uv run python -m pytest -q
uv run python evals\run_model_router_eval.py
uv run python evals\run_multiturn_eval.py
uv run python evals\run_runtime_report.py
```

评估覆盖：

- Intent Accuracy；
- Turn Relation Accuracy 与 Entity Reference Jaccard；
- Task Coverage；
- Planning Efficiency；
- Tool / Agent Selection Accuracy；
- Model Route Accuracy、Fast Route Rate、Thinking Route Rate；
- Task Recovery Rate；
- Stale Result Rejection Rate；
- Approval Accuracy；
- Artifact Version Correctness；
- Tool Job Completion Rate；
- RAG HitRate 和 MRR。

需要调用真实 DeepSeek 的规划评估：

```powershell
uv run python evals\run_planner_eval.py
```

`run_runtime_report.py` 读取真实 PostgreSQL 运行记录，只输出聚合数据。用于验证指标
计算公式的合成样本必须显式执行：

```powershell
uv run python evals\run_runtime_eval.py --fixture
```

合成样本输出会标记 `benchmark=false`，不得作为实际恢复率或 RAG 指标写入简历。
