# Phase 11.6-D8 Data Layer Retirement Audit

## Scope

This is a read-only audit. No database, schema, migration, Worker, Planner,
ToolRuntime, ExecutionStateManager, or PlanExecution changes were made.

Canonical execution data remains:

```text
PlanExecution -> ExecutionStateManager -> ExecutionEventStore
```

The compatibility boundary remains:

```text
assistant_runs / RunRepository <- legacy history
run_execution_link / RunExecutionAdapter <- ID mapping only
```

`assistant_runs` is not safe to delete yet. It is still written at the API
boundary and read by legacy history endpoints.

## 1. Inventory and classification

| Item | Evidence | Role | Decision |
| --- | --- | --- | --- |
| `assistant_runs` | `packages/assistant_core/greenbook_assistant_core/db/repositories.py:55` | Legacy request/history projection | KEEP temporarily |
| `RunRepository` | `.../db/repositories.py:284`, `apps/assistant_api/.../routes.py:302-359` | CRUD wrapper for the legacy table | KEEP temporarily |
| `run_id` | `RunResponse`, `/runs`, E2E/history references | Legacy external identifier | KEEP at compatibility boundary |
| `RunExecutionLink` | `.../compatibility/history/run_execution_link.py:31` | Legacy ID to canonical execution ID | KEEP while legacy history exists |
| `run_execution_link` | `.../compatibility/history/run_execution_repository.py:74` | Persistent ID mapping metadata | KEEP while adapter exists |
| `assistant_task_intents.run_id` | `.../task/registry.py:49` | Historical intent correlation | MIGRATE after consumers are confirmed |
| `last_successful_run_id` | `.../db/repositories.py:35` | Conversation history pointer | MIGRATE candidate |
| `assistant_approvals.run_id` | `.../db/repositories.py:80` | Legacy approval correlation | MIGRATE reads; retain nullable compatibility field |

### KEEP

- Legacy `run_id`, user/session/tenant metadata, prompt/content, trace and
  result fields required by `GET /runs/{run_id}`.
- `RunRepository.find_by_id()` and the minimal legacy history contract.
- `RunExecutionAdapter` and `RunExecutionLink` while a legacy response needs
  a stable execution reference.
- Legacy-only approval correlation and historical migrations until retention
  ownership is explicit.

### MIGRATE

- Runtime status, current step, progress, retry/error state: read only from
  `PlanExecution` and the Execution API.
- Runtime events and SSE: read only from `ExecutionEventStore`.
- Runtime approval control: resolve `run_id` to `execution_id`, then use the
  execution approval service.
- ACTIVE list/index consumers: replace `GET /runs` with the execution list API.
- `assistant_task_intents.run_id` and conversation run pointers after all
  consumers are mapped to task/execution references.
- E2E/scripts where `run_id` is still a primary variable; retain it only for
  history assertions.

### DELETE_CANDIDATE

Candidates only; nothing is deleted in D8:

- `RunRepository.update()` after a complete caller scan confirms no legacy
  update path remains.
- Embedded `assistant_runs.events` for Runtime-backed rows after old history
  clients no longer require them.
- `assistant_runs.status`, `error_code`, and `error_message` as Runtime
  projections after legacy-only history semantics are frozen.
- Duplicated tool/timing/result fields after export and UI consumers are
  accounted for.
- Unreferenced task run pointers after data-retention review.
- Orphan `run_execution_link` rows after a reversible reconciliation job
  exists.

## 2. Read and write paths

### Writes

`_create_run()` at
`apps/assistant_api/greenbook_assistant_api/api/routes.py:533` delegates to
`RunRepository.create()`. The call site is the send-message path at `:1119`.
`_run_projection_fields()` at `:538` creates a compatibility projection for
Runtime requests and preserves legacy fields for non-Runtime results.

`RunRepository.update()` is defined at
`packages/assistant_core/greenbook_assistant_core/db/repositories.py:326`.
No ACTIVE Python caller was found for it in the scanned API, services,
scripts, or tests. It is a deletion candidate only after the history write
contract is retired.

### Reads

| Path | Current source | Target |
| --- | --- | --- |
| `GET /runs` | `find_all_by_user()` at `routes.py:1340` | Execution list for ACTIVE consumers; retain history compatibility |
| `GET /runs/{run_id}` | `find_by_id()` at `routes.py:613,1269` | Link resolution plus Execution API; row supplies history/content only |
| mapped status/steps | `_mapped_execution()` and `_execution_step_views()` | `PlanExecution` / `ExecutionStateManager` |
| runtime events/SSE | Runtime event routes | `ExecutionEventStore` only |
| approval by run | `ApprovalRepository.find_by_run_id()` at `repositories.py:376` | Resolve link, then execution approval service |
| approval by execution | `find_by_execution_id()` at `repositories.py:385` | Canonical lookup |
| task intent by run | `TaskIntentRepository.find_by_run()` at `task/registry.py:118` | Task/execution correlation after consumer audit |

No active Runtime read should use `assistant_runs.status` or
`assistant_runs.events` as execution truth.

## 3. Database analysis

### `assistant_runs`

- Primary key: `run_id` UUID.
- Foreign key: `conversation_id` to
  `assistant_conversations.conversation_id`.
- No foreign key to `PlanExecution` or `run_execution_link`.
- No explicit user/tenant/time index appears in the SQLAlchemy table
  definition; production query/index review is still required.
- `events`, `session_snapshot`, and `partial_results` are JSON compatibility
  payloads, not EventStore data.

### `assistant_approvals`

- Primary key: `approval_id` UUID.
- Foreign key: `conversation_id` to the conversation table.
- `run_id` and `execution_id` are nullable correlation columns without
  declared foreign keys.
- Both lookup methods exist; execution lookup is the canonical direction.

### `run_execution_link`

Defined in
`packages/assistant_core/greenbook_assistant_core/compatibility/history/run_execution_repository.py:74`:

- primary key `run_id`;
- unique nullable `execution_id`;
- conversation/task IDs and mapping source/version/timestamp metadata;
- no status, event, checkpoint, or step payload;
- no foreign keys, so orphan reconciliation is an operational concern.

### Migrations and table creation

Assistant tables are created with SQLAlchemy `metadata.create_all()` by
`db/repositories.py:_ensure_tables()`. The link table is created with its own
`compatibility_metadata.create_all()`. No ACTIVE dedicated migration for
`assistant_runs` or `run_execution_link` was found. Historical migrations in
the archived legacy workspace are not an ACTIVE migration owner.

### Schema mismatch requiring follow-up

The `_runs` table declaration does not contain `execution_id` or `task_id`,
but the Runtime projection and `/runs/{run_id}` response path read/write those
keys. This is a migration defect/risk. D8 does not change it. The approved
follow-up should resolve the link through `RunExecutionAdapter` and read
execution metadata from Runtime, rather than adding a second execution state
source to `assistant_runs`.

## 4. Jobs, cleanup, and retention

No scheduled task or cleanup script targeting `assistant_runs`,
`RunRepository`, or `run_execution_link` was found in ACTIVE scripts,
Assistant worker code, CI, Docker, or backend scheduled services.

The P0 harness cleans its own Redis namespace and local artifacts. Java
cleanup tasks target idempotency/outbox records, not this data boundary.

Operational gaps are therefore explicit: no retention policy, orphan-link
reconciliation, or run archival job currently owns these records.

## 5. Deletion gates

Before any retirement implementation:

1. Remove the hidden Runtime metadata dependency from `assistant_runs` through
   the adapter/Execution API path.
2. Inventory production rows and legacy clients by tenant and retention age.
3. Define indexes and reconciliation tooling with a database owner.
4. Prove ACTIVE frontend, scripts, E2E, approval, events, and SSE consumers use
   `execution_id`.
5. Define export, rollback, retention, and deletion windows.

No table, migration, index, foreign key, or cleanup job is approved for
deletion in this phase.
