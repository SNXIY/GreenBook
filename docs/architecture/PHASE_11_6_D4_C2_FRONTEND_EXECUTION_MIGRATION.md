> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-C2 Frontend Execution Migration

## 1. ACTIVE list source

The ACTIVE TaskCenter list now reads Runtime metadata through:

```text
TaskCenterPage
  -> assistantService.listExecutions()
  -> executionService.listExecutions()
  -> GET /executions?limit=30
  -> Execution Runtime list API
```

The response is the Runtime-native `{ items, next_cursor }` contract. It is
not discovered through `GET /runs`, `assistant_runs`, or `RunRepository`.

Each ACTIVE row is identified by `execution_id`. The existing TaskCenter view
model is retained as a presentation adapter so the rest of the page can be
migrated incrementally without creating a second execution model.

## 2. Runtime identity and compatibility

Runtime-backed list items contain:

- `execution_id` as the primary identity;
- `task_id`, `plan_id`, status, progress, current step, and timestamps from
  the Runtime list response;
- an empty `run_id` compatibility field because no Legacy Run is required to
  display or operate the Runtime row;
- an `ExecutionReference` whose source is `runtime`.

`run_id` remains available in the shared assistant types and in the existing
Legacy-only response/control fallback. It is not used as the React key or as
the Runtime control identity.

## 3. Controls and visibility

The existing control paths remain execution-based when an execution reference
exists:

| Capability | ACTIVE source |
| --- | --- |
| status | Execution API / `PlanExecution` projection |
| steps | `/executions/{execution_id}/steps` |
| events | `/executions/{execution_id}/events` |
| SSE | `/executions/{execution_id}/stream` |
| pause/resume/cancel | Runtime control endpoints |
| retry | Runtime step retry endpoint |
| approval | Execution-aware approval endpoint |

The Legacy `/runs` paths remain only for a response that has no
`execution_id`. They are not used to control mapped Runtime executions.

## 4. Frontend changes

- Added `ExecutionListItem` and `ExecutionListResponse` types.
- Added `executionService.listExecutions()` with cursor support.
- Replaced the ACTIVE `assistantService.listRuns()` implementation with a
  Runtime execution list adapter and renamed the ACTIVE caller to
  `listExecutions()`.
- Changed TaskCenter assistant row keys to prefer `execution_id`.
- Used Runtime list progress directly, converting the API ratio to the UI
  percentage.
- Added frontend tests for Runtime list rendering, cursor requests, and the
  absence of a Legacy `/runs` list request.

## 5. Deliberately retained

This phase does not delete or alter compatibility infrastructure:

- `assistant_runs`
- `RunRepository`
- `LegacyAgent`
- the Legacy `/runs` API
- Legacy-only history references and fallback behavior

No Worker, Planner, ToolRuntime, ExecutionStateManager, or PlanExecution code
was changed.

## 6. Verification

- Frontend Vitest: `4 passed`.
- Frontend production build (`tsc && vite build`): passed.
- `git diff --check`: passed.

The remaining frontend `/runs` references are limited to Legacy-compatible
single-run lookup, control, and SSE fallback code; no ACTIVE list consumer
uses that endpoint.
