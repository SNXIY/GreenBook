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
Verifier / Replan / Final Response（仅复杂路径）
```

Java 后端始终是用户身份、帖子、评论、资源归属和发布状态机的事实源。Assistant
不直接访问业务数据库，也不能通过计划或 Prompt 绕过 Java 权限。

## Agent Runtime

- Adaptive Supervisor 在第一次模型调用中同时输出结构化 Intent 和
  `DIRECT / TOOL / CREATOR / ORCHESTRATED` 执行路径。
- 执行器根据 Tool Registry 的真实风险和副作用元数据复核路径；模型不能将写操作
  降级为快路径。
- `DIRECT` 不启动 Planner 和 Verifier；`TOOL` 只允许一个只读工具；
  `CREATOR` 只允许一个 `creator.create_draft`；其余任务进入完整编排。
- Planner 根据 Intent、Agent Registry、Skill Registry 和工具契约生成动态 Task DAG。
- Supervisor 支持并行只读步骤、条件分支、预算重规划和结果验证。
- 同一用户采用 Lane 隔离：`READ` 默认最多并发 3 个，`WRITE` 默认串行；
  PostgreSQL 租约和 advisory lock 保证多 Worker 下仍遵守并发边界。
- Run、Step、Checkpoint、Approval、SideEffect 和事件流持久化到 PostgreSQL。
- Worker 使用数据库租约、心跳和 fencing，拒绝旧 Worker 的迟到结果。
- 支持 `interrupt`、`resume`、`retry`、取消传播和定时任务。

## Blackboard 与 Artifact

每个成功步骤都发布不可变 Artifact，记录：

- 产生它的 Run、Task、Agent 和 Tool；
- Artifact 类型、版本和 SHA-256；
- 父 Artifact 列表；
- 完整结构化内容和创建时间。

Blackboard 聚合 Intent、Task Ledger、Progress Ledger、Checkpoint、任务状态、
Artifact 和人工审批。Agent 通过产物引用协作，不覆盖其他 Agent 已发布的结果。

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

## Creator 与 Moderation 边界

`creator.create_draft` 通过 Tool Registry 调用 Creator Agent 的持久化 HTTP 任务。
该链路保留类型契约、任务版本、Checkpoint、取消和轮询能力。MCP 用于可插拔工具，
不为了协议形式替换核心创作任务链路。

普通 AI 创作和社区运营不自动加入 Moderation Agent。审核保持独立：

- 用户手动创作的帖子走 Java 发布向导中的审核步骤；
- 管理员治理和复核使用 Moderation Agent；
- Assistant 不能自行决定跳过或插入安全门禁。

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

## 四层记忆

Assistant 将记忆分为：

1. 当前 Run 的工作状态；
2. 有界的最近对话上下文；
3. PostgreSQL 中的情景任务记忆；
4. Qdrant 中可重建的语义索引。

历史记忆是不可信证据，不能授予权限，也不能证明历史副作用已经在当前任务执行。
敏感请求和普通即时问答不会自动沉淀为长期任务记忆。

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

```powershell
uv sync --dev
uv run python -m pytest -q
uv run python evals\run_runtime_report.py
```

评估覆盖：

- Intent Accuracy；
- Task Coverage；
- Planning Efficiency；
- Tool / Agent Selection Accuracy；
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
