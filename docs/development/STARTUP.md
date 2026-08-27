# GreenBook Startup Guide

This is the canonical local startup order. Run commands from the repository
root in PowerShell.

## 1. Configure the environment

```powershell
Copy-Item .env.example .env
```

Fill model-provider credentials, Java secrets, and the dedicated E2E account
locally. Never commit `.env` or print secret values. The Agent runtime token
and MCP runtime token must be present and match.

## 2. Start infrastructure

```powershell
docker compose up -d
docker compose ps
```

The Compose services provide MySQL `33306`, PostgreSQL `25432`, Redis `26379`,
Kafka `39092`, Elasticsearch `29200`, and Qdrant `26333`/`26334`.

## 3. Start application services

```powershell
.\scripts\start-greenbook.ps1
```

The launcher starts and checks services in this order:

```text
Infrastructure -> Java -> Business MCP -> Agent API/Worker -> Frontend
```

The default local profile uses queue dispatch with the API's in-process queue
consumer. For an external worker, set `GREENBOOK_AGENT_IN_PROCESS_WORKER=false` in
the local environment and start `scripts/start-agent-worker.ps1` after the
API.

Individual scripts are available when only one listener needs restarting:

- `scripts/start-be.ps1`: Java backend on `8080`.
- `scripts/start-mcp.ps1`: MCP Streamable HTTP on `8095`.
- `scripts/start-agent.ps1`: Agent API on `8094`.
- `scripts/start-agent-worker.ps1`: external durable queue worker.
- `scripts/start-fe.ps1`: Vite frontend on `5173`.

## 4. Health checks

```powershell
.\scripts\check-runtime-env.ps1
.\scripts\check-runtime-status.ps1
Invoke-WebRequest http://127.0.0.1:8080/actuator/health
Invoke-WebRequest http://127.0.0.1:8094/health
Invoke-WebRequest http://127.0.0.1:8095/health
```

Expected application endpoints are Java `200`, Agent API `200`, MCP `200`,
and frontend `http://127.0.0.1:5173/`. A service marked DOWN or UNKNOWN must
be diagnosed before running business tests.

## Runtime safety

Agent writes must use the canonical `Agent API -> TurnCoordinator -> ActionLoop
-> Durable Runtime -> MCP -> Java` path. Do not bypass MCP or invoke a write
tool directly. Destructive Browser checks use a dedicated USER account and
reject approval unless the test explicitly requires a safe approval path.
