# GreenBook Creator Agent Analysis

## 1. 定位

Creator Agent (`creator-agent`) 是一个**独立的 AI 内容创作服务**。它拥有完整的研究 → 策略 → 写作 → 批评 → 评估的创作管线，支持人机协作审批和多轮修改。

---

## 2. 项目结构

```
creator-agent/
├── app/
│   ├── main.py                          # FastAPI 入口
│   ├── api/routes.py                    # /actuator/health
│   ├── core/config.py                   # 180+ 配置项
│   │
│   └── creator/
│       ├── agents/
│       │   ├── specialists.py           # 7 个 specialist agent
│       │   ├── gateway.py               # 模型路由
│       │   └── schemas.py               # 结构化 IO 类型
│       │
│       ├── runtime/
│       │   ├── graph.py                 # LangGraph StateGraph
│       │   ├── supervisor.py            # CreatorSupervisorAgent (确定性)
│       │   ├── runtime.py               # LangGraphCreatorRuntime
│       │   └── checkpoints.py           # 检查点 (sqlite/memory/pg)
│       │
│       ├── application/
│       │   ├── harness.py               # CreatorAgentHarness (持久化)
│       │   └── ports.py                 # 协议接口
│       │
│       ├── domain/
│       │   └── models.py                # Task, Run, HumanDecision
│       │
│       ├── api/
│       │   ├── routes.py                # /api/v1/creator/* (14 端点)
│       │   ├── service.py               # CreatorApiService
│       │   ├── identity.py              # OIDC + Trusted Proxy
│       │   └── dispatcher.py            # 进程内执行分发
│       │
│       ├── memory/                      # 三层记忆 (Redis + PG + Qdrant)
│       ├── retrieval/                   # Agentic RAG (multi-round)
│       ├── tools/                       # 9 个 Creator Tools
│       ├── evaluation/                  # 评估框架 (11 指标)
│       ├── drafts/                      # 版本化草稿
│       ├── studio/                      # 创作者工作区
│       ├── publication/                 # 发布移交
│       ├── providers/                   # Java 数据提供者
│       └── worker/                      # Outbox Worker
│
├── mcp_tools/creator_server.py          # FastMCP 服务器
├── migrations/                          # Alembic 迁移 (4 versions)
├── tests/                               # 13 测试文件
└── scripts/                             # 启动脚本
```

---

## 3. 7 个 Specialist Agent

| Agent | Capability | 职责 | Tools |
|-------|-----------|------|-------|
| MemoryAgent | LOAD_MEMORY | 加载创作者画像、历史、参与度、语义记忆 | get_creator_profile, get_user_history |
| ContentAnalyzerAgent | ANALYZE_CONTENT | 分析创作者人设、过往帖子、互动信号 | — |
| StrategyAgent | BUILD_STRATEGY | 从画像+分析+研究生成内容策略和主题选项 | — |
| ResearchAgent | RESEARCH_TOPIC | Agentic RAG，生成带证据的 Evidence Pack | search_posts, get_post_detail, get_comments |
| WriterAgent | WRITE_DRAFT | 生成 DraftDocument (含引用、证据、不支持声明) | save_draft, update_draft |
| CriticAgent | CRITIQUE_CONTENT | 审查事实/结构/风格/质量，打分+修改建议 | — |
| EvaluationAgent | EVALUATE_CONTENT | 无参考的运行时评估 (faithfulness/relevance/style) | — |

**模型路由**: 不同 agent 使用不同模型配置 (analysis / writer / critic / assist)。

---

## 4. LangGraph Supervisor Loop

```
supervise → Send(execute_agent) → supervise │
                                            │
Terminal: await_human / finalize / fail     │
                                            └──────── loop

Supervisor 是确定性代码 (不是 LLM planner):
  - 读取 task kind
  - 读取已产生的 ArtifactRefs
  - 读取最近 agent 结果
  - 读取 budget counter
  - 动态计算下一步 PlanSnapshot/PlanSteps
```

### 创作流程 (CREATE_CONTENT)

```
Creator Goal
  │
  ├─ MemoryAgent + ContentAnalyzerAgent + ResearchAgent
  │   └─ → StrategyAgent → Topic Options
  │                           │
  │                    (Human Topic Selection)  ← HITL gate
  │                           │
  ├─ Content Outline
  │                    (Human Outline Approval) ← HITL gate
  │                           │
  ├─ WriterAgent → Draft
  │       │
  │       ├─ CriticAgent → 修改建议
  │       │       │
  │       │       └─ WriterAgent → Revised Draft (最多 4 轮)
  │       │
  │       └─ EvaluationAgent → RuntimeEvaluationSummary
  │
  └─ finalize → FINAL_CONTENT artifact
```

### 质量门控

```
SupervisorPolicy:
  target_quality_threshold: 0.70        # 达到发布标准
  minimum_publishable_threshold: 0.60   # 最低可发布
  max_replans: 4                        # 最多重规划次数
  max_writer_revisions: 4               # 最多修改轮次

quality_score >= 0.70 → 通过, finalize
0.60 <= quality_score < 0.70 → 降级标记
quality_score < 0.60 → 不发布
```

---

## 5. 如何接收任务和返回结果

### 接收任务 (3 个入口)

1. **HTTP API**: `POST /api/v1/creator/tasks`
   - Idempotency-Key 头
   - `CreatorAgentHarness.create_task(command)` 原子创建 Task + Run + Event + Outbox

2. **MCP Server**: `app/mcp_tools/creator_server.py`
   - FastMCP stdio/streamable-http
   - 受限工具集 (8 read + 2 write; publish_post 不暴露)

3. **Outbox Worker**: `CreatorOutboxWorker`
   - `FOR UPDATE SKIP LOCKED` 领取消息
   - 获取 Run Lease
   - 调用 Runtime

### 返回结果

- Terminal `RuntimeOutcome` (COMPLETED/FAILED/NEEDS_INPUT)
- 投影回 Task/Run 状态
- `final_artifact_id` (FINAL_CONTENT)
- 完整事件流 → SSE (支持 Last-Event-ID 重放)

### 两种执行模式

| 模式 | 配置 | 行为 |
|------|------|------|
| local | `CREATOR_API_EXECUTION_MODE=local` | API 进程内执行 |
| outbox | `CREATOR_API_EXECUTION_MODE=outbox` | API 只做身份/命令/查询，Worker 异步执行 |

---

## 6. 内容生成管线

### Research (研究)

```
AgenticRAG:
  plan (intent + channels + queries)
    → parallel SQL + Qdrant search
    → dedupe
    → SQL hydrate (权威加载，tenant ACL)
    → weighted fusion (BM25 + vector + business + RRF + recency + affinity + authority)
    → rerank
    → grade
    → rewrite (最多 2 轮)

未通过 SQL 验证的外部索引结果不进入 Evidence Pack
```

### Strategy (策略)

```
CreatorMemoryService.load() (三层并行):
  Redis short-term ─┐
  SQL long-term    ─┼→ MemoryBundle
  Qdrant semantic  ─┘

UsedContentAngle ledger:
  Jaccard ≥ 0.72 → conflict (避免重复选题)
```

### Writing (写作)

```
WriterAgent:
  结构化输出: DraftDocument
    title, body_markdown, citations[], evidence_ids[]
    unsupported_claims[] (不支持的主张，不隐藏)

_ground_draft_citations():
  重新验证每个 citation 对应的 evidence
  无法验证的 → 移除
```

### Critique (批评)

```
CriticAgent:
  CritiqueDocument
    overall_score: 0.0-1.0
    fact_verdicts[]: 每个事实主张的判决
    structure_score
    style_score
    revision_suggestions[]
```

### Evaluation (评估)

```
CreatorRuntimeContextEvaluator (确定性 judge):
  faithfulness: claim 级别词法忠实度 ≥ 0.42
  relevance: concept-coverage 相关性
  style: 风格检查

可选: OpenAICompatibleGenerationJudge (LLM judge)
```

---

## 7. Artifact 系统

```
不可变产物:
  creator_artifacts (id, task, run, step_id, kind, revision, content_json, content_sha256)

Artifact 类型:
  TOPIC_OPTIONS → EVIDENCE_PACK → CONTENT_OUTLINE
  → DRAFT → CRITIQUE → FINAL_CONTENT

每步产出一个新 revision (不可变)
同一 (run, step, kind, revision) → 不可覆写
```

---

## 8. Human-in-the-Loop

### 决策类型

- TOPIC_SELECTION: 选择主题
- OUTLINE_APPROVAL: 确认大纲
- DRAFT_REVIEW: 审查草稿

### 决策操作

- SELECT: 选择候选
- APPROVE: 批准
- REQUEST_CHANGES: 要求修改 (附带编辑)
- EDIT: 直接编辑

### 协作模式

| 模式 | 行为 |
|------|------|
| ADAPTIVE | 高置信度主题/大纲自动推进；最终草稿始终需确认 |
| GUIDED | 所有门手动确认 |
| AUTO | 质量门内全自动 (低风险批量任务) |

---

## 9. Creator 与 Main Agent 如何通信？

### 调用方向

```
Main Agent System
  │
  ├─ packages/creator_client/CreatorClient
  │   └─ HTTP POST /api/v1/creator/tasks (Idempotency-Key)
  │       → Task API
  │
  └─ MCP content.py handlers
      └─ ctx.creator.create_task() + wait_for_completion() + get_artifact()
```

### 身份传递

- **OIDC**: 生产环境，非对称 JWT (JWKS)
- **Trusted Proxy**: Java Gateway HMAC 签名 (X-Zhiguang-* headers)
- **Basic Auth**: 本地开发

### Publication Handoff

```
Creator 完成 → CreatorPublicationHandoffService
  ├─ 保存本地 AI_ASSISTED 草稿
  └─ POST {java}/api/v1/knowposts/ai-drafts
      Header: X-Creator-Handoff-Secret
      Body: AiDraftCreateRequest
```

---

## 10. 为什么 Creator 独立？

1. **业务边界明确**: Creator 只做内容创作——研究、策略、写作、批评、评估。没有聊天、咨询、风险评估。

2. **可靠性需求不同**: Creator 是长时间运行的多步任务（研究→策略→写作→多轮修改），需要：持久化控制面 (Task/Run/Event/Outbox)，LangGraph checkpoint，Lease 恢复，独立扩缩容。

3. **安全边界独立**: 联邦身份（OIDC + Trusted Proxy），Tool 授权（agent 级 allowlist），独立的安全策略。

4. **运维独立**: 独立的 PostgreSQL/Redis/Qdrant，独立的迁移，独立的 Docker 部署。可单独扩缩容。

5. **Human-in-the-Loop 是持久化状态**: 创作者审批（主题、大纲、草稿）可能跨越数小时甚至数天，不能绑定单个 API 请求的生命周期。

6. **评估自成体系**: 11 个评估指标，离线回归，基线对比，快照评估。嵌入式 Agent 不需要这个评估负担。
