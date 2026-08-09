# Phase 11.6-D9-A Final Resource Audit

Audit date: 2026-08-09

This is a read-only resource audit. No Runtime implementation, database
table, repository, script, Docker resource, or CI job was deleted.

## 1. Decision Summary

| Area | Resource | Classification | Evidence / decision |
|---|---|---|---|
| Runtime | `PlanExecution`, `ExecutionStateManager`, `ExecutionEventStore` | KEEP / ACTIVE | Active API routes, execution services, tests, and Runtime reports reference them. |
| History | `assistant_runs`, `RunRepository` | COMPATIBILITY | API projection/read paths and projection contract tests still reference them. |
| History | `compatibility/history/RunExecutionLink` and repositories | COMPATIBILITY | API, approval, link tests, and persistent link metadata reference them. |
| Python package | `compatibility/intent` | COMPATIBILITY | Compatibility tests and legacy intent imports still reference the package. |
| Python package | `apps/assistant_api`, `packages/assistant_core`, `services/greenbook_mcp` | KEEP / ACTIVE | Workspace members, imports, CI, and API/runtime entrypoints reference them. |
| Scripts | `scripts/start-*.ps1`, `dev-up.ps1`, `setup-dev.ps1`, `verify-all.ps1`, `smoke-test.ps1` | KEEP / ACTIVE | Startup, local development, acceptance, and CI documentation reference them. |
| Scripts | `scripts/e2e-test.ps1`, `run_p0_e2e.py`, `test_run_p0_e2e.py` | KEEP / ACTIVE | Acceptance docs and the harness test reference them; they exercise active services. |
| Scripts | `Import-GreenBookEnv.ps1`, `ensure-jwt-keys.ps1`, `rotate-dev-secrets.ps1`, `scripts/ops/promote-admin.ps1` | KEEP / operational | Direct script consumers or documented administration flows exist. `promote-admin.ps1` is manually invoked and has no CI caller, so it is a future candidate only. |
| Scripts | `runtime-report.ps1` | KEEP / ACTIVE | Queries the canonical `/executions` API. |
| Docker | `zhiguang-mysql`, `zhiguang-kafka`, `creator-postgres`, `creator-redis`, `greenbook-qdrant` | KEEP / ACTIVE | Declared in root Compose and consumed by local startup/application configuration. |
| Docker | Root named volumes and default network | KEEP / ACTIVE | Compose service persistence and inter-service networking depend on them. |
| Database | `assistant_runs` and `run_execution_link` | COMPATIBILITY | Runtime projection/history and ID mapping boundaries remain active. |
| Database | `001_assistant_runs_history_projection.sql` | KEEP / migration | Required to make Runtime-backed history projection status nullable; no deployment consumer was found that permits deletion. |
| Database | Execution metadata created by `execution_*` repositories | KEEP / ACTIVE | Persistent execution stores call `metadata.create_all()` and are part of Runtime persistence. |
| CI | `.github/workflows/verify.yml` | KEEP / ACTIVE | Runs Java, frontend, Creator, and Assistant Runtime verification jobs. |
| Documentation | `docs/architecture/*.md` | KEEP / ACTIVE or audit record | Current architecture and migration decisions remain referenced; older Phase reports are historical records. |
| Documentation | `docs/archive/` | ARCHIVE | Historical reports and drafts; no production import path. |

## 2. Python Package and Import Graph

The scan covered Python files under `packages/`, `apps/`, `services/`, and
`tests/`. The active graph contains these relevant edges:

```text
Assistant API routes
  -> compatibility.history
  -> RunExecutionAdapter / ExecutionReference
  -> PlanExecution / ExecutionStateManager / ExecutionEventStore

Assistant API routes
  -> RunRepository
  -> assistant_runs

compatibility.history
  -> RunExecutionLink
  -> run_execution_repository
  -> run_execution_link table metadata
```

The retired `compatibility/runtime` package is absent. No active import of
that namespace was found. No module in the audited active Runtime or History
package was proven unreferenced by this static scan. Entrypoint modules and
package initializers are intentionally treated as roots rather than inferred
dead code.

## 3. Scripts and Development Flow

The local flow is connected as follows:

```text
dev-up.ps1
  -> Docker Compose middleware
  -> start-be.ps1 / start-creator.ps1 / start-assistant.ps1 / start-fe.ps1

setup-dev.ps1
  -> ensure-jwt-keys.ps1

verify-all.ps1 / smoke-test.ps1 / e2e-test.ps1
  -> application tests and live service checks

runtime-report.ps1
  -> Assistant /executions API

run_p0_e2e.py
  -> active Assistant, Creator, Java, Redis/SQLite test infrastructure
```

`test_run_p0_e2e.py` directly imports the P0 harness. `Import-GreenBookEnv.ps1`
is a shared dependency of startup, E2E, administration, and reporting scripts.
No script is a proven deletion candidate. `scripts/ops/promote-admin.ps1` is the weakest
candidate because it is an operator tool with no CI caller, but manual use and
runbook value have not been disproved.

## 4. Docker and Runtime Services

`docker compose config` reports these root services:

```text
creator-postgres
creator-redis
greenbook-qdrant
zhiguang-kafka
zhiguang-mysql
```

The root Compose file declares five named volumes:
`zhiguang-mysql-data`, `zhiguang-kafka-data`, `creator-postgres-data`,
`creator-redis-data`, and `greenbook-qdrant-data`. The root project uses the
default Compose network. `infra/docker-compose.dev.yml` provides the
development Postgres/Redis/Qdrant variant and its own three persistence
volumes. These are configuration resources, not deletion candidates.

The command emitted a Docker client warning that the local Docker config was
inaccessible, but still resolved the service, volume, and network model. No
container lifecycle or volume contents were changed by this audit.

## 5. Database and Persistence

The Assistant database boundary is split intentionally:

- `RunRepository` owns `assistant_runs` history/projection persistence.
- `001_assistant_runs_history_projection.sql` drops the old non-null status
  requirement for Runtime-backed projection rows.
- `RunExecutionLink` persists only the `run_id`/`execution_id` relationship and
  mapping metadata.
- Runtime execution repositories persist `PlanExecution`-related data through
  execution metadata, independently of `assistant_runs`.
- Creator owns its Alembic migrations and database schema separately.

No database object is classified DELETE_CANDIDATE. The Assistant migration
has an operational prerequisite and should remain available for deployment
and rollback review.

## 6. Configuration and CI

`pyproject.toml` declares the workspace packages and test paths. `.env.example`
defines Assistant Runtime, Creator, Java, Redis, Qdrant, model, and MCP
configuration. `.env` is local secret-bearing state and was not modified.

The prior audit identified Redis port and Assistant audience drift. D9-B
aligned those non-secret values. `.env.example` still intentionally contains
development placeholders and is protected by the secret-rotation/startup
flow.

These are configuration follow-up items, not grounds to delete either file.
The GitHub workflow has active Java, frontend, Creator, and Assistant Runtime
jobs and remains KEEP.

## 7. Documentation Classification

Current architecture, migration, compatibility, and D8/D9 documents are
ACTIVE decision records and should remain. `docs/archive/` and historical
Phase reports are ARCHIVE records; their old conclusions and paths are not
evidence of an active import and should not be rewritten as part of this
resource audit.

Some older architecture reports describe pre-migration script or Legacy Agent
relationships that no longer match the current Runtime-only wiring. They are
historical findings, not deletion authorization. The current source of truth
is the latest architecture and phase documentation plus the import/config
scans above.

## 8. DELETE_CANDIDATE Result

No resource met the evidence threshold for DELETE_CANDIDATE. The only weak
candidate found was the manually operated `scripts/ops/promote-admin.ps1`, which
still has plausible runbook value. `assistant_runs`, `RunRepository`,
`RunExecutionLink`, Runtime packages, Docker resources, migrations, and CI
configuration all have active or compatibility evidence and must be kept.
