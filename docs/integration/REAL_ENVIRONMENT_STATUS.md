# GreenBook Real Environment Status

> Phase 8.0 evidence captured on 2026-08-12 (Asia/Shanghai). This file records the real local environment used for integration validation. No fake LLM, mock Java client, mock Creator, or test substitute was used.

## Service status

| Service | Port | Status | PID / container | Health evidence |
|---|---:|---|---|---|
| Frontend (`zhiguang-fe`) | 5173 | UP | PID 60116 (`node.exe`) | `GET /` = 200; Vite `/agent-api` proxy with a real JWT = 200 |
| Agent API | 8094 | UP | PID 45800 (`python.exe`) | `GET /health` = 200; Java and Creator reachable |
| Agent Worker | no HTTP port | READY | worker PID 59528; shim PID 1716 | `.runtime/agent-worker-health.json`, Postgres queue consumer active |
| Java Backend (`apps/backend`) | 8080 | UP | PID 34308 (`java.exe`) | `GET /actuator/health` = 200 |
| Creator Service | 8092 | UP | PID 26956 (`python.exe`) | `/actuator/health` and `/actuator/health/ready` = 200 |
| PostgreSQL | 25432 -> 5432 | healthy | `greenbook-postgres` | Docker Compose healthcheck = healthy |
| Redis | 26379 -> 6379 | healthy | `greenbook-redis` | Docker Compose healthcheck = healthy |
| MySQL | 33306 -> 3306 | healthy | `greenbook-mysql` | Docker Compose healthcheck = healthy |
| Kafka / Redpanda | 39092 -> 9092 | healthy | `greenbook-kafka` | Docker Compose healthcheck = healthy; TCP port reachable |
| Qdrant | 26333 -> 6333; 26334 -> 6334 | running | `greenbook-qdrant` | `GET http://127.0.0.1:26333/collections` = 200; Compose has no healthcheck for this service |

The Agent API health response at capture time was:

```json
{
  "status": "UP",
  "version": "2.0.0",
  "javaConfigured": true,
  "creatorConfigured": true,
  "javaReachable": true,
  "creatorReachable": true,
  "executionDispatch": "queue",
  "executionStorage": "postgres",
  "executionConsumer": "external"
}
```

## Startup commands used

Infrastructure was started with:

```powershell
cd D:\agent\green-book
docker compose up -d
```

Application processes were started separately:

```powershell
.\scripts\start-be.ps1
.\scripts\start-creator.ps1
.\scripts\start-agent.ps1 -ApiOnly -NoReload
.\scripts\start-agent-worker.ps1 -WorkerAccessToken <fresh service JWT>
.\scripts\start-fe.ps1
```

The Worker was a real external Postgres queue consumer. The JWT value is intentionally not recorded.

## Logs and runtime evidence

| Process | Log / evidence location |
|---|---|
| Agent API | `.runtime/phase8-agent-api.out.log`, `.runtime/phase8-agent-api.err.log` |
| Agent Worker | `.runtime/phase8-agent-worker.out.log`, `.runtime/phase8-agent-worker.err.log` |
| Worker readiness | `.runtime/agent-worker-health.json` |
| Java, Creator, Frontend | Foreground PowerShell launch terminals during this validation; no persistent file was configured |
| Infrastructure | `docker compose ps`; container logs remain available through `docker compose logs <service>` |

## Environment boundary

- Agent execution dispatch was `queue`, persisted in PostgreSQL.
- Worker claim, checkpoint, ledger, retry, recovery, and completion projection ran through the real Worker process.
- Java business operations used the Java Agent Facade over HTTP with a real user JWT.
- Creator generation used the Creator HTTP API and the live configured model provider.
- The configured live LLM provider later returned HTTP 402 (`Insufficient Balance`). Cases that require new LLM reasoning after that point are reported as blocked rather than replaced with deterministic or mock output.
