# GreenBook

GreenBook is a community platform with one goal-driven Agent Runtime and a
durable execution layer. The Agent turns a conversation into a Goal/Task plan,
requests approval for protected actions, and reports progress and artifacts
through the web application. Content creation is assistant-first: the host LLM
writes drafts directly through the MCP tool runtime, which persists them to the
Java community backend.

## Current architecture

```text
zhiguang-fe
  -> Agent API (apps/agent_api)
  -> Command / Context / Goal / Task / AgentLoop
  -> Planning / ToolSelector / ToolPolicyGate
  -> ExecutionInput -> Queue / Agent Worker
  -> MCP-compatible in-process tool runtime
  -> Java Backend
```

Java owns community data, identity, drafts, and publication. The Agent Runtime
owns planning, tool selection, queueing, retries, checkpoints, leases,
idempotency, ledger/evidence, memory hooks, and recovery. See
[current architecture](docs/architecture/CURRENT_ARCHITECTURE.md) for the
ownership and contract boundaries.

## Repository layout

```text
apps/backend              Java Community Backend
apps/agent_api            GreenBook Agent API
apps/agent_worker         GreenBook Agent Worker
packages/agent_core       Agent Runtime core
packages/contracts        shared contracts and ToolMetadata policy
packages/java_client      Java API client
packages/evaluation       evaluation contracts and runners
packages/observability    metrics and tracing helpers
packages/security         security policy projections
services/greenbook_mcp    MCP-compatible in-process tool runtime
zhiguang-fe               frontend
contracts                 OpenAPI and shared YAML contracts
infra                     infrastructure notes; root compose is canonical
scripts                   local startup, checks, smoke tests
tests                     unit, contract, integration, E2E, and evaluation tests
docs                      current architecture, development, and migration docs
```

## Technology

- Python 3.12+, `uv`, FastAPI, Pydantic, SQLAlchemy, PostgreSQL, Redis, and Qdrant
- Java 21+/Spring Boot with MySQL, Redis, and Kafka integrations
- React, TypeScript, Vite, and CSS modules
- Docker Compose for local PostgreSQL, Redis, Qdrant, MySQL, and Kafka

## Local startup

Copy `.env.example` to `.env`, fill the required credentials, then run:

```powershell
docker compose up -d
uv sync
.\scripts\start-greenbook.ps1
```

The canonical ports are Java `8080`, Agent API `8094`, and frontend `5173`.
`services/greenbook_mcp` is imported in-process; it is not a separately deployed
MCP process.

## Agent capabilities

The active tool catalog covers community search and posts, draft creation,
scheduling and publication, comment replies, and analytics. The user-facing
concepts are Conversation, Task, Progress, Execution, Artifact, Approval, and
Schedule.

## Verification

```powershell
uv run pytest -q
uv run ruff check packages/agent_core apps/agent_api apps/agent_worker services/greenbook_mcp packages/contracts packages/security packages/java_client tests
cd apps/backend; mvn test
cd ..\..\zhiguang-fe; npm run lint; npm run build
```

See [local setup](docs/development/LOCAL_SETUP.md),
[configuration](docs/development/CONFIGURATION.md), and
[testing](docs/development/TESTING.md) for the complete workflow.

## 启动方式

1. 基础设施
```powershell
cd D:\agent\green-book
docker compose up -d
```

.\scripts\start-be.ps1        # Java 后端（:8080，通常已运行则跳过）
.\scripts\start-agent.ps1     # Agent API
.\scripts\start-fe.ps1        # 前端

