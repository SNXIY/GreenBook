> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-C Legacy Removal Preparation

## 1. Scope

This document is a deletion-readiness audit and API design. No database,
`assistant_runs` row, Legacy source, or compatibility module was deleted.

The canonical Runtime source remains:

```text
execution_id -> PlanExecution -> ExecutionEventStore
```

## 2. Pre-Deletion Classification

### DELETE_NOW

None.

Every inspected candidate still has at least one production, API, test,
rollback, or historical compatibility dependency. Deleting any candidate now
would either break the Runtime-to-legacy response link, remove Legacy-only
history, or make the emergency path impossible to disable safely.

### KEEP_TEMPORARY

| Component | Current dependency | Removal gate |
| --- | --- | --- |
| `LegacyAgentService` | Imported by Assistant API and constructed for explicit Legacy mode/emergency fallback | `ASSISTANT_RUNTIME_MODE=off` removed, emergency fallback usage is zero, and no Python import remains |
| `LegacyFallbackAdapter` | Sole explicit Runtime-failure-to-Legacy boundary | Emergency process retired and fallback tests archived |
| `RunExecutionAdapter` | API boundary link binding, `ExecutionReference`, and `/runs` compatibility resolution | No API response or consumer requires `run_id` |
| `RunOperationAdapter` | `/runs` cancel, pause, resume, events, and stream delegation | All `/runs` operation routes retired |
| `ExecutionReference` | Public accepted response and frontend compatibility contract | Public contract becomes execution-only and old clients are retired |
| `RunRepository` / `assistant_runs` | Legacy history/projection and metadata lookup | Retention/export policy and database migration approved |
| `/runs` API | Legacy-only history, metadata, and rollback surface | Execution list/history replacement deployed and old clients retired |
| `community-assistant-agent` | Historical report script, identity audience, archived docs/IDE references | Script and identity migrations complete, then archive verification passes |

## 3. Execution List API Design

### Proposed endpoint

```http
GET /api/v1/assistant/executions?limit=30&cursor=<cursor>
```

The endpoint is an ACTIVE Runtime API. It must not read
`assistant_runs.status`, `assistant_runs.events`, or any Legacy execution
state.

### Response

```json
{
  "items": [
    {
      "execution_id": "execution-123",
      "task_id": "task-456",
      "plan_id": "plan-789",
      "status": "RUNNING",
      "current_step": "search_posts",
      "progress": 0.5,
      "created_at": "2026-08-09T00:00:00Z",
      "updated_at": "2026-08-09T00:01:00Z"
    }
  ],
  "next_cursor": null
}
```

### Source and authorization

- Read execution snapshots through `ExecutionStateManager.list_executions()`
  or the configured execution repository.
- Derive status, current step, and progress from `PlanExecution`.
- Use `ExecutionEventStore` only for event-specific views, never
  `assistant_runs.events`.
- Apply tenant/user authorization before returning an item.
- Because `PlanExecution` does not currently carry user/tenant metadata, the
  API must use an existing authorized task/execution ownership resolver or an
  explicitly configured `execution_authorizer`; it must not expose an
  unfiltered repository-wide list.
- Pagination must be deterministic, preferably by `updated_at` plus
  `execution_id`.

### Client migration

After this endpoint is available:

1. `apps/frontend` replaces the remaining `/runs` index discovery call with
   `/executions`.
2. The UI uses `execution_id` as the primary task key and display reference.
3. `run_id` remains optional only when rendering Legacy history.
4. `RunExecutionAdapter` is no longer needed by active Runtime list/detail
   consumers, although old API responses may continue to emit
   `ExecutionReference` during the compatibility window.

## 4. `/runs` Dependency Removal Plan

Current mapped Runtime status, steps, events, SSE, cancel, pause, resume,
retry, and approval operations already use Execution API or delegate through
the compatibility adapter. The remaining active-boundary dependency is the
frontend metadata/index lookup:

```text
GET /runs -> execution_id discovery + user-facing metadata
```

It cannot be removed until the execution list API supplies an authorized
replacement for both execution metadata and task-list pagination. Once the
frontend migrates, `/runs` is Legacy-only history and can be marked
`DELETE_CANDIDATE` rather than an ACTIVE consumer.

## 5. `community-assistant-agent` Archive Readiness

### Scan result

- CI no longer runs from `community-assistant-agent` after D4-A.
- P0 E2E Assistant startup now targets `greenbook_assistant_api` from the
  workspace root.
- Setup and smoke verification target the ACTIVE workspace.
- `scripts/runtime-report.ps1` still invokes the historical report script from
  the old project.
- `start-assistant.ps1` and Java auth configuration retain the old audience
  string `community-assistant-agent`; this is an identity contract value, not
  a source path.
- Archived architecture and phase documents contain historical references.

### Classification

`community-assistant-agent/` is an `ARCHIVE_CANDIDATE`, not ready for archive
execution in this phase. First migrate or archive `runtime-report.ps1`, decide
the JWT audience contract, and exclude historical documents/IDE metadata from
the active reference scan.

No Docker service currently starts the old directory, but Docker should be
rechecked after the identity and report migrations.

## 6. Legacy Invocation Boundary

The only permitted Legacy invocation remains:

```text
ASSISTANT_RUNTIME_MODE=off
or explicit ENABLE_LEGACY_AGENT_FALLBACK=true emergency path
```

Defaults remain:

```text
ASSISTANT_RUNTIME_MODE=on
ENABLE_LEGACY_AGENT_FALLBACK=false
```

The normal Runtime success path does not instantiate or call
`LegacyAgentService`. Runtime failure is not automatically converted into a
Legacy execution.

## 7. Removal Sequence

### D4-C1

Implement and authorize `GET /executions` without using `assistant_runs` as a
state source.

### D4-C2

Migrate the frontend list/index flow and add execution-list contract tests.

### D4-C3

Retire `/runs` mapped control/event consumers; keep only Legacy history
queries and compatibility tests.

### D4-C4

Retire the emergency fallback and remove active imports of
`LegacyAgentService`/`agent.py`.

### D4-C5

Archive `community-assistant-agent`, then handle `RunRepository` and
`assistant_runs` through a separate data retention and schema migration plan.

## 8. Result

No safe `DELETE_NOW` candidate exists. The next concrete dependency-removal
step is the authorized execution list API; until it exists, the frontend's
compatibility index and the Runtime-to-legacy ID bridge must remain in place.
