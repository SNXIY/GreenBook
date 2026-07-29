# GreenBook Community Assistant Agent

知光社区的 Community Agent Orchestration Platform。它不是聊天壳，也不是固定
workflow，而是由 LLM 生成受约束 Task DAG、由确定性控制面执行的多 Agent Harness：

- 独立 Intent Agent 将自然语言解析为 domain、goal、priority、constraints、
  required capabilities、scope、risk 和 confidence，不采用关键词路由；
- Planner 生成支持并行、依赖和条件分支的 Task DAG；LangGraph 编译并校验图拓扑，
  PostgreSQL 负责生产级持久化 Checkpoint、租约、审批和副作用一致性；
- Agent Registry 根据 capability 与 tool 契约动态选择 Search、Analytics、
  UserInsight、ContentCreation、Moderation、Publish、Interaction 或 MCP Agent；
- Supervisor 维护 Task Ledger 与 Progress Ledger，Verifier 可触发有预算的重规划；
- 无依赖的只读步骤会并行执行；创建、审核、发布等副作用严格串行跨越幂等边界；
- 任务支持 `interrupt / resume / retry`，已完成步骤不会重做，旧 Worker 的迟到结果会被拒绝；
- Creator 与 Moderation 都是同层专业 Agent：助手等待真实远程任务，不模拟创作或审核；
- 社区运营链路为“趋势与用户洞察并行 → 创作 → 审核 → PASS 条件发布”；
- MCP Gateway 只发现配置白名单中的远程工具，调用前校验 JSON Schema，并限制返回上下文；
- DeepSeek Supervisor 采用 `Observe → Plan → Act → Verify → Replan`，Verifier 不通过时只补规划缺失动作；
- 类型化 Tool Registry 同时声明输入与输出契约、风险、超时和模型可见性；返回资源与请求资源不一致时确定性拒绝；
- PostgreSQL 保存对话、消息、Run、Step、Checkpoint、审批、事件、定时任务、显式用户记忆和幂等记录；
- Alembic 管理 `assistant_*` 表的版本，启动脚本会先执行 `upgrade head`；
- Worker 使用数据库租约并行认领任务，默认全局并发 4、单用户并发 1，拒绝旧 Worker 覆盖新状态；
- Creator 创建采用持久化 `WAITING_DEPENDENCY`，等待时释放 Worker；用户取消会传播到
  Creator 并撤销本地租约，迟到结果无法覆盖取消状态；
- 所有外部写入先进入 `PREPARED → IN_FLIGHT → COMPLETED/UNKNOWN/FAILED` 副作用账本，未知结果使用原幂等键恢复；
- 暂时性网络、超时、限流和 5xx 错误采用有预算的指数退避，参数错误与权限错误不盲目重试；
- 每个 Run 固化模型、工具、重规划和总时长预算，避免无限循环及失控成本；
- 每个 Run 固化模型、Prompt 协议和 Tool Schema 指纹；部署升级后，已有副作用的旧
  Run 不会在新协议下盲目续跑；
- 发送给模型的最近对话、工具证据和帖子正文都有独立确定性预算，完整结果仍保存在
  PostgreSQL，不因模型上下文压缩而丢失审计证据；
- `publish_now` 必须经过绑定 `run version + plan hash + input hash` 的人工确认，旧审批不能批准新计划；
- 2—10 篇批量定时发布会先逐篇创作和审核，再把全部草稿 ID、SHA-256、首发时间与
  间隔合并为一次审批；确认后生成可分别取消的定时任务；
- “删除我的所有帖子”使用 Java 当前用户身份分页枚举，而不是公开搜索；执行器绑定
  完整本人帖子 ID 清单、忽略模型生成的 ID，并将最多 1000 篇软删除合并为一次审批；
  Java 以 20 篇为一个授权分块幂等执行，清单不完整时拒绝删除；
- SSE 实时传输运行事件并降低空闲数据库查询频率；不支持流式传输的代理环境自动退化为轮询；
- Run 记录排队、模型、工具、外部依赖等待与端到端耗时，聚合指标可定位主要延迟来源；
- 评论区 `@助手` 的结果由 Java 系统账号写入正式评论表，并保留 run 与来源帖子哈希；
- 长期记忆只能由用户显式写入、查看和删除，Agent 不会静默“记住”敏感信息；
- Java 工具网关采用“服务身份 + 用户委托 Capability”双重校验，Capability 限定动作、资源、Run、期限和使用次数；
- 定时发布不保存长期用户 Token，只保存加密的、仅可发布指定草稿的限次 Capability（当前最多提前约 6 天）；
- 用户取消定时任务时会先撤销 Java Capability，再提交 `CANCELLED` 状态，作为可逆步骤的补偿动作；
- Creator 产物的 SHA-256 会贯穿草稿、审批、定时任务和 Java 发布；草稿被编辑后，
  原审批或定时发布自动失效，避免发布用户未确认的新内容；
- 定时执行的每一次认领都有独立 attempt 记录；只对超时、限流、连接错误和 5xx
  重试，权限、参数与内容版本错误直接终止；
- Creator Agent 负责真实内容创作，Java 后端仍是帖子、权限和发布状态的唯一事实源；
- 所有模型调用必须使用真实 DeepSeek，缺少 Key 时服务直接拒绝启动。

## 本地启动

先从根目录启动中间件、Java 后端和 Creator Agent，然后：

```powershell
.\scripts\start-assistant.ps1
```

默认地址：`http://127.0.0.1:8094`，健康检查：

```text
GET /actuator/health
```

## 关键流程

`用户消息 → durable run → Supervisor plan → typed I/O tools → side-effect ledger → step events → final response`

完整控制回路：

`Observe → Plan → Act → Checkpoint → Verify → (Complete | Replan | Fail)`

社区运营：

`Intent Agent → Planner DAG → AnalyticsAgent + UserInsightAgent → ContentCreationAgent → ModerationAgent → Human Approval → PublishAgent`

定时创作发布：

`Creator AUTO task → FINAL_CONTENT → Java AI_ASSISTED draft → scoped capability → scheduled action → publish`

立即发布：

`Creator draft → WAITING_APPROVAL → 用户确认版本与草稿 → Java 幂等发布`

评论区：

`用户正式评论 → Assistant run(context_comment_id) → Java 系统助手正式回复 → provenance`

## 开发与生产进程

本地默认 `ASSISTANT_PROCESS_ROLE=all`，一个终端同时运行 API、Run Worker 和
Scheduler Worker，调试最方便。部署时可拆成三个进程：

```powershell
$env:ASSISTANT_PROCESS_ROLE="api"
python run_service.py

$env:ASSISTANT_PROCESS_ROLE="run-worker"
python run_worker.py

$env:ASSISTANT_PROCESS_ROLE="scheduler-worker"
python run_worker.py
```

三种进程共享 PostgreSQL 状态，不依赖进程内队列。

## 评测

确定性契约测试不调用模型：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

需要消耗真实 DeepSeek 配额的规划评测：

```powershell
.\.venv\Scripts\python.exe evals\run_planner_eval.py
```

场景覆盖只检索、上下文总结、创作、定时发布、立即发布审批、社区运营和禁止无关副作用。
输出 Intent Accuracy、Task Coverage、Planning Efficiency、Tool Selection Accuracy
和 Agent Selection Accuracy。

定时任务的逐次执行记录：

```text
GET /api/v1/assistant/scheduled-actions/{action_id}/attempts
```

编排与长任务接口：

```text
GET  /api/v1/assistant/agents
GET  /api/v1/assistant/runs/{run_id}/graph
POST /api/v1/assistant/runs/{run_id}/interrupt
POST /api/v1/assistant/runs/{run_id}/resume
POST /api/v1/assistant/runs/{run_id}/retry
GET  /api/v1/assistant/mcp/tools
```

## 生产化参考

- [Restate AI examples](https://github.com/restatedev/ai-examples)：借鉴崩溃安全的模型/工具调用和人工确认边界；
- [Restate](https://github.com/restatedev/restate)：借鉴 durable execution、稳定幂等重放和完成步骤不重复执行；
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)：借鉴类型化工具结果、handoff 与 guardrail 的职责拆分；
- [Cedar](https://github.com/cedar-policy/cedar)：借鉴默认拒绝以及 principal/action/resource/context 的授权建模。

本轮还对照了本机只读参考项目 Pico 与 nanobot：从 Pico 借鉴运行身份指纹和恢复时
freshness 校验；从 nanobot 借鉴上下文预算、最近历史裁剪、定时执行记录与故障恢复。
没有照搬它们面向文件系统/通用聊天机器人的能力，也没有采用自动“梦境记忆”，因为
社区助手的长期记忆必须由用户显式管理。

本项目没有增加新的常驻工作流中间件或策略服务。LangGraph 只负责编译和表达动态
DAG，生产状态仍落在已有 PostgreSQL、Java JWT 与确定性代码中，以控制本机启动压力。
# Four-layer memory

The assistant now separates working state, bounded conversation context, durable
task episodes, and semantic recall. PostgreSQL is authoritative and Qdrant is a
rebuildable per-tenant/per-user index. Completed non-sensitive tasks are
consolidated without another LLM call; trivial `ANSWER` runs and requests
containing credentials or identity data are skipped.

Recalled memory is injected into Intent, Planner, and Answer as untrusted
evidence. It never grants permission or proves that a historical side effect ran
in the current task. Users can disable episodic or semantic memory and delete
individual episodes or the entire history. Qdrant failures degrade to
PostgreSQL lexical recall and do not fail the main agent run.

Relevant endpoints:

```text
GET    /api/v1/assistant/memory/settings
PUT    /api/v1/assistant/memory/settings
GET    /api/v1/assistant/memory/episodes
DELETE /api/v1/assistant/memory/episodes/{episode_id}
DELETE /api/v1/assistant/memory/episodes
```
