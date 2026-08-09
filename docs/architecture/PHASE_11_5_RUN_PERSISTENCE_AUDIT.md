# Phase 11.5 Run Persistence Audit

## Scope and Decision

This audit covers:

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
- `RunRepository` and the `assistant_runs` table definition
- the legacy migration chain
- Assistant API routes and `LegacyAgentService`
- SSE/event routes
- approval, cancel, interrupt, and resume flows

Protected components were not changed:

- Worker
- Planner
- ToolRuntime
- ExecutionStateManager
- database tables and migrations

**Decision: `RunRepository` and `assistant_runs` cannot be retired yet.**
The Runtime has a canonical `PlanExecution` persistence path, but the API
still writes and reads `assistant_runs` for every Assistant request and the
legacy control/data contracts still depend on `run_id`.

## 1. Current Persistence Models

### `RunRepository`

`packages/assistant_core/greenbook_assistant_core/db/repositories.py` defines
`RunRepository` over the `assistant_runs` table with:

- `create(**fields)`;
- `find_by_id(run_id)`;
- `find_all_by_user(user_id, tenant_id, limit)`;
- optimistic-version `update(run_id, **fields)`.

The table stores:

- run/conversation/user/tenant identifiers;
- status and final content;
- error code/message;
- tool round count and trace ID;
- an embedded JSON `events` list;
- approval ID, session snapshot, and partial results;
- version and created timestamp.

This is a legacy turn/result projection, not the canonical Runtime execution
model. It still has active API responsibilities.

### Canonical Runtime persistence

The Runtime uses `PlanExecution`, `StepExecution`, ExecutionRepository,
EventStore, CheckpointStore, and execution persistence keyed by
`execution_id`. These models contain execution status, steps, retries,
checkpoints, leases, and canonical execution events.

The intended relationship is:

```text
PlanExecution / ExecutionEvent / Checkpoint
  -> canonical execution state

RunExecutionLink
  -> compatibility-only run_id <-> execution_id mapping

assistant_runs
  -> legacy API/result/history projection during migration
```

The compatibility link must not copy status, steps, events, or checkpoints.

## 2. Does New Code Still Write `assistant_runs`?

**Yes.**

In `apps/assistant_api/greenbook_assistant_api/api/routes.py`:

1. `send_message()` creates a `run_id` at request entry.
2. It calls `AssistantService`, which may route to Runtime.
3. When the result returns, the route unconditionally calls `_create_run(...)`.
4. `_create_run()` delegates to `repos.runs.create(...)`.
5. With a database session, `repos.runs` is `RunRepository`; therefore the
   result is persisted into `assistant_runs` even for a Runtime execution.

The same route also persists message/session data and approval records. This
means the current system is not yet Runtime-only persistence. The table is a
dual-path API projection for both legacy and Runtime results.

### Runtime ID binding gap

The route attempts to bind a compatibility link only when
`ctx.execution_id` is populated. However, the audited
`RuntimeAgentService` creates the `PlanExecution` internally and uses its
execution ID for the worker, trace, and executor. The request context is not
consistently updated with that ID before the route's binding check.

Consequences:

- some Runtime requests can write `assistant_runs` without a persisted
  `run_id -> execution_id` link;
- `RunResponse.execution_id` may be absent unless a record or link supplies
  it;
- legacy API control/event routes cannot reliably delegate to Runtime for
  every Runtime-created run.

This is a migration blocker, not a reason to modify the execution core in this
audit.

## 3. API Dependencies on `run_id`

### Assistant API routes

`apps/assistant_api/greenbook_assistant_api/api/routes.py` currently uses
`run_id` for:

- request/run creation and `RunResponse`;
- `_create_run()` and `_find_run()`;
- `GET /api/v1/assistant/runs`;
- `GET /api/v1/assistant/runs/{run_id}`;
- `GET /api/v1/assistant/runs/{run_id}/events`;
- `GET /api/v1/assistant/runs/{run_id}/events/stream`;
- `POST /api/v1/assistant/runs/{run_id}/cancel`;
- `POST /api/v1/assistant/runs/{run_id}/interrupt`;
- `POST /api/v1/assistant/runs/{run_id}/resume`;
- approval persistence and lookup;
- session `last_successful_run_id`;
- task/intent persistence and trace/event payloads.

The routes now prefer the `RunOperationAdapter` for mapped Runtime operations,
but first call `_find_run()` against the legacy repository. Therefore even a
mapped Runtime control request still requires the corresponding
`assistant_runs` record for route ownership and response construction.

### `RunResponse`

`RunResponse` exposes both:

- legacy `run_id`;
- optional `execution_id`;
- optional `ExecutionReference`.

This is an intentional compatibility contract. Removing `run_id` requires an
API versioning or deprecation plan and client migration, not a repository-only
change.

## 4. LegacyAgentService Dependencies

`apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`:

- imports and executes `CommunityOperationsAssistant`;
- emits events containing `run_id`;
- passes `ctx.run_id` as `agent_run_id` to MCP tools;
- returns `RuntimeResult` with legacy execution metadata;
- participates in approval creation through `approval_data`;
- is still constructed by `main.py` and retained by `AssistantService` as the
  compatibility fallback.

`LegacyAgentService` itself does not directly call `RunRepository`; the API
route persists its result. Its output contract nevertheless requires the
legacy run ID for event identity, approval association, and response mapping.

It remains COMPATIBILITY and cannot be retired while the API fallback and
legacy API route family remain supported.

## 5. SSE and Event Persistence

### Legacy event path

The legacy `assistant_runs.events` JSON field is populated from the
`RuntimeResult.events` list by API request persistence. Legacy event routes
read this list when no `run_id -> execution_id` mapping is available:

- `GET /api/v1/assistant/runs/{run_id}/events` returns an SSE-formatted
  historical stream;
- `GET /api/v1/assistant/runs/{run_id}/events/stream` streams the stored legacy
  event list.

### Runtime event path

When a link exists and Runtime state is configured, the routes delegate to
`RunOperationAdapter`, which reads the canonical Execution EventStore and
polls the canonical execution stream. This is the correct target path.

### Current limitation

Because `_find_run()` still requires `assistant_runs` before either branch,
the Runtime event path is not independent of the old table. The migration
must separate authorization/reference resolution from legacy result lookup
before `assistant_runs` can become read-only.

No event data should be copied from the canonical EventStore into the legacy
JSON field as a substitute for a link. The old field can remain a historical
projection during the compatibility window.

## 6. Approval, Cancel, Interrupt, and Resume

### Approval

The current API persists approvals through `ApprovalRepository`, whose schema
contains a `run_id` column. Approval creation receives `run_id` from the
request route even when the execution is Runtime-backed.

The current approval flow therefore depends on:

- `assistant_runs` for run ownership and lookup;
- `assistant_approvals.run_id` for association;
- conversation `pending_approval` state;
- LegacyAgentService `approval_data` for legacy requests;
- Runtime human-interaction state for Runtime requests.

Migration requires an execution-based approval reference while preserving
legacy run lookup. Approval decisions must resolve the execution link before
delegating to Runtime human interaction.

### Cancel

- mapped run: `RunOperationAdapter.cancel_run(run_id)` delegates to
  `ExecutionStateManager.cancel_execution(execution_id)`;
- legacy-only run: route updates `assistant_runs` and appends a legacy event.

The route still calls `_find_run()` first, so the table remains required for
both ownership and response construction.

### Interrupt

- mapped run: the compatibility adapter maps interrupt to Runtime pause;
- legacy-only run: route preserves the old cancellation/event behavior.

This semantic split is intentional but must be documented to clients before
removing legacy storage.

### Resume

- mapped run: resolves the execution and calls Runtime resume;
- legacy-only run: currently returns the legacy record without fabricating a
  Runtime execution.

Legacy approval resume and Runtime execution resume are related but not yet a
single persistence contract.

## 7. Historical-Only Data

The following fields are likely candidates for historical projection rather
than Runtime source-of-truth data:

| Data | Current location | Likely target/status |
| --- | --- | --- |
| Legacy run status | `assistant_runs.status` | Derive from `PlanExecution.status` for mapped runs; retain for legacy-only history |
| Embedded legacy events | `assistant_runs.events` | Read-only history for legacy-only runs; canonical EventStore for mapped runs |
| `tool_rounds` | `assistant_runs.tool_rounds` | Execution evaluation/trace or derived Runtime metrics |
| `content` / final response | `assistant_runs.content` | Artifact/message/result projection; retain for API history during transition |
| `error_code`, `error_message` | `assistant_runs` | Canonical step/execution error plus compatibility projection |
| `session_snapshot` | `assistant_runs.session_snapshot` | Conversation/session history; not execution state |
| `partial_results` | `assistant_runs.partial_results` | Artifact/result history; map to execution artifacts where possible |
| `approval_id` | `assistant_runs.approval_id` | Execution-based approval reference plus legacy lookup |
| `last_successful_run_id` | `assistant_conversations` | Compatibility pointer; eventually an execution reference |

These fields are not safe to delete until API clients, audit/reporting, and
historical read requirements are classified.

## 8. Migration Feasibility: `run_id` to `execution_id`

### Feasible now at the operation boundary

- Runtime control operations can delegate through `RunExecutionAdapter`.
- Runtime event history and SSE can use canonical EventStore after resolving
  the link.
- New API responses can expose `ExecutionReference` alongside `run_id`.

### Not feasible as a table replacement yet

- Runtime requests still unconditionally create an `assistant_runs` record.
- API ownership lookup requires `_find_run()` before Runtime event/control
  delegation.
- Approval schema and approval route still use `run_id`.
- Legacy fallback and `community-assistant-agent` use run-linked events,
  artifacts, approvals, and database migrations.
- P0/CI/scripts and external Java/MCP contracts still carry run IDs.

### Required target shape

```text
New Runtime request
  -> PlanExecution(execution_id)
  -> ExecutionRepository / EventStore / CheckpointStore
  -> persisted RunExecutionLink(run_id, execution_id)

Legacy API request
  -> resolve run_id
  -> if link exists: Runtime operation/event API
  -> else: legacy repository and legacy events
```

`assistant_runs` should become a compatibility projection/read model, not a
second execution state source.

## 9. Migration Gates

Before changing `RunRepository` from active write model to compatibility
projection, all of the following must pass:

1. Every Runtime-created execution receives a persisted link with the real
   `PlanExecution.execution_id`.
2. API authorization can resolve ownership from an execution/reference store
   without requiring a fresh `assistant_runs` read.
3. Runtime status, steps, events, approval, cancel, interrupt, resume, retry,
   and SSE endpoints have API-level tests.
4. Legacy-only runs still resolve through `RunRepository` and remain readable.
5. Approval data has an execution-based association and historical run lookup.
6. `RunResponse`/`ExecutionReference` migration is documented for frontend,
   Java client, MCP, and external consumers.
7. New Runtime paths stop writing embedded execution events into
   `assistant_runs.events`, or clearly mark that field as a compatibility
   projection.
8. CI, Docker, P0, scripts, and deployment health checks no longer require
   the legacy application for Runtime operation.
9. Historical retention and rollback policies for `assistant_runs`, approvals,
   events, and artifacts are approved.

## 10. Final Classification

| Area | Classification | Recommendation |
| --- | --- | --- |
| `RunRepository` | COMPATIBILITY / ACTIVE API persistence | KEEP; migrate writes gradually |
| `assistant_runs` | COMPATIBILITY projection plus legacy source | KEEP; no schema deletion |
| `assistant_runs.events` | Historical/legacy event storage | Keep read path for legacy-only runs; prefer EventStore for mapped runs |
| API `run_id` routes | COMPATIBILITY public contract | Keep while clients migrate |
| `RunExecutionAdapter` | ACTIVE migration boundary | Complete persistent link coverage |
| Runtime `PlanExecution` persistence | ACTIVE canonical execution state | Preserve as source of truth |
| Legacy migrations | Historical/runtime compatibility schema | KEEP until data retention/migration is complete |
| Approval `run_id` association | COMPATIBILITY | Add execution reference before retirement |

## Final Decision

The old Run persistence layer is partially replaceable at the control and
event operation boundary, but it is not yet replaceable as an API persistence
contract. New code still writes `assistant_runs`, and several public,
approval, event, CI, and historical paths still require `run_id`.

No database table, migration, Worker, Planner, ToolRuntime, or
ExecutionStateManager code was modified by this audit.
