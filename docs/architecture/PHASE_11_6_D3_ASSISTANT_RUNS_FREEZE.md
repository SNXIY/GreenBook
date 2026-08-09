# Phase 11.6-D3 assistant_runs Freeze

## 1. Freeze Boundary

`PlanExecution` is the canonical execution record. Runtime status and step
state are read through `ExecutionStateManager`; execution history is read from
`ExecutionEventStore`. `assistant_runs` is retained only as a compatibility
history/projection for legacy clients.

The freeze does not delete `assistant_runs`, `RunRepository`, the legacy
`/runs/{run_id}` endpoints, or the `RunExecutionAdapter`.

## 2. Write Audit

The repository write points are:

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
  - `RunRepository.create()` inserts `assistant_runs` rows.
  - `RunRepository.update()` updates legacy rows.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  - `send_message()` creates the initial row.
  - Legacy-only cancel and interrupt retain their existing row updates.

Runtime-backed `send_message()` requests now write a projection with:

- `run_id`, user/session/conversation metadata, and `trace_id`;
- `execution_id` as the compatibility link;
- user-visible content, tool-round summary, approval metadata, and partial
  result metadata where present;
- `status="RUNTIME_BACKED"` as a non-authoritative marker;
- an empty `events` collection and no copied Runtime error fields.

The marker is intentionally not interpreted as current execution state. No
Runtime event list or Runtime status is written as the source of truth in
`assistant_runs`.

Legacy-only results continue to persist their historical status, events, and
error fields so existing emergency compatibility behavior remains intact.

## 3. `/runs` Compatibility API

For a mapped Runtime run, the API resolves:

```text
run_id -> RunExecutionLink -> execution_id -> PlanExecution / EventStore
```

The following behavior applies:

| API surface | Runtime-backed source | Legacy-only source |
| --- | --- | --- |
| `GET /runs/{run_id}` status | `PlanExecution.status` | `assistant_runs.status` |
| `GET /runs/{run_id}` steps | `PlanExecution.steps` | legacy event projection |
| `GET /runs/{run_id}/events` | `ExecutionEventStore` | `assistant_runs.events` |
| `/runs/{run_id}/events/stream` | Runtime event stream | legacy stored events |
| cancel / interrupt / pause / resume | `ExecutionStateManager` through adapter | `RunRepository` legacy behavior |
| `GET /runs` status and steps | mapped `PlanExecution` | legacy row |

The old `run_id` contract remains at the API boundary. Runtime UI and direct
Runtime APIs continue to use `execution_id`; `run_id` is only a compatibility
reference for mapped or legacy requests.

## 4. Scope and Non-Changes

This phase changes only the API projection and regression coverage. It does
not modify `Worker`, `Planner`, `ToolRuntime`, `ExecutionStateManager`,
`PlanExecution`, database schema, migrations, or the legacy repositories.

## 5. Verification

- `tests/unit/test_assistant_runs_freeze.py`: covers Runtime projection
  freezing, Legacy projection preservation, mapped status/steps, and mapped
  event lookup.
- `tests/compat/runtime`: 21 passed.
- `python -m compileall -q apps/assistant_api/greenbook_assistant_api packages/assistant_core/greenbook_assistant_core`: passed in the project virtual environment.
- `git diff --check`: passed.

With the project `.venv`, the new freeze tests passed (`3 passed`) and the
evaluation suite passed (`44 passed, 1 skipped`). The unit suite completed with
`511 passed, 1 failed`; the failure is the existing
`test_revision_success_then_schedule_failure_is_partial_failure`, which
returns HTTP 502 instead of its expected 409 before the Runtime projection is
written. The system Anaconda interpreter cannot collect API tests because it
does not provide `fastapi`.
