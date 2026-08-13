> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D8.1 Assistant Runs Projection Freeze

## 1. Decision

`assistant_runs` is classified as a Legacy history projection. It is not an
execution state store, event store, checkpoint store, or progress index.

Canonical Runtime sources remain:

```text
PlanExecution
ExecutionStateManager
ExecutionEventStore
```

The compatibility relationship is:

```text
run_id -> RunExecutionAdapter -> execution_id -> Execution API
```

No database or Runtime code was modified in this phase.

## 2. Write audit

### History metadata

The send-message path calls `_create_run()` at
`apps/assistant_api/greenbook_assistant_api/api/routes.py:1119`, which
delegates to `RunRepository.create()` in
`packages/assistant_core/greenbook_assistant_core/db/repositories.py:284`.
The permitted projection data is:

- `run_id`;
- `conversation_id`;
- `user_id` and `tenant_id`;
- prompt/content and final display content;
- trace reference;
- legacy approval/history references;
- legacy display metadata needed by `GET /runs/{run_id}`.

### Runtime state and duplicate data

`_run_projection_fields()` currently returns the following Runtime branch at
`routes.py:538`:

```text
execution_id
status = RUNTIME_BACKED
content
tool_rounds
events = []
error_code = None
error_message = None
session_snapshot
approval_id
partial_results
```

Classification:

| Field | Classification | Freeze decision |
| --- | --- | --- |
| `execution_id` | execution correlation | Resolve through `RunExecutionLink`; do not store it as an undeclared run-table state field |
| `status` | runtime state | Must not be written for Runtime-backed rows |
| `events` | event data | Must not be written; use `ExecutionEventStore` |
| `error_code`, `error_message` | runtime failure state | Must not be written for Runtime-backed rows |
| `tool_rounds` | duplicate execution metric | Must not be used as Runtime state; retain only where legacy history explicitly requires it |
| `content` | history/display metadata | Allowed |
| `session_snapshot`, `partial_results` | legacy history payload | Allowed only when not treated as Runtime state |
| `approval_id` | compatibility reference | Allowed as a legacy reference; approval status comes from the approval/Execution path |

`RunRepository.update()` is defined at `repositories.py:326`; no ACTIVE caller
was found in the scanned API, services, scripts, or tests. It must not be
reintroduced for Runtime status or event updates.

## 3. Schema blocker

The current `_runs` table declares `status` as `nullable=False` and does not
declare `execution_id` or `task_id`. Therefore the strict rule “do not write
Runtime status” cannot be implemented safely by simply removing `status` from
the existing insert: the current database contract requires a value.

This is an explicit migration blocker, not a reason to write a fake Runtime
status. A future approved data migration must choose one of these approaches:

1. make the legacy projection insert/history contract nullable and store only
   allowed history metadata; or
2. introduce a separate history-only representation with an explicit
   compatibility migration and rollback plan.

Neither approach is executed in D8.1 because schema changes are prohibited.
`assistant_runs` must not receive `RUNTIME_BACKED`, current status, progress,
step state, or Runtime error data as a workaround.

## 4. Runtime read audit

### Status

`GET /executions/{execution_id}` in
`apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py` reads from
`RuntimeManager` and `ExecutionStateManager`. `/runs/{run_id}` calls
`_mapped_execution()` and returns `runtime_execution.status` when a link
exists. It must never fall back to the legacy row's status for a mapped
Runtime execution.

### Steps

`_execution_step_views()` derives mapped run response steps from the canonical
execution steps. The native endpoint
`GET /executions/{execution_id}/steps` reads through the Runtime manager.
Legacy `assistant_runs.events` must not be parsed into steps for a mapped
Runtime execution.

### Events and SSE

Native history and streaming endpoints are:

- `GET /executions/{execution_id}/events`;
- `GET /executions/{execution_id}/stream`.

They use `ExecutionEventStore` and the execution event subscription. The
legacy run event/stream control endpoints are retired. For a mapped Runtime
run, `assistant_runs.events` must remain empty/unused and must not be used as a
fallback event source.

### Approval

Approval may retain `run_id` for legacy compatibility, but a Runtime approval
must resolve to `execution_id`. Approval status and resume behavior must not be
projected into `assistant_runs.status` or `assistant_runs.events`.

## 5. Projection contract

### Allowed for Runtime-backed requests

```text
run_id
conversation_id
user_id / tenant_id
prompt/content used for history display
trace reference
legacy reference fields
```

### Forbidden for Runtime-backed requests

```text
Runtime status
execution progress
current step or step state
retry count as execution state
Runtime error code/message
ExecutionEvent payloads
SSE cursor/event state
checkpoint data
```

The `execution_id` is an identity reference and must be resolved through the
compatibility link. It does not make `assistant_runs` an execution state
source.

## 6. Required follow-up before implementation

1. Resolve the non-null `assistant_runs.status` schema conflict through an
   approved migration or history-only write contract.
2. Add a projection contract test asserting that Runtime-backed writes contain
   no Runtime status, event, progress, step, or error fields.
3. Add a database integration test against the deployed schema, rather than
   relying only on SQLAlchemy `create_all()`.
4. Verify `/runs/{run_id}` uses the adapter for identity and Runtime API for
   all mapped execution fields.
5. Keep legacy-only rows readable until retention and client migration are
   complete.

No source or database mutation was performed by this phase; this document is
the freeze contract and identifies the schema change required for enforcement.
