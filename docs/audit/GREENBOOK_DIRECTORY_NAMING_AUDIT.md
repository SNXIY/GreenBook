# GreenBook 目录与命名现状审计报告

生成时间：2026-08-13
审计方式：全量扫描（git grep / find / 配置读取），**READ ONLY，未做任何修改**

---

## 1. Executive Summary

GreenBook 已完成从"Assistant 时代"到"Agent Runtime"的概念迁移，**代码层的 Assistant→Agent 收敛已基本完成**（前端已无 AssistantPanel/assistantService，API 已无 `/api/v1/assistant*` 路由），但存在两类历史命名残留：

1. **品牌残留（zhiguang）**：全项目 317 处（非二进制，docs 143 + 源码约 170）。集中在 Java 后端（主类/artifact/JWT/DB）、MySQL 库名、docker 容器名、前端目录名与 localStorage key、Creator 的 `source_system` 持久化字段、服务间 HMAC 协议头。
2. **概念双轨（run vs execution）**：API 层同时暴露 `/runs/{run_id}` 与 `/executions/{execution_id}` 两套 ID 体系，靠 `RunExecutionAdapter` 兼容层映射，前端只消费 run_id。这是**改名之外的独立议题**（API 契约收敛）。

**Python 侧命名已完全统一**：10 个包的目录名 = import 名 = distribution 名（全部 `greenbook-*`），无需改动。

**结论**：目录结构已经可以冻结（无新建/移动需求）；zhiguang 命名建议按"用户可见 > 持久化 > 公开契约 > 内部"的优先级分阶段收敛，其中约 60% 属于安全改名，约 30% 需要数据迁移或兼容保留，约 10% 建议删除。

---

## 2. Current Directory Tree（真实现状，3-4 层）

```
green-book/
├── .github/workflows/verify.yml        # CI：java / frontend / agent-runtime / creator-service
├── .env.example                        # 权威环境变量模板（111 GREENBOOK_*，4 ZHIGUANG_*）
├── PROJECT_CONTEXT.md                  # 当前架构权威速览
├── README.md
├── pyproject.toml                      # uv 工作区（10 members）
├── docker-compose.yml                  # 仅基础设施（MySQL/Kafka/PG/Redis/Qdrant）
├── uv.lock
├── apps/
│   ├── agent_api/greenbook_agent_api/
│   │   ├── main.py                     # FastAPI 入口 :8094
│   │   ├── api/                        # routes.py + runtime_routes.py
│   │   ├── dependencies/
│   │   ├── models/
│   │   ├── services/                   # runtime_agent_service.py 等
│   │   └── streaming/
│   ├── agent_worker/greenbook_agent_worker/
│   │   └── main.py                     # 队列消费进程
│   └── backend/
│       ├── pom.xml                     # artifactId=zhiguang
│       ├── db/                         # schema.sql + 迁移 SQL
│       ├── Dockerfile
│       ├── docs/
│       └── src/main/java/com/tongji/
│           ├── agentfacade/            # Python Agent 的稳定门面
│           ├── auth/                   # JWT/JWKS
│           ├── comment/ counter/ knowpost/ notification/ profile/
│           ├── relation/ storage/ user/ llm/ cache/ common/ config/
│           └── ZhiGuangApplication.java
├── packages/
│   ├── agent_core/greenbook_agent_core/
│   │   ├── agent/                      # loop/selector/state/actions/recovery
│   │   ├── command/                    # interpreter/target/models
│   │   ├── goal/                       # decomposer/compiler/models
│   │   ├── planning/                   # contracts/graph/dynamic
│   │   ├── execution/                  # worker/queue/persistence/state_manager/retry_*/runtime/
│   │   ├── capability/ toolruntime/ task/ artifact/ memory/ context/
│   │   ├── conversation/ human/ db/ observability/ compatibility/
│   │   └── runtime/container.py        # 组合根
│   ├── contracts/greenbook_contracts/  # tool_contract.py 等
│   ├── java_client/greenbook_java_client/
│   ├── creator_client/greenbook_creator_client/
│   ├── security/greenbook_security/    # policy.py + jwt/jwks
│   ├── evaluation/greenbook_evaluation/
│   └── observability/greenbook_observability/  # 空壳（1 行）
├── services/
│   └── greenbook_mcp/greenbook_mcp_server/
│       ├── server.py tool_registry.py tool_schemas.py context.py
│       └── tools/                      # community/content/publication/interaction/analytics
├── creator-agent/                      # 独立 uv 项目（pyproject name=greenbook-creator-service）
│   ├── app/
│   │   ├── main.py                     # :8092
│   │   ├── api/ core/ mcp_tools/ static/
│   │   └── creator/
│   │       ├── agents/ drafts/ evaluation/ memory/ providers/ publication/
│   │       ├── retrieval/ runtime/ studio/ tools/ worker/
│   │       ├── api/ application/ deployment/ domain/ infrastructure/
│   ├── migrations/versions/            # Alembic
│   ├── tests/ scripts/ frontend/ docs/
│   └── Dockerfile
├── zhiguang-fe/                        # 前端（package name=zhiguang-fe）
│   ├── src/
│   │   ├── components/{agent,cards,comments,common,content,icons,layout}
│   │   ├── context/AuthContext.tsx     # localStorage: zhiguang_auth_tokens
│   │   ├── features/auth
│   │   ├── pages/                      # AiCreate/TaskCenter/ManualCreate/Login/Register 等 13 页
│   │   ├── services/                   # agentService/executionService/creatorTaskService 等 12 个
│   │   ├── theme/ types/
│   │   └── App.tsx
│   ├── vite.config.ts                  # /api→8080 /creator-api→8092 /agent-api→8094
│   ├── index.html                      # <title>GREEN-BOOK · 让知识自然生长</title>
│   └── tests/
├── contracts/
│   ├── agent-openapi.yaml              # Agent API v2.0.0（/api/v1/agent/...）
│   └── java-openapi.yaml               # Java Agent Facade（22.5KB）
├── tests/
│   ├── unit/ (77) contract/ (9) integration/ (3) e2e/ (2)
│   ├── evaluation/ (1) compat/history/ (1)
│   └── plan_factory.py
├── scripts/
│   ├── start-*.ps1 (be/creator/agent/agent-worker/fe/greenbook)
│   ├── dev-up.ps1 setup-dev.ps1 verify-all.ps1 smoke-test.ps1 e2e-test.ps1
│   ├── ensure-jwt-keys.ps1 rotate-dev-secrets.ps1 run_p0_e2e.py
│   ├── ops/promote-admin.ps1 verify/ (空)
│   └── dev/ (空)
├── docs/                               # 195 个 .md
│   ├── architecture/ (57) development/ migration/ evaluation/ integration/
│   ├── progress/ project-understanding/ reports/ demo/ archive/ design-system/
├── infra/                              # README + postgres/01-create-databases.sql
├── design-system/                      # 纯设计规范文档（greenbook-zhiguang/ 残留）
├── archive/                            # 废弃代码（无引用）
└── zhiguang-be/                        # 遗留空目录（4 个空 .sql 目录）
```

---

## 3. Module Responsibilities（真实代码证据）

| 目录 | 职责（证据来源） |
|---|---|
| `apps/agent_api` | Agent HTTP API（FastAPI :8094）。Conversation/Approval 入口（`api/routes.py` 前缀 `/api/v1/agent`）、执行控制台（`api/runtime_routes.py` 前缀 `/api/v1`）、意图理解→AgentLoop→执行提交（`main.py` lifespan 装配、`services/runtime_agent_service.py`） |
| `apps/agent_worker` | Durable queue consumer（`main.py`：claim `execution_queue_message` → `RuntimeExecutionQueueHandler` → ExecutionWorker） |
| `apps/backend` | GreenBook Community Java backend（Spring Boot :8080）。社区数据/身份/发帖/评论/关注/通知（`com.tongji.*` 各 domain 包）+ AgentFacade（Python Agent 的稳定业务门面）+ JWT 签发/JWKS（`auth/`） |
| `packages/agent_core` | Agent Runtime 核心库（24,621 行）。意图→目标→决策→执行全链路 + 持久化/重试/租约/记忆/审批 |
| `packages/contracts` | 共享契约：`ToolMetadata`/`ToolPolicyMetadata`/`TOOL_POLICY_CATALOG`/identity/events |
| `packages/java_client` | Java Agent Facade 的类型安全异步客户端（对照 `contracts/java-openapi.yaml`） |
| `packages/creator_client` | Creator Task API 客户端（默认 :8092） |
| `packages/security` | 安全策略投影 + JWT/JWKS 验证（fail-closed） |
| `packages/evaluation` | Agent 评估框架（EvalCase/Runner/MetricsCalculator/BadCase） |
| `packages/observability` | 空壳（1 行 docstring，仅声明 opentelemetry-api） |
| `services/greenbook_mcp` | 进程内 MCP 工具运行时（17 个工具注册于 `tool_registry.py`） |
| `creator-agent` | Creator Service（FastAPI :8092）。研究/写作/质量/产物/人工决策管线（`app/creator/`），发布交接至 Java，MCP stdio 服务 |
| `zhiguang-fe` | 前端 SPA（React 18 + Vite :5173） |
| `contracts/` | OpenAPI 契约（agent + java） |
| `tests/` | unit/contract/integration/e2e/evaluation/compat 分层测试 |
| `scripts/` | Windows PowerShell 启动/验证/运维脚本 |
| `docs/` | 架构/开发/迁移/评估文档 |
| `infra/` | 基础设施说明 + PG 建库脚本 |
| `design-system/` | 设计规范文档（非代码） |
| `archive/` | 废弃代码（`creator/creator_agent/`、旧 workflows，零引用） |
| `zhiguang-be/` | **遗留空目录**（仅 4 个空 `.sql` 目录，无代码） |

---

## 4. Brand Naming Inventory（全项目词频）

| Name | Count (files) | Main locations | Meaning |
|---|----:|---|---|
| greenbook（小写） | 329 | packages/apps/services 全部 Python 包、目录名 | 内部命名/包名，已统一 |
| GreenBook | 176 | README、PROJECT_CONTEXT、前端文案、docs、Java `spring.application.name=greenbook-community` | 品牌名，用户可见 |
| GREENBOOK（环境变量前缀） | 62（108 个变量） | `.env.example`、settings 代码、docker-compose、scripts | Agent Runtime 配置命名空间 |
| zhiguang / Zhiguang / ZHIGUANG | 88 文件 / **317 处命中** | Java 后端、MySQL、compose、前端 localStorage、Creator `source_system`、HMAC 头 | 历史产品名"智光"，见 §5 |
| 智光（中文） | **0** | — | 源码/文档中无中文品牌残留 |
| mindflow | 40 | docker-compose（`mindflow_creator` 库/用户/密码）、Creator 配置 | 历史基础设施账号名 |
| MindFlow | 13 | docs、creator-agent 文档/注释 | 同上，历史文档 |
| MindBridge | 1 | `docs/architecture/CREATOR_BOUNDARY.md` | 仅历史文档提及 |
| assistant / Assistant / ASSISTANT | 316 文件 | DB 表（7 张）、消息 role、Java boolean、docs | 历史概念，见 §6 |

---

## 5. Zhiguang Inventory（全量 317 处分类）

### 5.1 HTTP Header / 服务间协议（REQUIRES_MIGRATION / RETAIN_COMPATIBILITY）

| 当前名称 | 位置 | 职责 | 是否用户可见 | 是否持久化 | 公开契约 | 分类 |
|---|---|---|---|---|---|---|
| `X-Zhiguang-Service/User-Id/Roles/Timestamp/Nonce/Signature` | 生产：`creator-agent/app/creator/providers/java.py:311-317`；消费：`creator-agent/app/creator/api/identity.py:170-176`；测试：`creator-agent/tests/test_creator_identity.py:301-307` | Creator→Java 网关的 HMAC 服务间签名协议 | 否 | 否 | **是**（Java 网关验签） | REQUIRES_MIGRATION（双端+网关同步，见 §13 三阶段方案） |

### 5.2 Java 代码（apps/backend）

| 当前名称 | 位置 | 分类 | 建议目标 |
|---|---|---|---|
| 主类 `ZhiGuangApplication` | `src/main/java/com/tongji/ZhiGuangApplication.java:7,10`；pom.xml:220 | SAFE_RENAME | `GreenBookCommunityApplication` |
| `artifactId=zhiguang`、`name=zhiguang` | pom.xml:15,17 | SAFE_RENAME | `greenbook-backend` |
| Dockerfile jar `zhiguang-1.0-SNAPSHOT.jar` | Dockerfile:13 | SAFE_RENAME | 随 artifact 改 |
| JWT `issuer=zhiguang`、`key-id=zhiguang-key` | application.yml:98-99；AuthProperties.java:31,37 | **RETAIN_COMPATIBILITY**（已签发 token 的外部验证方） | 新 issuer 需双签发过渡 |
| JWT audience `zhiguang-api` | JwtService.java:64；AuthConfiguration.java:62 | **RETAIN_COMPATIBILITY**（Agent/Creator/前端三端校验） | 三端同步后改 |
| `tenant_id=zhiguang` claim | JwtService.java:74；application.yml:98 | **RETAIN_COMPATIBILITY**（已入 token，Creator 侧断言 tenant） | 双值过渡 |
| MySQL 库名 `zhiguang` | application.yml:5、application-docker.yml:3 | REQUIRES_MIGRATION（存量数据） | 见 §11 |
| Canal filter `zhiguang\.outbox` | application.yml:66 | REQUIRES_MIGRATION（topic 名） | `greenbook\.outbox` |
| 头像 URL `static.zhiguang.cn` | AuthService.java:122 | RETAIN（外部 CDN 域名） | 域名可用则不动 |
| DB 角色 `'zhiguang-assistant'` | mapper/UserMapper.xml:141 | REQUIRES_MIGRATION（种子数据） | `greenbook-agent` |
| schema 注释 "ZhiGuang" | db/schema.sql:1、V1__baseline.sql:1 | SAFE_RENAME（注释） | GreenBook |
| 测试 JWT 常量 | JwtValidationTest.java:46-182（issuer `zhiguang-test`） | SAFE_RENAME | 随 issuer 改 |
| 本机绝对路径 | Create1000UsersAndTokensTest.java:71,96 | **REMOVE** | 删除测试或改相对路径 |

### 5.3 环境变量（4 个 ZHIGUANG_*）

| 变量 | 位置 | 分类 |
|---|---|---|
| `ZHIGUANG_MYSQL_HOST_PORT=33306` | .env.example:21、compose:16、start-be.ps1:24 | RETAIN_COMPATIBILITY（运维契约；改名破坏现有 .env） |
| `ZHIGUANG_KAFKA_HOST_PORT=39092` | .env.example:22、compose:49 | 同上 |
| `ZHIGUANG_MYSQL_PASSWORD` | .env.example:27、compose:18 | 同上 |
| `ZHIGUANG_MYSQL_DB=zhiguang` | .env.example:28 | 同上（值与库名联动） |

另有值（非前缀）：`GREENBOOK_CREATOR_*_TENANT_ID=zhiguang`、`TRUSTED_PROXY_ALLOWED_SERVICE=zhiguang-java-backend`（creator-agent/.env.example:55-56、config.py:116-117）、`CREATOR_UI_ZHIGUANG_TOKEN`（scripts/verify-creator-ui.mjs:6）。

### 5.4 数据库 / 中间件（REQUIRES_MIGRATION）

| 项 | 位置 | 说明 |
|---|---|---|
| 容器 `zhiguang-mysql` / `zhiguang-kafka` + volumes | docker-compose.yml:11,35,106-107 | volume 持久化，改名丢数据引用 |
| `MYSQL_DATABASE: zhiguang` | docker-compose.yml:19 | 存量数据 |
| Kafka topic `zhiguang\.outbox` | application.yml:66 | canal 消费 |

### 5.5 前端 zhiguang-fe

| 项 | 位置 | 分类 | 建议目标 |
|---|---|---|---|
| 目录名 `zhiguang-fe` | CI verify.yml:35,42、scripts×5、tests/contract/test_runtime_devex.py:110、README/PROJECT_CONTEXT | SAFE_RENAME（10+ 文件联动） | `greenbook-web`（见问题 4） |
| `package.json` name | zhiguang-fe/package.json:2 | SAFE_RENAME | `greenbook-web` |
| localStorage `zhiguang_auth_tokens` | context/AuthContext.tsx:39 | SAFE_RENAME（注意清旧 key） | `greenbook_auth_tokens` |
| localStorage `zhiguang_current_user` | AuthContext.tsx:40 | 同上 | `greenbook_current_user` |
| localStorage `zhiguang_manual_create_autosave` | pages/ManualCreatePage.tsx:29 | 同上 | `greenbook_manual_create_autosave` |
| URL fragment `#zhiguang_token=` | pages/AiCreatePage.tsx:41、creator-agent/app/static/creator.js:8、verify-creator-ui.mjs:87 | **RETAIN_COMPATIBILITY**（FE↔CreatorUI 公开握手） | 双写过渡 |
| 事件名 `zhiguang:notification-unread-changed` | services/notificationEvents.ts:1 | SAFE_RENAME | `greenbook:notification-unread-changed` |

### 5.6 creator-agent（61 处）

| 项 | 位置 | 分类 | 建议目标 |
|---|---|---|---|
| `source_system="zhiguang"`（**写入 DB 的持久化字段**） | providers/models.py:40,60,145,166、memory/models.py:118、retrieval/models.py:135 | **REQUIRES_MIGRATION**（存量行） | 数据回填后 `greenbook` |
| `source_system="zhiguang-search"` | providers/java.py:430 | 同上 | `greenbook-search` |
| `backend_name="zhiguang-java"` | providers/java.py:40 | SAFE_RENAME（错误文案） | `greenbook-java` |
| 发布对接 "Zhiguang Java ai-drafts API" | publication/service.py:35-196 | SAFE_RENAME（文案） | GreenBook Java |
| 身份断言方 "Zhiguang Java gateway" | identity.py:151、config.py:114 | SAFE_RENAME（文案） | GreenBook Java gateway |
| UI `source:"zhiguang"` | app/static/creator.js:13 | SAFE_RENAME | greenbook |

### 5.7 文档 / 测试 / 设计系统

| 项 | 位置 | 分类 |
|---|---|---|
| docs/ 下 143 处 | 迁移记录、容器名、localStorage key 描述 | HISTORICAL_DOC（保留） |
| `design-system/greenbook-zhiguang/` | design-system/greenbook-zhiguang/MASTER.md:9 | REMOVE 或 HISTORICAL_DOC |
| 契约测试 tenant/audience 断言 | tests/contract/test_agent_api_authentication.py:61,74,87 | RETAIN（与 JWT 联动，随 JWT 迁移改） |

---

## 6. Assistant Legacy Inventory

| Current | Location | Current meaning | Proposed | Classification |
|---|---|---|---|---|
| `assistant_conversations` | db/repositories.py:23 | 会话表（SQLAlchemy） | `agent_conversations` | **DB_COMPAT**（有存量数据，需迁移） |
| `assistant_messages` | db/repositories.py:46 | 消息表 | `agent_messages` | DB_COMPAT |
| `assistant_runs` | db/repositories.py:61 | 运行表（**run_id 体系的真身**） | `agent_runs` | DB_COMPAT（run_id 兼容层依赖） |
| `assistant_approvals` | db/repositories.py:114 | 审批表 | `agent_approvals` | DB_COMPAT |
| `assistant_execution_result_projections` | execution/result_projection.py:70 | 结果投影表 | `execution_result_projections` | DB_COMPAT |
| `assistant_schema_migrations` | db/migration_runner.py:12,25 | 迁移登记表 | — | 保留（迁移基础设施） |
| `assistant_response` 字段 | result_projection.py:29,82 | 结果字段 | — | 可改（内部） |
| message `role=="assistant"` | routes.py:255,284,293 等 | **LLM 消息角色协议**（OpenAI 语义） | 保留 | REMOVE（不可改，协议字段） |
| Java `assistant` boolean | CommentRow.java:21、CommentResponse.java:18、AgentFacadeService.java:319 | "AI 生成"标记 | 保留 | REMOVE（语义字段非概念） |
| `/api/v1/assistant*` 路由 | —（已不存在，V5 迁移退役） | — | — | 已清理 |
| OpenAPI summary "Assistant health" | contracts/agent-openapi.yaml:186 | 契约文案 | "Agent health" | PUBLIC_COMPAT |
| `tests/integration/test_assistant_runs_projection_migration.py` | tests/integration/ | 投影迁移测试 | 改名+内容同步 | SAFE_RENAME |
| creator.html `assistant-pane/openAssistantPane` | creator-agent/app/static/creator.html:188,270-285 | Creator UI 内部命名 | agent-* | SAFE_RENAME |
| docs/architecture 30+ 文件 | PHASE_11_5_D_ASSISTANT_RUNS_PROJECTION.md 等 | 历史迁移记录 | 保留 | HISTORICAL_DOC |

**关键结论**：7 张 `assistant_*` 表全部仍有代码读写（非死表），改名必须走数据迁移 + 双写兼容；消息 `role` 与 Java `assistant` boolean 是协议/语义字段，**不应改**。

---

## 7. Python Naming（目录 = import = distribution，全部一致 ✅）

| Directory | Import name | Distribution | Status |
|---|---|---|---|
| packages/agent_core | `greenbook_agent_core` | `greenbook-agent-core` | ✅ 一致 |
| packages/contracts | `greenbook_contracts` | `greenbook-contracts` | ✅ |
| packages/security | `greenbook_security` | `greenbook-security` | ✅ |
| packages/java_client | `greenbook_java_client` | `greenbook-java-client` | ✅ |
| packages/creator_client | `greenbook_creator_client` | `greenbook-creator-client` | ✅ |
| packages/evaluation | `greenbook_evaluation` | `greenbook-evaluation` | ✅ |
| packages/observability | `greenbook_observability` | `greenbook-observability` | ✅ |
| services/greenbook_mcp | `greenbook_mcp_server` | `greenbook-mcp-server` | ✅ |
| apps/agent_api | `greenbook_agent_api` | `greenbook-agent-api` | ✅ |
| apps/agent_worker | `greenbook_agent_worker` | `greenbook-agent-worker` | ✅ |
| **creator-agent** | `greenbook_creator_service` | `greenbook-creator-service` | ⚠️ **目录名 `creator-agent` 与包名/发行名不一致**（独立 uv 项目，不在根 workspace） |

---

## 8. Java Naming（apps/backend）

| 项 | 值 | 归属 |
|---|---|---|
| groupId | `org.example` | **占位符**（未定公司域，非学校/产品） |
| artifactId / name | `zhiguang` | 产品品牌（旧） |
| 主类 | `com.tongji.ZhiGuangApplication` | 品牌（旧）+ 学校包 |
| base package | `com.tongji.*` | **同济大学拼音**（学校历史包名） |
| spring.application.name | `greenbook-community` | ✅ 已是 GreenBook |
| DB 库名 | `zhiguang` | 产品品牌（旧） |

`com.tongji` 属于**学校/历史包兼容**。Java base package 改名成本极高：全量文件移动 + import 重写 + 反射/MyBatis XML 路径 + 序列化兼容；且无运行时收益（不暴露给用户）。**建议 DO_NOT_RENAME**，仅新代码可逐步迁入新包（如需）。

---

## 9. Frontend Naming（zhiguang-fe）

| 类别 | 现状 | 分类 |
|---|---|---|
| 页面（13 个） | AiCreatePage、TaskCenterPage、CreateHubPage、ManualCreatePage、LoginPage、RegisterPage、ProfilePage、HomePage 等 | ✅ 全 GreenBook 语义（Task Center = 任务中心） |
| Agent 组件 | `components/agent/AgentPanel.tsx`、AgentActivityCards、AgentResultCards | ✅ 无 AssistantPanel 残留 |
| Service | `agentService.ts`（runs API）、`executionService.ts`、`creatorTaskService.ts` 等 12 个 | ✅ 命名一致 |
| 品牌文案 | 登录/注册/侧边栏全 "GreenBook"；index.html `<title>GREEN-BOOK · 让知识自然生长</title>` | ✅ 已统一 |
| localStorage | `zhiguang_auth_tokens`、`zhiguang_current_user`、`zhiguang_manual_create_autosave` | ⚠️ 与品牌割裂（SAFE_RENAME） |
| URL fragment | `#zhiguang_token=` | ⚠️ 外部握手契约（RETAIN） |
| API 前缀 | `/api`（Java）、`/creator-api`（Creator）、`/agent-api`（Agent） | ✅ 无品牌名 |

---

## 10. API Naming

| API | Owner | 状态 | 说明 |
|---|---|---|---|
| `/api/v1/agent/conversations*`、`/runs*`、`/approvals*` | agent_api `routes.py`（前缀 `/api/v1/agent`） | ✅ 现行 | 无 assistant 残留 |
| `/api/v1/executions*`、`/timeline`、`/stream`、`/pause|resume|cancel` | agent_api `runtime_routes.py`（前缀 `/api/v1`） | ✅ 现行 | 执行控制台 |
| `/api/v1/agent`（Java 门面） | apps/backend AgentFacadeController | ✅ 现行 | 与 Python Agent 路径同名，跨服务 |
| `/api/v1/knowposts/ai-drafts` | Java（发布交接） | ✅ | Creator 发布落点 |
| `/api/v1/assistant-tools` | — | ✅ **已退役**（V5 迁移删除） | 历史 |

**run_id / execution_id / assistant_runs 的真实关系**：
- `assistant_runs` 表**仍然存在且活跃**（`db/repositories.py:61`），是 run_id 的存储真身
- API 层**双路径并存**：`/runs/{run_id}`（routes.py:862，主对话流）与 `/executions/{execution_id}`（runtime_routes.py:292，控制台）
- 兼容层：`RunExecutionAdapter`（`compatibility/history/run_execution_link.py:49`）将 legacy run_id 解析为 canonical execution_id；持久化表 `run_execution_link`
- `RunAcceptedResponse` 同时返回 `run_id` + `execution_id`；前端 `agentService.ts` 只消费 run_id
- **混合点**：`/executions/{execution_id}/approve`（routes.py:1048）execution 风格路由嵌在主 router 内

---

## 11. Database Naming

| 存储 | 名称 | 分类 |
|---|---|---|
| PostgreSQL 库名 | `mindflow_creator` | **REQUIRES_MIGRATION**（物理库名；应用内可配置，改名 = 备份/重建/迁移数据，收益低，建议保留） |
| PG 用户/密码 | `mindflow` / `mindflow` | 同上（保留） |
| MySQL 库名 | `zhiguang` | REQUIRES_MIGRATION（见 §5.4；`MYSQL_DB` 可配置，改名需导数据） |
| PG 表（execution 域） | `execution/execution_control/execution_step/execution_event/checkpoint/execution_lease/external_operation/retry_task/execution_queue_message` | ✅ 命名健康 |
| PG 表（会话域） | `assistant_conversations/messages/runs/approvals` | DB_COMPAT（§6，需迁移） |
| PG 表（投影） | `assistant_execution_result_projections` | DB_COMPAT |
| PG 表（兼容层） | `run_execution_link` | 保留（run_id 兼容功能） |
| PG 表（产物/记忆） | `artifact_record/artifact_event/agent_memories` | ✅ 命名健康 |
| 迁移基础设施 | `assistant_schema_migrations` | 保留 |

**原则**：物理库名（mindflow_creator/zhiguang）只通过配置暴露，不暴露给用户，改名收益低、风险高（存量数据+多应用依赖）——**建议保留**；表名 `assistant_*` 改名收益中等（开发心智），需迁移+双写。

---

## 12. Config Naming（.env.example 变量前缀分布）

| 前缀 | 数量 | 分类 | 说明 |
|---|---|---|---|
| `GREENBOOK_*` | 108 | KEEP | Agent Runtime 配置，已统一 |
| `VITE_*` | 5 | KEEP | 前端构建变量 |
| `MYSQL_*` / `REDIS_*` / `KAFKA_*` / `QDRANT_*` / `OSS_*` / `JWT_*` | 19 | KEEP | 基础设施（通用名，无品牌） |
| `DEEPSEEK_*` / `AI_*` / `CANAL_*` / `STORAGE_*` / `DEFAULT_*` | 7 | KEEP | 外部服务 |
| `ZHIGUANG_*` | 4 | RETAIN_COMPATIBILITY（运维契约） | 见 §5.3 |
| `ASSISTANT_*` | 0 | — | 已全部移除（仅 docs 历史提及） |
| `GREENBOOK_CREATOR_*_TENANT_ID=zhiguang`（值） | 2 | RETAIN（值带品牌） | creator-agent/.env.example:55-56 |
| `CREATOR_UI_ZHIGUANG_TOKEN` | 1 | SAFE_RENAME | scripts/verify-creator-ui.mjs:6 |

---

## 13. Protocol / Header Naming

### `X-Zhiguang-*`（6 个 header）

| Header | 生产者 | 消费者 | 公开协议 | 持久化 | 改名兼容 |
|---|---|---|---|---|---|
| `X-Zhiguang-Service` | creator providers/java.py:311-317 | identity.py:170-176 | 是（Java 网关验签） | 否 | 需双写 |
| `X-Zhiguang-User-Id` | 同上 | 同上 | 是 | 否 | 需双写 |
| `X-Zhiguang-Roles` | 同上 | 同上 | 是 | 否 | 需双写 |
| `X-Zhiguang-Timestamp` | 同上 | 同上 | 是（防重放） | 否 | 需双写 |
| `X-Zhiguang-Nonce` | 同上 | 同上 | 是 | 否 | 需双写 |
| `X-Zhiguang-Signature` | 同上 | 同上 | 是（HMAC） | 否 | 需双写 |

**注意**：Java 侧源码中**无**这些 header 字符串的硬编码（验签在 Java 网关/中间件层，可能由部署网关处理）——已确认 `apps/backend` 无引用，需与 Java 网关部署方确认。

**三阶段迁移建议（本次不执行）**：
- **Phase 1**：Producer 双发 `X-Zhiguang-*` + `X-GreenBook-*`；Consumer 双收
- **Phase 2**：Producer 只发 `X-GreenBook-*`；Consumer 只收 `X-GreenBook-*`
- **Phase 3**：删除旧别名代码与文档引用

---

## 14. Documentation Naming

| 品牌 | docs 命中 | 分类 |
|---|---|---|
| GreenBook | 大量（architecture/development 主文档） | CURRENT_AUTHORITY（当前权威文档，已用新品牌） |
| zhiguang | 143 处 | HISTORICAL（迁移记录、容器名、旧目录描述；保留） |
| assistant | 30+ 文件 | HISTORICAL（Phase 迁移报告；保留） |
| 智光 | 0 | — |
| MindFlow | 13 | HISTORICAL（基础设施账号历史名；保留） |
| MindBridge | 1（CREATOR_BOUNDARY.md） | HISTORICAL（单次提及；保留） |

**原则**：历史 Phase 报告记录当时的真实命名，**不重写**；仅 CURRENT_AUTHORITY 文档（README、PROJECT_CONTEXT、CONFIGURATION、CURRENT_ARCHITECTURE）在完成改名后同步。

---

## 15. Naming Conflicts（同义重复清单）

| 同义词组 | 位置 | 语义 | 建议 |
|---|---|---|---|
| **Run vs Execution** | `/runs/{run_id}`（routes.py:862）vs `/executions/{execution_id}`（runtime_routes.py:292）；`RunExecutionAdapter`（compatibility/history/run_execution_link.py:49）；前端只用 run_id | 同一对象双 ID：Run=遗留用户可见 ID，Execution=规范执行状态 | 短期保留兼容层；中期前端切到 execution_id 后逐步下线 `/runs/*` |
| **AgentRunResult vs RuntimeResult** | `agent/actions.py:67` vs `execution/runtime_result.py:10` | 同义结果 envelope（legacy/runtime 兼容），后者统一含 artifacts | 收敛为 RuntimeResult |
| **TOOL_CALL vs ToolInvocationContext/InvocationResult** | `agent/actions.py:16-22` vs `execution/runtime/invocation_context.py:15`、`tool_runtime.py:51` | 同一次工具调用的动作级与运行时级命名，词根不一 | 统一词根（tool call / invocation 二选一） |
| assistant role / assistant boolean | routes.py:255 等、CommentRow.java:21 | LLM 消息角色、AI 生成标记（非概念实体） | **不改**（协议/语义字段） |
| Job vs Task | 无 job（仅 docs 历史 json） | — | 无冲突 |
| Plan vs Graph | `planning/graph.py`（PlanGraph）vs `planning/contracts.py`（TaskPlan） | graph=依赖图、plan=步骤计划，分层清晰 | 无冲突 |

---

## 16. Proposed GreenBook Naming Standard

### Brand
```
GreenBook（唯一品牌）
```

### Product
```
GreenBook Community   （原 zhiguang 社区）
GreenBook Agent       （Agent Runtime）
GreenBook Creator     （Creator Service）
GreenBook Task Center （前端任务中心）
```

### Apps
```
apps/agent_api      apps/agent_worker      apps/backend（建议 app 名 greenbook-community）
```

### Python（保持现状，全部一致 ✅）
```
greenbook_agent_api / greenbook_agent_worker / greenbook_agent_core / greenbook_contracts
greenbook_java_client / greenbook_creator_client / greenbook_security / greenbook_evaluation
greenbook_mcp_server / greenbook_creator_service
```

### Core Domain Terms（统一）
```
Command → Goal → GoalTree → Task → TaskNode → Plan → PlanGraph
ExecutionInput → Execution → StepExecution → AgentLoop
Tool / ToolMetadata / ToolRuntime → MCP
Artifact / Approval / Schedule / Conversation / Context / Memory
```

### 命名规则建议
1. **用户可见**（UI/API 响应）：只用 GreenBook
2. **环境变量**：`GREENBOOK_*`（唯一命名空间）
3. **数据库**：表名去 `assistant_` 前缀（迁移后），库名保留（物理名）
4. **服务间协议**：`X-GreenBook-*`（三阶段迁移）
5. **内部代码**：`agent_*` / `execution_*`，不再引入新 zhiguang/assistant 命名

---

## 17. Rename Risk Matrix

| Current name | Proposed name | Area | Risk | Action |
|---|---|---|---|---|
| `zhiguang-fe` 目录 | `greenbook-web` | 前端目录 | MEDIUM | RENAME_WITH_COMPAT（10+ 引用联动：CI/scripts/tests/docs） |
| `package.json` name=zhiguang-fe | greenbook-web | 前端 | LOW | RENAME_NOW |
| localStorage `zhiguang_auth_tokens` 等 3 个 | `greenbook_*` | 前端 | LOW | RENAME_WITH_COMPAT（迁移旧 key 一次） |
| `#zhiguang_token=` fragment | `#greenbook_token=` | FE↔CreatorUI | HIGH | KEEP_LEGACY（双写后下线） |
| `zhiguang:notification-unread-changed` | `greenbook:...` | 前端事件 | LOW | RENAME_NOW（内部事件） |
| `X-Zhiguang-*` header | `X-GreenBook-*` | 服务间协议 | HIGH | RENAME_WITH_COMPAT（三阶段） |
| Java `ZhiGuangApplication` | `GreenBookCommunityApplication` | Java | LOW | RENAME_NOW |
| `artifactId=zhiguang` | `greenbook-backend` | Java 构建 | LOW | RENAME_NOW |
| JWT issuer/audience/tenant | greenbook 系 | 认证契约 | HIGH | KEEP_LEGACY（双签发过渡） |
| MySQL 库名 `zhiguang` | greenbook | 数据库 | HIGH | KEEP_LEGACY（物理名，可配置；改名需导库） |
| `ZHIGUANG_*` env（4 个） | GREENBOOK_* | 配置 | MEDIUM | KEEP_LEGACY（别名过渡） |
| `zhiguang-mysql`/`zhiguang-kafka` 容器 | greenbook-* | Docker | MEDIUM | RENAME_WITH_COMPAT（compose 重创建，volume 保留） |
| Kafka topic `zhiguang\.outbox` | `greenbook\.outbox` | 消息 | MEDIUM | RENAME_WITH_COMPAT（双 topic） |
| `source_system="zhiguang"`（DB 值） | greenbook | Creator 持久化 | MEDIUM | RENAME_WITH_COMPAT（存量回填） |
| `assistant_*` 表（7 张） | `agent_*` | 数据库 | MEDIUM | RENAME_WITH_COMPAT（迁移+双写，run 兼容层联动） |
| `run_id` API 路径 | execution_id | API 契约 | HIGH | KEEP_LEGACY（兼容层；前端迁移后下线） |
| `com.tongji.*` | com.greenbook.* | Java 包 | **HIGH（成本极高）** | **DO_NOT_RENAME**（新代码可逐步迁入） |
| `mindflow_creator` 库 | greenbook_creator | 数据库 | HIGH | KEEP_LEGACY（物理名） |
| `org.example` groupId | 真实域 | Java | LOW | RENAME_NOW（正式化） |
| `creator-agent` 目录 | greenbook-creator | Python 项目 | LOW | RENAME_WITH_COMPAT（独立 uv 项目，pyproject 已用 greenbook-creator-service） |
| `zhiguang-be/` 空目录 | — | 目录 | LOW | DELETE |
| `design-system/greenbook-zhiguang/` | — | 文档 | LOW | DELETE 或归档 |
| `Create1000UsersAndTokensTest` 绝对路径 | — | 测试 | LOW | DELETE |
| `spring.application.name=greenbook-community` | — | Java | ✅ 已符合 | KEEP |
| docs 历史 zhiguang/assistant | — | 文档 | — | DOC_ONLY（保留） |

---

## 18. Proposed Final Directory Tree（建议目标，当前不执行）

```
green-book/
├── apps/
│   ├── agent_api/              # 不变
│   ├── agent_worker/           # 不变
│   └── backend/                # 不变（application.name 已是 greenbook-community）
├── packages/                   # 全部不变（已统一 greenbook-*）
├── services/greenbook_mcp/     # 不变
├── creator-agent/              # 建议改名 greenbook-creator/（或保留目录，标注对齐）
├── web/                        # ← 从 zhiguang-fe 更名
│   └── （原 zhiguang-fe 全部内容）
├── contracts/ tests/ scripts/ docs/ infra/   # 不变
├── zhiguang-be/                # ← 删除（空目录）
└── archive/ design-system/     # 保留（design-system/greenbook-zhiguang 归档）
```

---

## 19. Recommended Migration Order（建议顺序，本次不执行）

1. **Phase A — 零风险清理**（0.5 天）：删除 `zhiguang-be/` 空目录、测试本机绝对路径、`design-system/greenbook-zhiguang` 归档；Java 主类改名 + artifactId；schema 注释
2. **Phase B — 前端收敛**（1-2 天）：localStorage key 迁移（双读旧 key→写新 key→删旧）、事件名、`zhiguang-fe` 目录更名（CI/scripts/tests/docs 联动）
3. **Phase C — 服务间协议**（3-5 天）：`X-Zhiguang-*` → `X-GreenBook-*` 三阶段（双发→单发→删旧），需 Java 网关配合
4. **Phase D — 持久化**（1 周+，独立排期）：`assistant_*` 表迁移（需与 run_id 兼容层、schema_guard、V5 迁移联动）；`source_system` 回填；Kafka topic 双写
5. **Phase E — API 契约收敛**（独立议题）：前端切 execution_id → 下线 `/runs/*` → 移除 RunExecutionAdapter（如评估后确定不再需要）
6. **DO_NOT_RENAME**：`com.tongji`、`mindflow_creator` 库名、MySQL 库名、JWT claims（存量）、`ZHIGUANG_*` 环境变量（别名过渡）

---

## 20. Final Answers

### 1. 当前项目里还有多少 Zhiguang / zhiguang / 智光？

**317 处命中**（非二进制文件，大小写不敏感），分布在 88 个文件。其中 **docs/ 143 处**（历史记录），**纯源码约 170 处**：creator-agent 61（其中 `source_system` 等持久化字段）、apps/backend/src 33、scripts+CI 25、前端 zhiguang-fe 约 15、tests 若干。**中文"智光"为 0 处**（源码与文档均无中文品牌残留）。

### 2. 哪些可以直接改成 GreenBook？

- Java：主类 `ZhiGuangApplication`、artifactId、Dockerfile jar 名、schema 注释、JwksController javadoc
- 前端：localStorage key（3 个，双读迁移）、事件名、package.json name、`zhiguang-fe` 目录（10+ 引用联动）
- Creator：错误文案、`backend_name="zhiguang-java"`（文案部分）、发布对接文案
- 测试：JWT 测试常量（随 issuer 同步）、测试文件名
- 共约 **60% 属于直接可改**（不涉及存量数据与外部契约）

### 3. 哪些不能直接改？为什么？

| 不能直接改 | 原因 |
|---|---|
| `X-Zhiguang-*` header | Java 网关 HMAC 验签，外部契约；Java 侧无源码引用说明验签在部署网关，需对方配合 |
| JWT issuer/audience/tenant claim | 已签发 token 被三端（Agent/Creator/前端）校验，`tenant_id=zhiguang` 已被 Creator 断言 |
| `#zhiguang_token=` fragment | FE↔CreatorUI 公开握手协议，双端硬编码 |
| MySQL 库名 `zhiguang`、`mindflow_creator` 库名 | 存量数据 + 多应用依赖；只通过配置暴露，不暴露给用户 |
| `assistant_*` 7 张表 | 全部活跃读写（非死表），run_id 兼容层依赖 `assistant_runs` |
| `ZHIGUANG_*` 4 个环境变量 | 运维契约，改名破坏现有 .env/compose |
| `zhiguang-mysql`/`zhiguang-kafka` 容器与 volume | volume 持久化数据，改名丢引用 |
| `source_system="zhiguang"` DB 值 | 存量行，需回填 |
| `run_id` API | 前端全部消费 run_id；兼容层 `run_execution_link` 表在 |

### 4. zhiguang-fe 是否值得改成 greenbook-web？

**值得，但属于中等风险**。改动范围：目录名 + `package.json` name + CI（verify.yml:35,42）+ 5 个 scripts + `tests/contract/test_runtime_devex.py:110` + README/PROJECT_CONTEXT + `.env.example` 注释，共 **10+ 处引用联动**。前端代码内部（import 路径）不涉及品牌名，安全。建议与 Phase B 一起做，git mv + 一次性改引用。

### 5. X-Zhiguang-* 是否值得改成 X-GreenBook-*？怎样兼容？

**值得**（品牌统一），但要**三阶段**：Phase 1 双发双收（旧 header 继续验签）→ Phase 2 只发新 header → Phase 3 删旧。关键前提：**Java 侧源码无 header 字符串引用**（验签在 Java 网关/部署层），必须先与 Java 网关负责人确认验签位置，否则 Agent/Creator 双端改完而网关不认新头，服务直接不可用。

### 6. com.tongji 是否应该改？

**不建议改**。理由：① base package 改名 = 全量文件移动 + import 重写 + MyBatis XML mapper 路径 + 潜在序列化影响，工作量 2-3 天纯机械改动，无用户可见收益；② `tongji`（同济）是学校历史包名，不属于用户可见品牌。**结论：DO_NOT_RENAME**；如团队坚持，可只对新增模块使用 `com.greenbook.*` 渐进迁移，不整体替换。

### 7. mindflow_creator 是否应该改？

**不建议改**。物理库名只通过 `GREENBOOK_AGENT_DATABASE_URL`/`POSTGRES_DB` 配置暴露，不暴露给用户；改名需要备份→建新库→迁移数据→改所有连接串，风险高收益低。**建议 KEEP_LEGACY**。同理 MySQL 库名 `zhiguang`。可接受的最低成本方案：保留库名，只在文档与配置注释中标注 "database: mindflow_creator (GreenBook runtime)"。

### 8. assistant_runs / run_id 是否应该改？

分两层：
- **表名 `assistant_runs` → `agent_runs`**：可改但需迁移（表有存量数据，且 `db/schema_guard.py`、`result_projection`、兼容层均引用）；与另外 6 张 `assistant_*` 表一起走统一迁移，双写过渡。
- **`run_id` API 契约**：**当前不建议改**。前端 `agentService.ts` 全部消费 run_id，`RunAcceptedResponse` 双 ID 返回，`RunExecutionAdapter` 兼容层在运行。正确顺序：先让前端切到 execution_id（API 已有完整 `/executions/*` 端点）→ 验证无回归 → 下线 `/runs/*` → 再考虑移除兼容层与 `run_execution_link` 表。这是独立排期项。

### 9. 当前目录结构是否还需要调整？还是已经可以冻结？

**基本可以冻结**。Python 侧（apps/packages/services）已是最优结构，无需调整。唯一建议：① `zhiguang-fe` → `web/`（品牌统一）；② `creator-agent` 目录名与 pyproject 名（`greenbook-creator-service`）对齐；③ 删除 `zhiguang-be/` 空目录；④ `archive/` 保留但标记废弃。除这 4 项外**不建议任何结构性调整**。

### 10. 最终推荐的完整目录结构是什么？

见 §18。核心变化仅 3 处：`zhiguang-fe` → `web/`、`creator-agent` → `greenbook-creator/`（可选）、删除 `zhiguang-be/`。其余全部保持不变。

---

## 附录：数据源

- 词频统计：`git grep`（排除 .lock/.png/.jpg/.venv/node_modules）
- zhiguang 全量 317 处逐条核对（docs 143 / 源码 ~170）
- assistant 分布：`git grep -il assistant` 全量分类
- 包名：10 个 pyproject.toml 逐一核对
- 环境变量：`.env.example` 逐行分类（GREENBOOK 108 / ZHIGUANG 4 / 其他 31）
- 协议头：creator providers/java.py + identity.py + 测试
- 数据库：SQLAlchemy 表定义（repositories.py / persistence.py / artifact / memory / compatibility）+ schema.sql + Alembic 迁移
