> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-B Legacy Execution Isolation

## 1. Scope

This phase audits and isolates Legacy execution. No Legacy code, database
table, repository, or Runtime core was deleted or moved.

The active execution source remains:

```text
execution_id -> PlanExecution -> ExecutionEventStore
```

## 2. `/runs` Endpoint Classification

| Endpoint | Current use | Classification |
| --- | --- | --- |
| `GET /runs/{run_id}` | Legacy history; mapped Runtime compatibility detail | KEEP as history API, MIGRATE mapped consumers |
| `GET /runs` | Compatibility index used to discover old references and metadata; mapped rows refresh status/steps through Execution API | KEEP temporarily as history/index API |
| `GET /runs/{run_id}/events` | Legacy-only event history; mapped requests delegate to ExecutionEventStore | KEEP as compatibility API |
| `GET /runs/{run_id}/events/stream` | Legacy-only stream; mapped requests delegate to Runtime event stream | KEEP as compatibility API |
| `POST /runs/{run_id}/cancel` | Legacy-only control; mapped requests delegate to ExecutionStateManager | KEEP as compatibility API |
| `POST /runs/{run_id}/interrupt` | Legacy-only control; mapped requests delegate to Runtime pause | KEEP as compatibility API |
| `POST /runs/{run_id}/resume` | Legacy-only control; mapped requests delegate to Runtime resume | KEEP as compatibility API |
| `POST /runs/{run_id}/approve` | Legacy-only approval compatibility entrypoint | KEEP as compatibility API |

### Active consumer result

There are no ACTIVE Runtime consumers using `/runs` as the execution state,
step state, event source, SSE source, or control source. Runtime-backed
frontend operations use `/executions/{execution_id}` and its subroutes.

The frontend still calls `GET /runs` as a compatibility index to obtain
legacy references and user-facing metadata. It immediately refreshes mapped
Runtime status and steps through the Execution API. This is a reference
discovery dependency, not an ACTIVE execution lifecycle dependency.

Removing this remaining index call requires a separately authorized execution
list API because `PlanExecution` does not itself contain the conversation and
tenant metadata needed by the current task list. It is therefore not safe to
remove in this phase.

## 3. Runtime Compatibility Adapter Audit

### `RunExecutionAdapter`

Current references:

- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  creates links after Runtime execution and resolves mapped legacy requests.
- `apps/assistant_api/greenbook_assistant_api/services/runtime_linking.py`
  binds the Runtime result at the API boundary.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` uses it for
  `ExecutionReference` responses and legacy endpoint delegation.
- `tests/compat/runtime`, Runtime link tests, and freeze tests cover the
  boundary.

**Classification:** `MIGRATE`, then `DELETE_CANDIDATE` after the `/runs` API,
legacy response fields, and compatibility tests are retired. It does not own
execution state and must not be deleted while Runtime-backed responses still
need a legacy link.

### `RunOperationAdapter`

It is used only by legacy run operation routes. It translates run cancel,
pause, resume, interrupt, event lookup, and stream requests into
`ExecutionStateManager`/`ExecutionEventStore` operations.

**Classification:** `DELETE_CANDIDATE` after all `/runs` control and stream
routes are retired. Until then it is the required isolation boundary.

### `ExecutionReference`

It is still emitted in Assistant accepted responses and normalized by the
frontend. It provides a stable bridge for old clients while carrying the
canonical `execution_id`.

**Classification:** `KEEP` during API contract migration; later
`DELETE_CANDIDATE` only if the public contract becomes execution-only and all
legacy response consumers are gone.

## 4. `community-assistant-agent` Reference Scan

### No ACTIVE workspace execution dependency found

The D4-A migration removed its use from the ACTIVE CI job, setup workflow,
smoke checks, and P0 Assistant process startup. The P0 harness now starts
`greenbook_assistant_api` from the workspace root.

### Remaining compatibility or historical references

- `scripts/runtime-report.ps1` still enters the historical project to run its
  old evaluation report script.
- `scripts/start-assistant.ps1` retains the historical JWT audience default
  `community-assistant-agent`; this is an identity contract value, not a
  source-directory dependency.
- Backend Java auth tests/configuration retain the audience value for token
  compatibility.
- `apps/backend/.idea/workspace.xml` contains IDE history references.

**Classification:**

- Project directory: `DELETE_CANDIDATE` after the historical report and token
  audience migration are approved.
- `runtime-report.ps1`: `MIGRATE` or archive; not an ACTIVE Runtime consumer.
- JWT audience string: `KEEP` until Java/API identity contract migration;
  never globally replace it with `execution_id`.
- IDE metadata: `ARCHIVE_CANDIDATE`, with no runtime significance.

No Docker service or current CI job was found that directly starts
`community-assistant-agent` after D4-A. The directory remains intact.

## 5. Legacy Agent Isolation

`LegacyAgentService` is constructed only when one of these explicit conditions
holds:

```text
ASSISTANT_RUNTIME_MODE=off
or
ENABLE_LEGACY_AGENT_FALLBACK=true
```

The default configuration is Runtime-first:

```text
ASSISTANT_RUNTIME_MODE=on
ENABLE_LEGACY_AGENT_FALLBACK=false
```

The normal Runtime success path does not instantiate or invoke Legacy. Runtime
failure returns a Runtime error unless the emergency fallback switch is
explicitly enabled. The fallback is wrapped by `LegacyFallbackAdapter`; the
Runtime service does not import or call `agent.py` directly.

**Classification:**

- `LegacyAgentService`: `MIGRATE`, then `DELETE_CANDIDATE` after emergency
  fallback retirement.
- `LegacyFallbackAdapter`: `KEEP` as the isolated emergency boundary, then
  `DELETE_CANDIDATE`.
- `packages/assistant_core/greenbook_assistant_core/agent.py`:
  `DELETE_CANDIDATE` after zero production imports and explicit Legacy tests
  have been retired.
- `ASSISTANT_RUNTIME_MODE` and `ENABLE_LEGACY_AGENT_FALLBACK`: `KEEP`
  temporarily for rollback/emergency operation, then remove with their code
  paths.

## 6. Remaining Legacy Consumers

The following are deliberately retained as compatibility/history consumers:

- `RunRepository` and `assistant_runs` history/projection storage;
- old `/runs` API routes;
- Legacy-only frontend fallback branches;
- compatibility runtime tests;
- Java `run_id`/`assistant_run_id` provenance fields;
- Creator `run_id` and `creator_run_id` contracts.

These identifiers are not interchangeable with `execution_id`. No global
replacement is safe.

## 7. Removal Gates

Before deleting any candidate, verify:

- no active UI operation reads `/runs` for Runtime status, steps, events, SSE,
  control, retry, or approval;
- an authorized execution-list/history replacement exists for the remaining
  frontend index use;
- no Python import of `LegacyAgentService` or `agent.py` remains in active
  execution code;
- emergency fallback is disabled in all deployed environments;
- `community-assistant-agent` is absent from CI, Docker, scripts, E2E, and
  deployment documentation, except explicitly archived records;
- Java and Creator contracts have independent migration decisions;
- legacy data retention/export and database rollback plans are approved;
- unit, compatibility, evaluation, integration, and frontend suites pass.

## 8. Result

The ACTIVE Runtime lifecycle is isolated from Legacy. No `/runs` endpoint is
used as the Runtime state source, and no current CI/P0 process starts the old
assistant project. The remaining adapter, history, and Legacy implementation
are migration-bound compatibility components, not safe immediate deletion
targets.
