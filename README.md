# GreenBook

GreenBook is a community platform with one goal-driven Agent Runtime, a
durable execution layer, and a dedicated Creator Service. The Agent turns a
conversation into a Goal/Task plan, requests approval for protected actions,
and reports progress and artifacts through the web application.

## Current architecture

```text
zhiguang-fe
  -> Agent API (apps/agent_api)
  -> Command / Context / Goal / Task / AgentLoop
  -> Planning / ToolSelector / ToolPolicyGate
  -> ExecutionInput -> Queue / Agent Worker
  -> MCP-compatible in-process tool runtime
  -> Java Backend or Creator Service
```

Java owns community data and identity. Creator owns research, writing, quality,
artifacts, and its own human-in-the-loop workflow. Reliable Execution owns
queueing, retries, checkpoints, leases, idempotency, ledger/evidence, memory
hooks, and recovery. See [current architecture](docs/architecture/CURRENT_ARCHITECTURE.md)
for the ownership and contract boundaries.

## Repository layout

```text
apps/backend              Java Community Backend
apps/agent_api            GreenBook Agent API
apps/agent_worker         GreenBook Agent Worker
packages/agent_core       Agent Runtime core
packages/contracts        shared contracts and ToolMetadata policy
packages/java_client      Java API client
packages/creator_client   Creator Service client
packages/evaluation       evaluation contracts and runners
packages/observability    metrics and tracing helpers
packages/security         security policy projections
services/greenbook_mcp    MCP-compatible in-process tool runtime
creator-agent             GreenBook Creator Service
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

The canonical ports are Java `8080`, Creator Service `8092`, Agent API `8094`,
and frontend `5173`. `services/greenbook_mcp` is imported in-process; it is not
a separately deployed MCP process.

## Agent capabilities

The active tool catalog covers community search and posts, draft creation and
revision, scheduling and publication, comment replies, and analytics. The
user-facing concepts are Conversation, Task, Progress, Execution, Artifact,
Approval, and Schedule.

## Verification

```powershell
uv run pytest -q
uv run ruff check packages/agent_core apps/agent_api apps/agent_worker creator-agent/app
cd apps/backend; mvn test
cd ..\..\zhiguang-fe; npm run lint; npm run build
```

See [local setup](docs/development/LOCAL_SETUP.md),
[configuration](docs/development/CONFIGURATION.md), and
[testing](docs/development/TESTING.md) for the complete workflow.

## 启动方式
1. 基础设施
cd D:\agent\green-book
docker compose up -d
2. Java Backend
窗口二：
cd D:\agent\green-book
.\scripts\start-be.ps1
端口：8080
3. Creator Service
窗口三：
cd D:\agent\green-book
.\scripts\start-creator.ps1
端口：8092
4. Agent API
窗口四：
cd D:\agent\green-book
.\scripts\start-agent.ps1 -ApiOnly
端口：8094
5. Agent Worker
窗口五：
cd D:\agent\green-book
.\scripts\start-agent-worker.ps1
当前是 queue 模式，因此需要配置有效的：
GREENBOOK_AGENT_WORKER_ACCESS_TOKEN=...
6. Frontend
窗口六：
cd D:\agent\green-book
.\scripts\start-fe.ps1
访问：
http://127.0.0.1:5173
如果改成直连开发模式：
GREENBOOK_AGENT_EXECUTION_DISPATCH=direct
则不需要启动 Agent Worker