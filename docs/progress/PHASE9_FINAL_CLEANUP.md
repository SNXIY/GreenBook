# Phase 9 Final Cleanup

Phase 9 completed without changing the certified business behavior. A recovery checkpoint was created first:

- branch: `feature/runtime-http-migration`
- checkpoint: `7b07c59 chore: checkpoint phase 8.3 certified state`

## 1. Removed

- Removed the empty `packages/observability` workspace package. Active observability remains in `packages/agent_core/greenbook_agent_core/observability`.
- Removed the empty `zhiguang-be/` directory.
- Removed the unused code `archive/` tree.
- Removed empty worker package directories and generated compatibility test cache directories.
- Removed unused Agent configuration variables from `.env.example`, configuration documentation, and the P0 environment runner:
  - `GREENBOOK_AGENT_REDIS_URL`
  - `GREENBOOK_AGENT_DISTRIBUTED_LIMITS_*`
  - `GREENBOOK_AGENT_SEMANTIC_MEMORY_*`
  - `GREENBOOK_AGENT_MEMORY_QDRANT_*`

`tests/compat/history` was retained because it contains active compatibility tests.

## 2. Moved

Shared execution composition was moved from the API application into `packages/agent_core`:

- runtime context and result contracts
- runtime agent service
- queue execution handler
- approval runtime service
- task provider
- result resolver
- execution presenter and projection adapter
- completion projection coordinator and publisher

The former API module paths are thin re-exports for current API/test compatibility; there is only one implementation.

## 3. Dependency Cleanup

`agent_worker` no longer depends on `greenbook-agent-api` and no longer imports `greenbook_agent_api` or `apps.agent_api`.

Both API and Worker now compose the shared runtime from `greenbook_agent_core`. An architecture boundary test prevents the Worker/Core layer from importing the API layer.

The obsolete Worker-local execution handler was removed.

## 4. Approval TODO

The active Worker TODO that auto-approved a resumed execution was removed. Durable approval remains owned by `ApprovalRuntimeService`, which validates the persisted decision. Direct Worker resumption now requires an explicit approved decision and cannot auto-approve.

## 5. Retained Compatibility

The following names were intentionally retained because they are active or persisted contracts:

- `zhiguang-fe`: referenced by scripts, CI, documentation, and tests.
- `mindflow_creator`: active Creator deployment/database identity.
- `assistant_runs` and `run_id`: active history/API compatibility identifiers.
- historical API module paths: thin re-exports during the internal ownership move.
- `tests/compat/history`: active compatibility coverage.

No public API path, database field, migration identity, or persisted history identifier was renamed.

## 6. Deferred Debt

- Large runtime methods such as `RuntimeAgentService._execute_single`, `AgentLoop.run`, and `ExecutionWorker.run` were not behaviorally refactored.
- Historical documentation naming was not broadly rewritten.
- Creator style-only Ruff findings and unrelated UI/archive cleanup were not expanded beyond this phase.

These items were intentionally deferred to avoid changing certified execution behavior.

## 7. Final Tree

```text
green-book/
├── apps/
│   ├── agent_api/
│   ├── agent_worker/
│   └── backend/
├── packages/
│   ├── agent_core/
│   ├── contracts/
│   ├── creator_client/
│   ├── evaluation/
│   ├── java_client/
│   └── security/
├── services/
│   └── greenbook_mcp/
├── creator-agent/
├── zhiguang-fe/
├── contracts/
├── docs/
├── infra/
├── scripts/
├── tests/
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
└── README.md
```

The removed `archive/`, `zhiguang-be/`, and top-level `packages/observability/` paths are not part of the active tree.

## 8. Regression

Final validation results:

| Check | Result |
|---|---|
| Root `uv run pytest -q` | PASS |
| Creator `uv run pytest -q` | PASS |
| Backend `mvn test -q` | PASS |
| Frontend lint | PASS |
| Frontend build | PASS |
| Frontend execution tests | PASS |
| `uv lock --check` | PASS |
| Active runtime Ruff | PASS |
| `python -m compileall` | PASS |
| `docker compose config --quiet` | PASS |
| `git diff --check` | PASS |

Focused cleanup regression also passed: `21 passed` for Worker, runtime, completion publisher, control API, and architecture-boundary tests. Pytest emitted only an existing cache-directory permission warning.

## 9. Certification

```text
CERTIFIED preserved
```

No certified business capability or runtime state-machine behavior was intentionally changed.
