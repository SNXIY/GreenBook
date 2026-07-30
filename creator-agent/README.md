# MindFlow Creator Intelligence Agent

MindFlow Creator 是一个面向知识社区创作者的可恢复、多 Agent 内容创作系统。
它不是一次性生成文章的 Prompt Demo，而是把分析、研究、策略、人工决策、写作、
评审和评估组织成可持久化的长任务。

当前仓库只包含 Creator 业务，不包含聊天、咨询、风险评估或后台报告系统。

与 GreenBook Java/React 项目的双路径创作、OIDC 身份、真实社区数据和发布溯源约定见
[GreenBook 集成说明](../docs/INTEGRATION.md)。

## 创作流程

```text
Creator Goal
    |
    +--> MemoryAgent -----------+
    +--> ContentAnalyzerAgent --+--> StrategyAgent --> Topic Options
    +--> ResearchAgent ---------+                         |
                                                          v
                                                 Human Topic Selection
                                                          |
                                                          v
                                                  Content Outline
                                                          |
                                                          v
                                                 Human Outline Approval
                                                          |
                                                          v
                                                     WriterAgent
                                                          |
                                                          v
                                                     CriticAgent
                                                   /             \
                                              revise               accept
                                                |                    |
                                                +---------------- EvaluationAgent
                                                                     |
                                                                     v
                                                               Final Content
```

Creator 工作台支持以下任务类型：

- `CREATE_CONTENT`：完整的选题、大纲、正文、评审与评估闭环。
- `RESEARCH_TOPIC`：生成经过授权回填的 Evidence Pack。
- `ANALYZE_CONTENT`：分析创作者画像、历史内容和互动信号。
- `BUILD_STRATEGY`：基于画像、内容分析和研究结果生成内容策略。
- `IMPROVE_DRAFT`：对已有草稿执行评审、修订和评估。

## 核心设计

### Harness 与控制面

`CreatorAgentHarness` 负责：

- 原子创建 Task、Run、Event、Outbox 和幂等记录。
- Worker lease、续租、过期接管和陈旧结果拒绝。
- 取消、自动重试和显式新 Run 重试。
- 输出事件数量、Payload 大小、租户范围和乐观版本校验。

### LangGraph Runtime

控制环保持为：

```text
supervise -> Send(execute_agent) -> supervise
```

`CreatorSupervisorAgent` 根据任务类型、当前 Artifact、执行结果和预算动态生成计划，
而不是把业务步骤硬编码为固定图边。图状态只传递紧凑引用，正文和分析结果写入
不可变 `creator_artifacts`。

### Human-in-the-loop

选题与大纲节点使用 LangGraph `interrupt()` 暂停。提交人工决定后，系统通过
Checkpoint 和 `Command(resume=...)` 恢复精确中断：

- 决策状态为 `PENDING -> SUBMITTED -> APPLIED`。
- 校验租户、创作者、Task version、Checkpoint、Interrupt 和候选项白名单。
- `REQUEST_CHANGES` 会产生新的计划与 Artifact revision。
- 重复提交通过幂等记录返回同一业务结果。

协作模式分为：

- `ADAPTIVE`：默认模式。高置信度选题和大纲自动推进，正文始终交给创作者确认。
- `GUIDED`：选题、大纲和正文都由创作者逐步确认。
- `AUTO`：在质量门禁和预算约束内自动完成，适合低风险批量任务。

### Creator Studio

工作台围绕创作者而不是 Agent 运行步骤组织：

- 左侧管理作品、长期项目和真实素材；任务可显式绑定项目与素材。
- 中间使用 Tiptap 编辑正文，保存不可变版本，并从任意版本创建实验分支。
- 右侧展示 AI 局部修改建议、可信引用、质量结果和创作者反馈。
- AI 修改先生成与当前正文版本绑定的差异建议；接受后才创建新版本，正文变化后旧建议
  自动失效。
- 定稿可派生社区长帖、系列短帖、邮件通讯和文章版本，不覆盖主稿。
- Agent 计划、事件和中断保留在运行详情中，不占用日常写作主界面。

项目、素材、建议、分支、渠道稿和反馈均按 `tenant_id + creator_id` 隔离并持久化。
素材只在创作者明确选择后进入模型上下文，并带有可追溯的 `material:{id}` 标识。

### Creator Memory

- Redis：带 TTL 和 CAS 版本的短期 Task/Run 快照。
- PostgreSQL：版本化 Creator Profile。
- Qdrant：按 `tenant_id + creator_id` 隔离的历史内容语义记忆。
- 单个外部存储故障会形成可审计降级，不改变 SQL/Checkpoint 事实源。

### Agentic RAG

检索循环为：

```text
plan -> retrieve -> SQL hydrate -> fuse -> rerank -> grade -> rewrite
```

- SQL 提供关键词候选。
- Qdrant 提供语义候选。
- SQL 提供业务分候选和最终 ACL/事实回填。
- 融合分包含 BM25、向量、业务分、RRF、时效性、创作者亲和度和来源权威度。
- 外部索引结果不能通过当前租户 SQL 回填时，不允许进入 Evidence Pack。

### Creator Tools 与 MCP

九个 Creator 工具共享同一套 Pydantic 校验、角色授权、Agent allowlist、超时、
结果大小限制和审计策略：

- `get_creator_profile`
- `get_user_history`
- `search_posts`
- `get_post_detail`
- `get_comments`
- `get_post_metrics`
- `get_engagement`
- `save_draft`
- `update_draft`

`publish_post` 当前不开放。外部写操作应在后续接入明确的人工确认边界。

### Evaluation

版本化评估覆盖：

- Retrieval：Recall@K、Precision@K、MRR、nDCG、ACL Safety。
- Agent：Task Success、Tool Calling Accuracy、Planning Quality。
- Generation：Faithfulness、Relevance、Style Consistency。

默认 Deterministic Judge 适合本地和 CI，也可以显式配置 OpenAI-compatible Judge。

## 目录结构

```text
app/
├── api/                     # 健康检查
├── core/                    # Creator 配置
├── creator/
│   ├── agents/              # 七类专业 Agent 与结构化模型网关
│   ├── api/                 # HTTP API、身份、SSE 和工作台服务
│   ├── application/         # Creator Harness
│   ├── deployment/          # Alembic 与 Checkpoint 迁移入口
│   ├── domain/              # Task、Run、Decision 领域模型
│   ├── drafts/              # 版本化草稿
│   ├── evaluation/          # Dataset、Metric、Judge、报告持久化
│   ├── infrastructure/      # SQLAlchemy、UoW、Artifact Store
│   ├── memory/              # Redis、SQL、Qdrant Memory
│   ├── providers/           # GreenBook Java Community Provider
│   ├── retrieval/           # Agentic RAG
│   ├── runtime/             # LangGraph、Supervisor、Checkpoint
│   ├── tools/               # Tool Gateway、审计和 MCP 适配
│   └── worker/              # Outbox Runtime Worker
├── mcp_tools/               # Creator MCP Server 入口
└── static/                  # Creator 登录页与工作台

migrations/                  # Creator Alembic migrations
tests/                       # Creator Runtime/API/Memory/RAG/MCP/Eval 测试
docs/                        # 架构与各阶段实现说明
```

## 本地启动

本地默认使用：

- SQLite Creator Database。
- SQLite LangGraph Checkpoint。
- Rule-based Creator Model Gateway。
- SQL 检索与模拟社区数据。
- 不要求 Redis、Qdrant 或外部模型。

```powershell
cd D:\agent\green-book
.\scripts\start-creator.ps1
```

打开 `http://127.0.0.1:8092/creator.html`。融合开发模式由 GreenBook 根启动脚本
加载统一 `.env`，连接 Docker 中的 PostgreSQL、Redis 和 Qdrant，并使用 Java
后端的 OIDC/JWKS。

Creator 单项目 Basic Auth 模式仍可用于独立开发。建议把以下配置写入 Creator
项目根目录的 `.env`，这样服务重启后仍然生效：

```dotenv
CREATOR_BASIC_USERNAME=creator
CREATOR_BASIC_PASSWORD=<local-development-password>
CREATOR_BASIC_CREATOR_ID=creator-local
CREATOR_BASIC_ACTOR_ID=creator-local
CREATOR_BASIC_DISPLAY_NAME=GreenBook Creator
CREATOR_LOCAL_AUTO_LOGIN=true
CREATOR_API_TENANT_ID=tenant-local
```

`CREATOR_LOCAL_AUTO_LOGIN=true` 只允许浏览器从 `127.0.0.1` 或 `::1` 获取本地
Basic 会话，不会把密码写入前端静态资源。主动点击“退出登录”后，当前标签页仍会停留
在登录页。

## 接入真实模型

Ollama：

```powershell
$env:AI_PROVIDER = "ollama"
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL = "qwen2.5:7b"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

DeepSeek：

```powershell
.\scripts\start-creator-deepseek.ps1 -Restart
```

启动脚本直接读取项目根目录 `.env` 中的 `DEEPSEEK_API_KEY` 和本地登录配置，
不需要每次在 PowerShell 中重新设置。DeepSeek 默认使用
`https://api.deepseek.com` 和 JSON Output。多 Agent 内容创作默认关闭思考模式，
以降低延迟和费用；需要更高推理强度时可改用 `deepseek-v4-pro` 并设置
`-Model deepseek-v4-pro -Thinking`。

同一提供商下可以为不同工作负载配置不同模型，未配置的角色继续使用提供商默认模型：

```dotenv
CREATOR_MODEL_ANALYSIS_MODEL=
CREATOR_MODEL_WRITER_MODEL=
CREATOR_MODEL_CRITIC_MODEL=
CREATOR_MODEL_ASSIST_MODEL=
```

`writer.*` 使用写作模型，`critic.*` 与 `evaluation.*` 使用评审模型，
`editor.*` 使用低延迟协作模型，其余分析、记忆、研究和策略操作使用分析模型。

OpenAI-compatible：

```powershell
$env:AI_PROVIDER = "openai"
$env:OPENAI_API_KEY = "<api-key>"
$env:OPENAI_MODEL = "<model-id>"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

真实模型返回不合法结构化 JSON 时，Runtime 会记录失败并进入 Harness 的失败/重试语义。
模型调用失败或返回不合法结构化 JSON 时，任务会明确失败并进入 Harness 的失败/重试语义，
不会返回模板内容或伪装成模型成功。

## HTTP API

API 前缀为 `/api/v1/creator`：

```text
GET    /status
POST   /tasks
GET    /tasks
GET    /tasks/{task_id}
POST   /tasks/{task_id}/cancel
POST   /tasks/{task_id}/retry
GET    /tasks/{task_id}/decisions
POST   /tasks/{task_id}/decisions/{decision_id}/responses
GET    /tasks/{task_id}/artifacts
GET    /tasks/{task_id}/events
POST   /tasks/{task_id}/drafts
GET    /drafts/{draft_id}
GET    /drafts/{draft_id}/versions
POST   /drafts/{draft_id}/versions
GET    /projects
POST   /projects
GET    /materials
POST   /materials
GET    /drafts/{draft_id}/suggestions
POST   /drafts/{draft_id}/suggestions
POST   /suggestions/{suggestion_id}/accept
POST   /suggestions/{suggestion_id}/reject
GET    /drafts/{draft_id}/branches
POST   /drafts/{draft_id}/branches
GET    /drafts/{draft_id}/channel-variants
POST   /drafts/{draft_id}/channel-variants
POST   /feedback
GET    /feedback/summary
```

写命令使用 `Idempotency-Key`；SSE 支持 `Last-Event-ID`、持久化重放、Heartbeat 和终态关闭。

## 可观测性

设置 `CREATOR_OTEL_ENABLED=true` 后启用 FastAPI 和模型调用 Trace。配置
`CREATOR_OTEL_EXPORTER_ENDPOINT` 可通过 OTLP/HTTP 接入 Jaeger、Tempo 或其他
OpenTelemetry Collector。Span 记录操作名、模型、预算、Token 估算和错误状态，不导出
Prompt、正文、素材或模型响应：

```dotenv
CREATOR_OTEL_ENABLED=true
CREATOR_OTEL_SERVICE_NAME=mindflow-creator
CREATOR_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces
CREATOR_OTEL_EXPORTER_HEADERS=
```

## Docker Compose

默认拓扑：

- `creator-postgres`
- `redis`
- `qdrant`
- `creator-migrate`
- `app`
- `creator-worker`

可选 Profile：

- `creator-mcp`：Streamable HTTP MCP Server。
- `creator-eval`：一次性评估任务。

```bash
docker compose up -d --build
docker compose ps
```

Compose 中 API 使用 `outbox` 模式，只负责身份、命令、查询和 SSE；模型、Memory、
RAG、Checkpoint 和 LangGraph 只在 `creator-worker` 中打开。Worker 使用
PostgreSQL `FOR UPDATE SKIP LOCKED`、owner lease、Heartbeat、指数退避和 DEAD 状态。

横向扩展：

```bash
docker compose up -d --scale creator-worker=3
```

生产环境必须：

- 将 `CREATOR_IDENTITY_MODE` 设置为 `oidc`。
- 通过 Secret Manager 注入数据库、模型和 MCP 凭据。
- 在 TLS/OIDC 网关后暴露 API。
- 使用真实 Embedding 和 Community Provider。
- 关闭开发用模拟数据 Seed。

## MCP

本地 stdio：

```bash
python -m app.mcp_tools.creator_server
```

Streamable HTTP 必须配置 Bearer Token、Host allowlist 和 Origin allowlist：

```dotenv
CREATOR_MCP_TRANSPORT=streamable-http
CREATOR_MCP_BEARER_TOKEN=<service-token>
CREATOR_MCP_ALLOWED_HOSTS=creator.example.com
CREATOR_MCP_ALLOWED_ORIGINS=https://creator.example.com
```

## Evaluation

```bash
python -m app.creator.evaluation.cli \
  --candidate-name mindflow-creator \
  --candidate-version development \
  --fail-on-threshold
```

报告默认输出到：

```text
target/creator-evaluation-report.json
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖 Harness、Multi-Agent Runtime、Adaptive Human-in-the-loop、Memory、
Agentic RAG、项目素材、局部建议、版本分支、渠道改写、引用约束、模型路由、MCP
Tools、OIDC、API/SSE、Migration、Outbox Worker 和 Evaluation。

## 当前边界

- 社区数据只来自 GreenBook Java 服务，缺少连接参数时服务拒绝启动。
- AI 生成与 AI 辅助内容经过 Creator Critic/Evaluation，不进入独立内容审核流程。
- Critic 负责事实、结构、风格和内容质量，不承担政策合规审核。
- 最终发布仍由外部系统负责；GreenBook 集成通过签名交接创建带 Artifact Lineage 的
  `AI_ASSISTED` 草稿，再由用户在现有编辑器确认发布。
- 当前素材支持文本、链接摘录和本地文本文件；网页抓取、PDF/Office 解析与向量化摄取
  仍需接入独立的异步解析管线。
- 下一阶段重点是 Java 历史内容与互动指标、发布后效果回流、真实模型预发布验收和
  多服务 Trace 关联。
