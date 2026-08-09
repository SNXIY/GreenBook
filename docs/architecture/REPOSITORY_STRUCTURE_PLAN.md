# GreenBook Repository Structure Plan

Phase 8.0 read-only structure audit. No files were moved, deleted, or
modified by this audit.

## 1. Current Architecture

The repository contains four kinds of content:

1. Active GreenBook Python Agent Runtime packages and applications.
2. Active adjacent Java, React, Creator, and infrastructure projects.
3. Compatibility and historical Agent implementations.
4. Documentation, evaluation, scripts, generated artifacts, and design assets.

The current tree is operationally valid but structurally mixed. In
particular, Java/React projects use root-level names while Python services use
`apps/`, `packages/`, and `services/`. Historical projects remain next to the
active Runtime, and generated local directories are visible at repository
root.

## 2. First-Level Directory Analysis

| Directory | Current role | Status | Recommended boundary |
| --- | --- | --- | --- |
| `apps/` | Assistant API and async worker applications | ACTIVE | Keep as application entrypoints |
| `packages/` | Python libraries: core, contracts, clients, security, evaluation, observability | ACTIVE | Keep as reusable packages |
| `services/` | `greenbook_mcp` business capability service; the former `creator_agent` package is archived | ACTIVE / ARCHIVE HISTORY | Keep active MCP under services; keep archive outside workspace |
| `creator-agent/` | Complete standalone Creator API, graph, worker, persistence, UI, Docker, and tests | ACTIVE ADJACENT SERVICE | Long-term candidate for `apps/creator_agent/`, after path migration |
| `community-assistant-agent/` | Historical Assistant Agent with own API, worker, database, `run_id`, and migrations | COMPATIBILITY / LEGACY | Long-term `archive/legacy/community-assistant-agent/` |
| `greenbook-backend/` | Active Java Spring Boot community backend | ACTIVE ADJACENT APP | Long-term candidate for `apps/community_backend/`; preserve current path until migration |
| `greenbook-frontend/` | Active React/Vite community frontend | ACTIVE ADJACENT APP | Long-term candidate for `apps/community_frontend/`; preserve current path until migration |
| `contracts/` | Cross-language OpenAPI contracts used by clients, backend scripts, and tests | ACTIVE CONTRACTS | Keep at root or move only with coordinated path update |
| `infra/` | Shared Docker development infrastructure and migrations | ACTIVE INFRASTRUCTURE | Keep at root; not an app or package |
| `tests/` | Unit, contract, integration, E2E, evaluation, and compatibility tests | ACTIVE TESTS | Keep at root; refine categories only after import audit |
| `docs/` | Architecture, formal docs, archived reports, drafts, integration docs | ACTIVE DOCS / HISTORY | Keep; architecture is the formal entrypoint |
| `archive/` | Archived Creator package and historical MCP workflows | ARCHIVE | Keep outside uv workspace and production imports |
| `scripts/` | Development, startup, smoke, E2E, secret, and verification scripts | SUPPORT | Keep; split by purpose later if useful |
| `docs/design-system/` | Design references and visual previews for multiple products | DESIGN ASSETS | Keep outside Python workspace |
| `contracts/` | API schemas, distinct from `packages/contracts` typed Python models | ACTIVE CONTRACTS | Keep, but document naming distinction |
| `zhiguang-be/` | Residual backend directory containing only database material in current tree | LEGACY / UNKNOWN | Verify ownership, then archive or delete candidate |
| `zhiguang-fe/` | Empty/residual frontend directory in current tree | DELETE CANDIDATE | Verify Git history/external references before deletion |

Hidden local directories such as `.venv`, `.venv-v2`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `.p0-*`, `node_modules`, `target`, `dist`, and
IDE metadata are generated or local state. They should be ignored or removed
by a separate safe artifact cleanup, not moved into the source archive.

## 3. Apps vs Packages

### Belongs under `apps/`

These directories have process/API entrypoints, deployment configuration, or
user-facing application behavior:

- `apps/assistant_api/`
- `apps/assistant_worker/`
- `creator-agent/` as a future `apps/creator_agent/`
- `greenbook-backend/` as a future `apps/community_backend/`
- `greenbook-frontend/` as a future `apps/community_frontend/`

`community-assistant-agent/` is also an application in a technical sense, but
it is historical and should go to `archive/legacy/`, not to ACTIVE `apps/`.

### Belongs under `packages/`

The following are reusable libraries with package metadata and no primary
process lifecycle:

- `packages/assistant_core/`
- `packages/contracts/`
- `packages/java_client/`
- `packages/creator_client/`
- `packages/security/`
- `packages/observability/`
- `packages/evaluation/`
- `packages/persistence/`

`services/greenbook_mcp/` is intentionally a service rather than a package:
it owns the capability/tool process boundary even though it also publishes a
Python package.

## 4. Archive and Delete Candidates

### Already archived

- `archive/creator/creator_agent/`: former `services/creator_agent` workspace
  skeleton.
- `archive/workflows/`: unwired historical Creator workflows.

These paths must remain outside root `uv` workspace members and production
`pythonpath` entries.

### Future archive candidates

- `community-assistant-agent/`, after `run_id` API, approval, persistence,
  CI, and deployment consumers are migrated.
- One of any duplicate Creator service trees, after deployment owner and API,
  migration, health, and data ownership are proven.
- `zhiguang-be/` and `zhiguang-fe/`, after confirming they are not part of a
  release, external script, or required historical data workflow.
- `docs/reports/` and `docs/drafts/` only through the existing documentation
  archive policy; do not mix them with source archives.

### Delete candidates

Potential deletion is limited to verified generated artifacts and empty
residual directories. No production source directory is approved for direct
deletion by this plan.

Deletion requires all of:

- no source, test, CI, Docker, script, or documentation contract reference;
- an existing replacement or confirmed historical status;
- no data/migration ownership;
- a clean recovery or Git-history path;
- a separate approved execution step.

## 5. Recommended Target Tree

This is a target design, not an instruction to move files in Phase 8.0:

```text
green-book/
  apps/
    assistant_api/
    assistant_worker/
    creator_agent/
    community_backend/
    community_frontend/
  packages/
    assistant_core/
    contracts/
    creator_client/
    java_client/
    security/
    observability/
    persistence/
    evaluation/
  services/
    greenbook_mcp/
  contracts/
    assistant-openapi.yaml
    java-openapi.yaml
  infra/
  tests/
    unit/
    contract/
    integration/
    e2e/
    evaluation/
    compat/
  docs/
    architecture/
    guides/
    archive/
  archive/
    legacy/
    creator_agent/
    greenbook_mcp/
  scripts/
    docs/design-system/
```

The target tree intentionally keeps `contracts/` separate from
`packages/contracts/`: the former is versioned cross-language API schema, the
latter is the Python typed contract package.

## 6. Move Impact Analysis

### 6.1 `pyproject.toml` and uv workspace

Moving a Python workspace member requires coordinated updates to:

- `[tool.uv.workspace].members`;
- `[tool.uv.sources]` names and editable paths;
- `tool.pytest.ini_options.pythonpath`;
- each moved package's build metadata and package discovery paths;
- `uv.lock` manifest members, editable sources, and package records.

The archived `services/creator_agent` was removed from the active workspace in
Phase 7.10. A future move of `creator-agent/` into `apps/creator_agent/`
would require a new package path audit and a lockfile regeneration.

Moving `packages/*` is higher risk than renaming an app because package names
are imported throughout Runtime, tests, and workspace metadata. Package
directory names should remain stable unless there is a strong reason.

### 6.2 Docker Compose

The root `docker-compose.yml` currently mounts:

- `greenbook-backend/db/*` into MySQL;
- shared PostgreSQL, Redis, Kafka/Redpanda, and Qdrant infrastructure.

It intentionally runs Java, frontend, Creator, and moderation services on the
host. Moving `greenbook-backend` or `greenbook-frontend` requires updating
volume paths, working directories in helper scripts, and any environment
defaults. Moving `creator-agent` requires reviewing its own
`creator-agent/docker-compose.yml`, Docker build context, migration paths, and
health checks.

### 6.3 GitHub Actions

`.github/workflows/verify.yml` currently uses these working directories:

- `greenbook-backend` for Maven;
- `greenbook-frontend` for npm;
- `creator-agent` for Creator lint/tests;
- `community-assistant-agent` for legacy Agent tests.

Any app move must update `defaults.run.working-directory`, cache paths,
generated key paths, and commands. The legacy CI job is a direct reason not to
archive `community-assistant-agent` yet.

### 6.4 Scripts and import paths

The following scripts hard-code current application paths:

- `scripts/start-be.ps1`
- `scripts/start-fe.ps1`
- `scripts/start-creator.ps1`
- `scripts/setup-dev.ps1`
- `scripts/smoke-test.ps1`
- `scripts/verify-all.ps1`
- `scripts/run_p0_e2e.py`
- `scripts/runtime-report.ps1`

Python package moves affect imports, editable installs, pytest `pythonpath`,
and workspace source resolution. Java/React moves affect process working
directories and asset/API environment variables rather than Python imports.

## 7. Phased Recommendation

1. Keep the current ACTIVE Python paths stable: `apps/`, `packages/`, and
   `services/greenbook_mcp/`.
2. Keep `creator-agent/`, `greenbook-backend/`, and `greenbook-frontend/` at
   their current paths until deployment and CI migration is planned.
3. Keep `community-assistant-agent/` as compatibility until the run/API/data
   migration is complete.
4. Keep root `contracts/` because Java client scripts and contract tests use
   its current paths.
5. Verify `zhiguang-be/` and `zhiguang-fe/` independently before archiving or
   deleting them; current directory contents alone are not sufficient proof.
6. Perform any application move as one coordinated change including code
   paths, metadata, CI, Docker, scripts, documentation, and tests.

## 8. Audit Boundary

This document is a structure plan only. It does not authorize or perform any
move, deletion, import rewrite, workspace regeneration, or deployment change.
