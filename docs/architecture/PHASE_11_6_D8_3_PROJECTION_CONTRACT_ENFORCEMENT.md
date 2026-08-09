# Phase 11.6-D8.3 assistant_runs Projection Contract Enforcement

## 1. New responsibility

`assistant_runs` is a Legacy history projection. Runtime-backed requests may
leave a history row for `run_id` lookup and response content, but the row is
not an execution record and is never a second Runtime state store.

## 2. Runtime source of truth

For a `run_id` mapped by `RunExecutionAdapter`, execution state is read from
`PlanExecution` through `ExecutionStateManager`. Runtime events are read from
`ExecutionEventStore`. The mapping stores identifiers only.

## 3. Allowed Runtime projection fields

Runtime-backed writes may contain only:

```text
run_id, conversation_id, user_id, tenant_id, content, trace_id
```

`status`, `events`, `error_code`, `error_message`, `tool_rounds`,
`partial_results`, `progress`, `current_step`, `execution_state`, `checkpoint`,
and `retry_state` are forbidden, including `None`, empty arrays, and fake
marker values such as `RUNTIME_BACKED` or `UNKNOWN`.

## 4. Repository write boundary

`RunRepository.create()` and `RunRepository.update()` require an explicit
`_legacy_projection` flag. `False` validates the Runtime metadata allowlist;
`True` preserves the historical status, event, error, and tool-round fields.
The in-memory compatibility repository applies the same validation.

The database migration
`001_assistant_runs_history_projection.sql` makes `assistant_runs.status`
nullable, so Runtime projection creation does not need a fake status.

## 5. Read boundary

`GET /runs/{run_id}` uses Runtime execution status and steps when the adapter
resolves an `execution_id`, and does not expose Runtime status, errors, or
tool-round values from the legacy row. An unmapped Legacy-only run continues
to use `assistant_runs` history fields.

## 6. Test coverage

`tests/unit/test_assistant_runs_projection_contract.py` covers Runtime writes,
fake-value rejection, Legacy writes, mapped Runtime reads, Legacy-only reads,
and nullable database status with both Runtime and Legacy rows.

## 7. Future retirement plan

Keep `assistant_runs`, `RunRepository`, and `RunExecutionAdapter` until Legacy
history retention, compatibility reads, and migration rollback requirements
are complete. Once no Legacy consumers remain, archive history and retire the
projection in a separate migration. Runtime state must remain exclusively in
the Execution Runtime throughout that process.
