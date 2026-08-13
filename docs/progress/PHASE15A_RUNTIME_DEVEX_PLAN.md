# Phase15-A Runtime Developer Experience Plan

## Scope

Phase15-A only improves local development startup and observability. The
runtime topology remains:

```text
Frontend -> Assistant API -> PostgreSQL Execution Queue -> Assistant Worker
         -> ExecutionWorker -> ToolRuntime -> Creator / Java Backend
```

Assistant Worker remains a separate process. The API does not execute queued
work synchronously and PostgreSQL Queue + Lease remains the durable dispatch
mechanism.

## Startup changes

The new entry point is:

```powershell
.\scripts\start-greenbook.ps1
```

It performs a configuration check, launches Java Backend, waits for Java
readiness, launches Creator Agent, waits for Creator readiness, launches
Assistant API, waits for the API health endpoint, and then launches Assistant
Worker and Frontend. Each service remains an independent PowerShell process
with its own log window.

Use `-NoReload` for a more stable local run:

```powershell
.\scripts\start-greenbook.ps1 -NoReload
```

The existing individual scripts remain available for focused debugging.

## Environment validation

`scripts/check-runtime-env.ps1` validates the durable Runtime profile before
startup:

- `ASSISTANT_DATABASE_URL`
- `ASSISTANT_EXECUTION_DISPATCH=queue`
- `ASSISTANT_EXECUTION_QUEUE_CONSUMER=true`
- `ASSISTANT_WORKER_ACCESS_TOKEN`
- `JAVA_BASE_URL` (with existing `ASSISTANT_JAVA_BASE_URL` compatibility)
- `CREATOR_BASE_URL` (with existing `ASSISTANT_CREATOR_BASE_URL` compatibility)

Missing or invalid values produce actionable errors and prevent the unified
launcher from starting application processes.

## Runtime status

`scripts/check-runtime-status.ps1` prints:

```text
GREENBOOK Runtime Status
API:      READY / UNAVAILABLE
Worker:   READY / UNAVAILABLE
Queue:    READY / UNAVAILABLE
Database: READY / UNAVAILABLE
Creator:  READY / UNAVAILABLE
Java:     READY / UNAVAILABLE
```

Worker readiness uses the existing heartbeat file. Queue readiness requires a
fresh Worker heartbeat and `ASSISTANT_EXECUTION_QUEUE_CONSUMER=true`; a running
API alone cannot make Queue READY.

## Docker Compose evaluation (design only)

Docker Compose is not introduced as the Phase15-A launcher. A future Compose
profile could run `frontend`, `assistant-api`, `assistant-worker`, `creator`,
and `backend` as separate services, with health-gated dependencies and a
shared PostgreSQL network. The following issues need a deliberate follow-up:

1. Java currently relies on host-side Maven and local development secrets.
2. Creator currently performs local migration and uses its own Python
   environment and supporting services.
3. Assistant API and Worker must share the same database URL, worker token,
   JWT/JWKS configuration, and service URLs.
4. Frontend proxy configuration must resolve service names instead of local
   loopback addresses.
5. Compose health checks must distinguish API readiness from Worker/Queue
   readiness; starting the API must never imply that queued work is consumed.

Recommendation for Phase15-B: add an opt-in `docker-compose.dev.yml` profile
after container boundaries, secret injection, migration ownership, and
healthcheck contracts are documented. Do not replace the current PowerShell
launcher until the Compose profile passes the same runtime status and queue
claim checks.

## Explicit non-goals

- No Kafka migration.
- No Legacy Cleanup.
- No Planner, Intent, ToolRuntime, ExecutionStateManager, Java business logic,
  or compatibility-code removal.
- No service merging.
