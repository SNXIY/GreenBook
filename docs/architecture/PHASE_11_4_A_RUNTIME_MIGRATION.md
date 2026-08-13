> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.4-A Legacy API Runtime Migration

## Scope

This phase reduces Legacy API dependence on legacy execution while retaining
all legacy paths and `community-assistant-agent`.

Protected components were not modified:

- IntentSpec and Validator
- Planner and TaskPlan
- Worker
- ToolRuntime
- ExecutionStateManager core logic
- `community-assistant-agent/`

## 1. Audit Before Implementation

### Existing API boundary

`apps/assistant_api/greenbook_assistant_api/api/routes.py` already contained
the compatibility boundary helpers:

- `_run_execution_adapter(request)` resolves the configured ID mapping;
- `_run_operation_adapter(request)` constructs an operation adapter around the
  existing `ExecutionStateManager` and event store;
- `RunResponse` exposes `execution_id` and `ExecutionReference`.

The route behavior before this phase was mixed:

| Operation | Runtime-backed run | Legacy-only run |
| --- | --- | --- |
| `GET /runs/{run_id}` | Read legacy record and expose mapping; status was primarily record-derived | Legacy repository |
| `GET /runs/{run_id}/events` | Already read canonical execution events when mapped | Legacy event list |
| `GET /runs/{run_id}/events/stream` | Already streamed canonical execution events when mapped | Legacy event list stream |
| `POST /runs/{run_id}/cancel` | Already delegated to Runtime state | Legacy repository update |
| `POST /runs/{run_id}/interrupt` | Updated legacy repository directly | Legacy repository update |
| `POST /runs/{run_id}/resume` | No compatibility route existed | No route existed |

### Runtime compatibility components

- `compatibility/history/run_execution_link.py` owns only the persisted or
  injected `run_id` to `execution_id` relationship.
- `compatibility/runtime/run_operation_adapter.py` delegates cancel, pause,
  resume, event listing, and event streaming to the canonical state manager
  and event store.
- `ExecutionStateManager` remains the only state mutation entrypoint.
- `PlanExecution` remains the execution source of truth.

### Legacy-only fallback

When there is no link or no configured Runtime state manager, routes continue
using `RunRepository` and the existing legacy event records. No new execution
is created for a legacy-only run.

## 2. Implementation

### Operation adapter

Added `RunOperationAdapter.interrupt_run(run_id)`. For a mapped execution it
delegates to `pause_run()`, which resolves the execution ID and calls
`ExecutionStateManager.pause_execution()`.

This gives `interrupt` a resumable Runtime meaning:

```text
legacy run_id
    -> RunExecutionAdapter
    -> execution_id
    -> ExecutionStateManager.pause_execution()
    -> PlanExecution.PAUSED
```

The adapter does not copy state, events, or step records.

### Legacy API routes

Updated `apps/assistant_api/greenbook_assistant_api/api/routes.py`:

- `POST /runs/{run_id}/interrupt`
  - mapped run: delegates to Runtime pause;
  - legacy-only run: keeps the existing legacy cancellation/event behavior.
- `POST /runs/{run_id}/resume`
  - mapped run: resolves the execution and delegates to Runtime resume;
  - legacy-only run: returns the existing legacy record without fabricating a
    Runtime execution.
- `POST /runs/{run_id}/cancel`
  - existing mapped-run Runtime delegation remains in place;
  - legacy-only behavior remains unchanged.
- `GET /runs/{run_id}/events` and
  `GET /runs/{run_id}/events/stream`
  - existing mapped-run canonical EventStore behavior remains in place;
  - legacy-only behavior remains unchanged.

Responses for mapped control operations expose the canonical Runtime status
when a state transition returns an execution. The legacy `RunResponse` shape
and route paths remain intact.

## 3. State and Event Flow

### Cancel

```text
POST /runs/{run_id}/cancel
    -> resolve execution_id
    -> ExecutionStateManager.cancel_execution(execution_id)
    -> PlanExecution.CANCELLED
    -> canonical execution events
```

### Interrupt and resume

```text
POST /runs/{run_id}/interrupt
    -> resolve execution_id
    -> ExecutionStateManager.pause_execution(execution_id)
    -> PlanExecution.PAUSED

POST /runs/{run_id}/resume
    -> resolve execution_id
    -> ExecutionStateManager.resume_execution(execution_id)
    -> PlanExecution.RUNNING
```

The distinction is intentional: an explicit `cancel` remains terminal,
whereas a mapped `interrupt` is now a resumable control operation. Legacy-only
runs preserve their prior route behavior.

### Events and SSE

The event routes do not create a second event stream:

```text
run_id
  -> RunExecutionAdapter
  -> execution_id
  -> ExecutionEventStore / subscribe_execution_events
```

Legacy-only runs continue reading their historical event list. This keeps
historical compatibility without copying legacy events into the Runtime
store.

## 4. Files Changed

- `packages/assistant_core/greenbook_assistant_core/compatibility/runtime/run_operation_adapter.py`
  - added the explicit `interrupt_run()` delegation.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  - routed mapped interrupt operations through Runtime pause;
  - added mapped/legacy-compatible resume endpoint.
- `tests/compat/runtime/test_run_operation_adapter.py`
  - added coverage for interrupt-to-pause behavior.
- `docs/architecture/PHASE_11_4_A_RUNTIME_MIGRATION.md`
  - this audit and implementation report.

No changes were made to `community-assistant-agent/`, Worker, Planner,
ToolRuntime, IntentUnderstanding, Validator, PlanExecution, or
ExecutionStateManager.

## 5. Remaining Migration Work

- Bind every Runtime-created API run to its actual `PlanExecution` using the
  production-persistent link repository rather than a request-local default.
- Add API-level tests for mapped and legacy-only cancel, interrupt, resume,
  event history, and SSE behavior.
- Migrate approval decisions to the Runtime human-interaction lifecycle while
  retaining old approval routes.
- Migrate CI, P0, scripts, and deployment checks away from the standalone
  legacy process before retiring `LegacyAgentService`.
- Define historical `assistant_runs` read/retention policy.

## 6. Verification

Added unit coverage verifies that a mapped legacy interrupt delegates to
Runtime pause. Full test execution is not included in this documentation
phase report; existing worktree changes and environment dependencies should be
validated separately before merging.

## Decision

Phase 11.4-A is implemented as an API compatibility migration. Mapped runs
now prefer canonical Runtime control for cancel, interrupt, resume, events,
and SSE, while legacy-only runs retain their old behavior. No legacy
application or execution-core component was removed.
