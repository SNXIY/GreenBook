# GreenBook 当前目录结构

## 顶层目录

```
green-book/
│
├── apps/                      # 可部署应用
├── packages/                  # 共享库
├── services/                  # 服务运行时
├── creator-agent/             # Creator 创作服务 (独立)
├── zhiguang-fe/               # 前端 React
├── zhiguang-be/               # 旧 DDL (历史)
├── contracts/                 # OpenAPI 契约
├── tests/                     # 测试 (100+ 文件)
├── evaluation/                # Phase15-F 运行时评估
├── docs/                      # 文档
├── scripts/                   # 开发/运维脚本
├── infra/                     # 基础设施 (SQL)
├── design-system/             # 设计系统
├── archive/                   # 历史代码存档
├── docker-compose.yml         # 开发环境编排
├── pyproject.toml             # Python workspace
└── uv.lock                    # 依赖锁定
```

---

## apps/ — 可部署应用

| 目录 | 语言 | 端口 | 职责 | 是否核心 |
|------|------|------|------|----------|
| `apps/agent_api` | Python/FastAPI | 8094 | Agent HTTP 入口 | ✓ |
| `apps/agent_worker` | Python | — | Agent 后台 Worker | ✓ |
| `apps/backend` | Java/Spring Boot | 8080 | 社区业务后端 | ✓ |

```
apps/
├── agent_api/
│   └── greenbook_agent_api/
│       ├── main.py                # FastAPI 入口 + lifespan
│       ├── api/routes.py          # /api/v1/agent/*
│       ├── api/runtime_routes.py  # /api/v1/* (执行状态)
│       └── services/              # 适配器 + 管线
├── agent_worker/
│   └── greenbook_agent_worker/
│       ├── main.py                # Worker 入口
│       └── execution_handler.py   # 重新导出
└── backend/
    ├── src/main/java/com/tongji/
    │   ├── agentfacade/           # Agent API 层
    │   ├── knowpost/              # 帖子
    │   ├── comment/               # 评论
    │   └── ...
    └── src/main/resources/
        ├── application.yml
        └── keys/
```

---

## packages/ — 共享库

| 目录 | 依赖 | 职责 | 是否核心 |
|------|------|------|----------|
| `packages/agent_core` | contracts, security | Agent 推理+执行核心 | ✓ |
| `packages/contracts` | — | 共享契约 (叶节点) | ✓ |
| `packages/security` | contracts | JWT + 安全 | ✓ |
| `packages/java_client` | contracts | Java HTTP 客户端 | ✓ |
| `packages/creator_client` | contracts | Creator HTTP 客户端 | ✓ |
| `packages/evaluation` | agent_core, mcp, java_client | 行为评估框架 | 辅助 |
| `packages/observability` | contracts | 空壳 (未实现) | 辅助 |

```
packages/
├── agent_core/
│   └── greenbook_agent_core/
│       ├── command/         # Command 理解
│       ├── agent/           # AgentLoop
│       ├── goal/            # Goal 分解
│       ├── planning/        # 动态规划
│       ├── task/            # Task 管理
│       ├── execution/       # 可靠执行 (38 文件)
│       ├── context/         # 上下文构建
│       ├── memory/          # 长期记忆
│       ├── toolruntime/     # Tool 策略
│       ├── capability/      # 能力目录
│       ├── artifact/        # 产物管理
│       ├── human/           # 人机交互
│       ├── conversation/    # 会话管理
│       ├── observability/   # 可观测性
│       ├── db/              # 数据库
│       ├── compatibility/   # 历史兼容
│       └── runtime/         # Container 组装
├── contracts/               # ToolResult, ToolPolicy, AuthContext
├── security/                # JWT 验证, AuthContextResolver, SecurityPolicy
├── java_client/             # JavaClient, 16 个端点方法
├── creator_client/          # CreatorClient, 4 个方法
├── evaluation/              # EvalCase, EvaluationRunner, 12 golden cases
└── observability/           # 空壳
```

---

## services/ — 服务运行时

| 目录 | 职责 | 是否核心 |
|------|------|----------|
| `services/greenbook_mcp` | MCP Tool Runtime (16 handlers) | ✓ |

```
services/
└── greenbook_mcp/
    └── greenbook_mcp_server/
        ├── server.py         # GreenBookMCPServer
        ├── context.py        # ToolContext
        ├── tool_registry.py  # 16 handler 注册
        ├── tool_schemas.py   # 15 Pydantic 参数模型
        └── tools/            # handler 实现
```

---

## creator-agent/ — Creator 服务

独立部署的内容创作服务。包含：7 个 specialist agent, LangGraph supervisor loop, Research→Strategy→Write→Critique→Evaluation 管线, Agentic RAG, 三层记忆, 评估框架, 创者工作区。

```
creator-agent/
├── app/
│   ├── main.py                  # FastAPI
│   ├── api/routes.py            # /api/v1/creator/*
│   ├── core/config.py           # 180+ settings
│   └── creator/
│       ├── agents/specialists.py  # 7 agents
│       ├── runtime/graph.py       # LangGraph StateGraph
│       ├── application/harness.py # 持久化控制面
│       ├── memory/                # 三层记忆
│       ├── retrieval/             # Agentic RAG
│       ├── evaluation/            # 11 指标评估
│       ├── drafts/                # 版本化草稿
│       └── studio/                # 创作者工作区
├── migrations/                    # Alembic
└── tests/                         # 13 测试文件
```

---

## zhiguang-fe/ — 前端

React/TypeScript/Vite 前端。端口 5173。

```
zhiguang-fe/
├── src/
│   ├── pages/          # 页面
│   ├── features/       # 业务模块
│   ├── components/     # 组件
│   ├── services/       # API 调用
│   └── context/        # 状态管理
└── dist/               # 构建产物
```

---

## zhiguang-be/ — 历史 DDL

旧数据库迁移脚本，不应修改。

---

## tests/ — 测试

100+ 个测试文件，5 个目录：

```
tests/
├── unit/        # 85 文件 — 单元测试 (fake LLM, in-memory/SQLite)
├── contract/    # 9 文件 — OpenAPI 契约, 认证, 错误分类
├── e2e/         # 3 文件 — 端到端 (mocked 客户端 + TestClient)
├── integration/ # 3 文件 — 集成测试
├── evaluation/  # 1 文件 — badcase 测试
└── compat/      # 1 文件 — 历史兼容
```

---

## docs/ — 文档

```
docs/
├── architecture/     # 90+ 文件 — 架构文档
├── development/      # 开发指南 (SETUP, TESTING, CONFIGURATION)
├── evaluation/       # 评估报告
├── migration/        # 20+ 文件 — 迁移记录
├── progress/         # 45+ 文件 — 阶段进度报告
├── archive/          # 历史文档
└── design-system/    # 设计系统文档
```

---

## contracts/ — API 契约

```
contracts/
├── agent-openapi.yaml  # Agent API v2.0.0
└── java-openapi.yaml   # Java Agent Facade v1.0.0
```

---

## scripts/ — 运维脚本

```
scripts/
├── start-greenbook.ps1    # 启动所有服务
├── start-agent.ps1        # 启动 Agent API
├── start-agent-worker.ps1 # 启动 Worker
├── verify-all.ps1         # 全量校验
├── e2e-test.ps1           # 端到端测试
├── check-runtime-status.ps1  # 健康检查
├── runtime-health-check   # Python 健康检查脚本
└── run_p0_e2e.py          # P0 E2E 测试
```
