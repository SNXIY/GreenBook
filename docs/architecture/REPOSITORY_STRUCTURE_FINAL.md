# GreenBook Repository Structure

## Formal Runtime Path

```text
User
 -> apps/assistant_api
 -> packages/assistant_core
 -> packages/creator_client / packages/java_client
 -> services/greenbook_mcp
 -> PlanExecution / ExecutionStateManager / Worker
```

## Top-Level Responsibilities

| Directory | Responsibility | Boundary |
| --- | --- | --- |
| `apps/` | Deployable applications: Assistant API/worker, Java backend, React frontend, Creator service | ACTIVE applications |
| `packages/` | Reusable Python libraries and the Agent Runtime core | ACTIVE Runtime and shared contracts |
| `services/` | MCP and external capability service implementations | ACTIVE service layer |
| `tests/` | Unit, integration, evaluation, compatibility and end-to-end coverage | Verification |
| `docs/` | Architecture, operations, integration and historical documentation | Project documentation |
| `scripts/` | Local development, startup, smoke and verification entrypoints | Operational tooling |
| `infra/` | Local infrastructure configuration and database bootstrap resources | Deployment support |
| `archive/` | Historical Creator, workflow, backend and report material | ARCHIVE |
| `community-assistant-agent/` | Older Assistant API, run model and data compatibility surface | COMPATIBILITY / LEGACY |

## ACTIVE / COMPATIBILITY / ARCHIVE

### ACTIVE

- `apps/assistant_api/`
- `apps/assistant_worker/`
- `apps/backend/`
- `apps/frontend/`
- `apps/creator-agent/`
- `packages/assistant_core/`
- shared packages under `packages/`
- `services/greenbook_mcp/`

The Agent Runtime active path is `IntentSpec -> Validator -> PlanningContext -> Planner -> TaskPlan -> PlanExecution -> ExecutionStateManager -> Worker -> ToolRuntime`.

### COMPATIBILITY

- `community-assistant-agent/`
- `TaskIntent` and intent compatibility adapters
- legacy `run_id` and `RunRepository` boundaries

These surfaces remain available for migration and legacy API/data compatibility. They are not new Runtime state sources.

### ARCHIVE

- `archive/legacy/`
- `archive/creator/`
- `archive/workflows/`
- archived phase reports under `docs/archive/`

Archived material is not imported by the ACTIVE Runtime and should not receive new behavior.

## Workspace and Import Boundary

The root `pyproject.toml` uv workspace contains the reusable Python packages, Assistant API/worker, and MCP service. `apps/backend`, `apps/frontend`, and `apps/creator-agent` remain independently built applications and are intentionally not uv workspace members.

Python imports continue to resolve through package names such as `greenbook_assistant_core`, `greenbook_contracts`, `greenbook_java_client`, and `greenbook_creator_client`; the Phase 8.2 directory moves did not rename them. The root pytest `pythonpath` remains aligned with the Python workspace directories.

## Development Startup

- Infrastructure: `docker compose up` using `docker-compose.yml`.
- Java backend: `scripts/start-be.ps1` from `apps/backend/`.
- React frontend: `scripts/start-fe.ps1` from `apps/frontend/`.
- Creator service: `scripts/start-creator.ps1` from `apps/creator-agent/`.
- Python Assistant API/worker: use the uv workspace applications under `apps/assistant_api/` and `apps/assistant_worker/`.
- Full checks: `scripts/verify-all.ps1` and the GitHub Actions workflow.

## Deployment Relations

`docker-compose.yml` provides local infrastructure such as MySQL, PostgreSQL, Redis, Kafka and Qdrant. The Java backend, frontend and Creator service run as independently deployed applications with host paths under `apps/`. The Assistant API and worker consume the uv workspace packages and call MCP, Java and Creator capabilities through their existing contracts.

The frontend retains the public `/creator-agent` proxy path, and Creator identity audience values remain service contract identifiers. Filesystem consolidation does not change those API or authentication contracts.

