# Phase 11.6-D8.2 Projection Write Contract Migration

## Result

Runtime-backed requests now write only Legacy history metadata to
`assistant_runs`. Runtime execution state remains exclusively in
`PlanExecution`/`ExecutionStateManager`, and Runtime events remain in
`ExecutionEventStore`.

Runtime projection fields are limited to:

```text
run_id
conversation_id
user_id
tenant_id
content
trace_id
```

The projection no longer writes:

```text
status
events
error_code
error_message
tool_rounds
partial_results
```

`execution_id` is resolved through `RunExecutionLink`; it is not copied into
the legacy run row as an execution state field.

## Implementation

- `routes.py::_run_projection_fields()` returns only history content for the
  Runtime path.
- `_create_run()` passes an internal `_legacy_projection` marker only to the
  database repository.
- `RunRepository.create()` adds legacy defaults (`status`, `events`, and
  `tool_rounds`) only when that marker is true.
- The in-memory compatibility repository drops the internal marker and never
  exposes it as stored history.
- Legacy execution paths retain their historical status, event, error, and
  tool-round fields.

## Database migration

Added:

```text
packages/assistant_core/greenbook_assistant_core/db/migrations/
001_assistant_runs_history_projection.sql
```

It executes:

```sql
ALTER TABLE assistant_runs
    ALTER COLUMN status DROP NOT NULL;
```

This removes the schema blocker without using a fake Runtime status. The
migration must be applied to the Assistant PostgreSQL database before
Runtime-backed writes are enabled against an existing database. The
SQLAlchemy `create_all()` path does not alter an existing column, so applying
the migration is an operational prerequisite.

## Read contract

For a mapped Runtime request:

- status comes from `GET /executions/{execution_id}`;
- steps come from `GET /executions/{execution_id}/steps`;
- events come from `GET /executions/{execution_id}/events`;
- SSE comes from `GET /executions/{execution_id}/stream`.

The legacy `/runs/{run_id}` response resolves the execution through the
adapter and uses the Runtime execution for mapped status and steps. Legacy row
events are only applicable to legacy-only records.

## Tests and verification

Updated `tests/unit/test_assistant_runs_freeze.py` to assert that Runtime
projection output contains no Runtime status, event, error, tool-round, or
partial-result fields while legacy projection behavior remains unchanged.

Verification:

- ACTIVE Python compileall: passed;
- `git diff --check`: passed;
- targeted pytest collection is blocked in the current system interpreter by
  missing `fastapi`; no Runtime logic failure was observed.

No `assistant_runs` table was deleted, and Worker, Planner, ToolRuntime,
ExecutionStateManager, and PlanExecution were not modified.
