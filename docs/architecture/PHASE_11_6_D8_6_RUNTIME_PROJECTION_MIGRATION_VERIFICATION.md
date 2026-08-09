# Phase 11.6-D8.6 Runtime Projection Migration Verification

## Schema status

The Assistant schema declares `assistant_runs.status` as nullable in the
SQLAlchemy metadata. Existing PostgreSQL databases cannot be changed by
`create_all()`, so the versioned SQL migration
`001_assistant_runs_history_projection.sql` applies:

```sql
ALTER TABLE assistant_runs
    ALTER COLUMN status DROP NOT NULL;
```

The Assistant startup now applies files in `db/migrations` in lexical version
order and records them in `assistant_schema_migrations`. This makes a fresh
environment and an existing environment follow the same migration path.

## Runtime guard

When `ASSISTANT_RUNTIME_MODE` is not `off`, startup queries
`information_schema.columns`. If `assistant_runs.status` is absent or is not
nullable, startup fails with:

```text
Runtime projection schema mismatch: assistant_runs.status must be nullable before enabling Runtime mode.
```

Legacy mode skips this Runtime-only check.

## Projection boundary

Runtime-backed writes to `assistant_runs` contain only `run_id`,
`conversation_id`, `user_id`, `tenant_id`, `content`, and `trace_id`.
Runtime status, events, errors, tool rounds, and partial results remain owned
by `PlanExecution`, `ExecutionStateManager`, and `ExecutionEventStore`.

`assistant_runs` remains a Legacy history projection. It is not a Runtime
repository and no default or fabricated Runtime status is written.

## Verification

Unit coverage validates nullable, not-null, and Legacy-mode guard behavior.
The PostgreSQL smoke test runs when
`ASSISTANT_PROJECTION_TEST_DATABASE_URL` is configured and verifies a Runtime
metadata insert leaves `status` NULL. The migration must be applied before
enabling Runtime mode in an existing deployment; the startup runner now makes
that application automatic.
