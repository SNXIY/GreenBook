# Phase 11.3 Legacy Agent Audit

## Scope and Conclusion

This audit covers:

- `packages/assistant_core/greenbook_assistant_core/agent.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `community-assistant-agent/`

It checks Python imports, API routes, CI, Docker/development scripts, tests,
database ownership, `assistant_runs`, approval, and SSE/event flows.

**Conclusion: Legacy Agent cannot be retired.** It remains a live API fallback
and an independently runnable application with its own persistence and
control-plane contracts. The ACTIVE Runtime is the formal path, but the
legacy boundary has not reached the retirement gate defined in
`COMPATIBILITY_RETIREMENT_PLAN.md`.

No files were deleted or moved, and no Runtime, Planner, Worker, ToolRuntime,
or ExecutionStateManager code was modified.

## 1. Component Inventory

| Component | Role | Current classification | Retirement risk |
| --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | `CommunityOperationsAssistant` legacy orchestration and tool-routing implementation | COMPATIBILITY / LEGACY | High |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | API service wrapper around the legacy Agent | COMPATIBILITY | Very high |
| `community-assistant-agent/` | Standalone legacy Assistant API/worker, database model, migrations, events, approvals, and tests | COMPATIBILITY / LEGACY | Very high |

## 2. Python Import and Service Wiring

### `agent.py`

The legacy implementation is imported by:

- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
  through the `CommunityOperationsAssistant` compatibility wrapper;
- `tests/integration/test_assistant_runtime_contracts.py`, which directly
  constructs and exercises `CommunityOperationsAssistant`.

The file also contains legacy tool-routing behavior for content creation,
revision, publishing, and community operations. This is not the ACTIVE
IntentSpec -> Planner -> TaskPlan path.

### `LegacyAgentService`

The service remains directly wired into the API application:

- `apps/assistant_api/greenbook_assistant_api/main.py` imports and constructs
  `LegacyAgentService` during application setup;
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
  stores it, exposes `_execute_legacy()`, and falls back to it when Runtime
  execution is unavailable or fails;
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` performs a lazy
  import and constructs a legacy service for a compatibility route.

The service records `execution_path="legacy"` in its legacy results. That
field is evidence that dual-mode routing is still intentional and active.

### Decision

`agent.py` and `LegacyAgentService` are **KEEP / MIGRATE**, not delete
candidates. The direct import and fallback must be removed only after the API
has a tested Runtime-only behavior and an explicit failure policy.

## 3. API Route Audit

The legacy application exposes a separate route family under
`community-assistant-agent/app/main.py`, including:

- `POST /api/v1/assistant/runs/{run_id}/interrupt`
- `POST /api/v1/assistant/runs/{run_id}/cancel`
- `GET /api/v1/assistant/runs/{run_id}/events/stream`
- `GET /api/v1/assistant/runs/{run_id}/events`

The same application creates accepted runs, returns `run_id`, publishes event
URLs, and handles resume/interrupt/cancel behavior. These routes are not yet
proven equivalent to the Runtime API's `execution_id` routes.

The Assistant API also retains the legacy service boundary in
`apps/assistant_api/greenbook_assistant_api/api/routes.py` and
`services/assistant_service.py`. A Runtime-only switch would therefore be an
API contract migration, not a file cleanup.

### API retirement blockers

- Legacy clients may still send and expect `run_id`.
- Approval, interrupt, cancel, event history, and event stream behavior must
  be mapped through the persisted RunExecution compatibility link.
- HTTP status and error behavior must be compared for legacy and Runtime
  paths.
- Authorization and user/tenant ownership checks must remain equivalent.

## 4. CI, Docker, and Scripts

### CI

`.github/workflows/verify.yml` has a job with
`working-directory: community-assistant-agent`. This is a direct CI
dependency and blocks retirement.

### Scripts

The following scripts still reference or start the legacy application:

- `scripts/start-assistant.ps1`
- `scripts/smoke-test.ps1`
- `scripts/setup-dev.ps1`
- `scripts/runtime-report.ps1`
- `scripts/verify-all.ps1`
- `scripts/run_p0_e2e.py`
- `scripts/dev-up.ps1` through the Assistant startup chain

`scripts/run_p0_e2e.py` starts the legacy Assistant process, assigns it a
database/Redis environment, waits for its health endpoint, and records legacy
assistant run IDs. `scripts/test_run_p0_e2e.py` tests that harness.

### Docker and deployment

The repository scan did not establish a separate active Docker service
definition named exclusively for `community-assistant-agent`, but the legacy
application is still a separately runnable Python project with its own
`pyproject.toml`, `uv.lock`, `run_service.py`, and `run_worker.py`. Docker and
compose retirement cannot be approved until the deployment environment is
checked for externally supplied service definitions and operational runbooks.

### Decision

CI and scripts make the legacy application an active operational dependency.
They must be migrated to the ACTIVE API/worker and Runtime health checks
before the application can be archived.

## 5. Database and Persistence Audit

`community-assistant-agent/app/database.py` defines the legacy persistence
model, including:

- `Run` mapped to `assistant_runs`;
- `RunStep` linked by `run_id`;
- `AgentEvent`;
- `Approval` and related approval records;
- artifact/tool and operational records linked to the run.

The legacy migration chain under
`community-assistant-agent/migrations/versions/` modifies `assistant_runs`
and related tables for checkpoints, retry data, execution metadata,
interrupts, approvals, artifacts, and run-linked facts. Examples include:

- `002_harness_controls.py` for checkpoints, budgets, trace data, and
  `assistant_approvals`;
- `003_saga_capabilities.py` for attempts, retries, and side effects;
- `007_orchestration_platform.py` for intent/progress/interrupt fields;
- `009_governed_runtime.py` and `010_adaptive_execution.py` for run metadata
  and indexes;
- `020_execution_reliability.py` for run/step reliability records.

This is a historical data and schema dependency. Deleting the application
without a read-only, retention, or migration policy for `assistant_runs` can
make existing runs, approvals, artifacts, and event history inaccessible.

### Decision

`assistant_runs` and its migrations are **KEEP / MIGRATE**. They cannot be
treated as dead code even if new executions use `PlanExecution`.

## 6. Approval Flow

Approval is implemented in both the legacy application and the current
Runtime/capability boundary, so the flows must not be conflated.

Legacy evidence includes:

- `community-assistant-agent/app/database.py:Approval` and approval-related
  models in `domain.py`;
- `community-assistant-agent/migrations/versions/002_harness_controls.py`
  creating `assistant_approvals`;
- `community-assistant-agent/app/main.py` handling run control around
  approval and resumption;
- `community-assistant-agent/app/worker.py` defining `ApprovalRequired` and
  waiting behavior;
- `packages/security/greenbook_security/approval.py` retaining run-linked
  approval identity;
- `apps/assistant_api/greenbook_assistant_api/main.py` comments and setup
  that delegate approval creation to `LegacyAgentService`.

Before retirement, the migration must prove equivalence for:

- approval request creation and deduplication;
- user/tenant authorization;
- pending approval lookup;
- approval decision and resume;
- rejection/cancellation behavior;
- audit records and idempotency.

The Runtime equivalent should use its existing human/approval and execution
state/event contracts, with `run_id` translated only at the compatibility
boundary. It must not create a second execution state source.

## 7. SSE and Event Flow

The legacy application exposes:

- `GET /api/v1/assistant/runs/{run_id}/events`
- `GET /api/v1/assistant/runs/{run_id}/events/stream`

`community-assistant-agent/app/main.py` builds event URLs in accepted-run
responses and serves event history/streaming. `AgentEvent` is persisted by
the legacy database model and is linked to `run_id`.

The ACTIVE Runtime has its own Execution Event Store and Runtime API keyed by
`execution_id`. Retirement requires an explicit mapping for:

| Legacy event concern | Runtime target |
| --- | --- |
| Run event history | `EventStore.list_events(execution_id)` |
| Live run stream | Runtime SSE/event stream for `execution_id` |
| Run event identity | `ExecutionReference` / RunExecutionAdapter |
| Step/retry/approval events | Execution Event types and payloads |
| Legacy-only run | Read-only legacy event source until retention expires |

No event store may be duplicated during migration. The adapter should resolve
the ID and delegate to the authoritative store.

## 8. Tests and Evaluation Dependencies

The legacy path is still exercised by tests, including:

- `tests/integration/test_assistant_runtime_contracts.py`, which imports and
  constructs `CommunityOperationsAssistant` and tests legacy run/event API
  behavior;
- `tests/e2e/test_long_term_content_revision.py` and
  `tests/e2e/test_java_topic_operation_workflow.py`, which construct legacy
  `TaskIntent`/run contexts and exercise approval or run-linked behavior;
- `scripts/test_run_p0_e2e.py`, which tests the legacy process harness;
- tests under `community-assistant-agent/tests/`, covering the standalone
  application, migrations, query behavior, and tool/runtime contracts.

These are not all necessarily ACTIVE Runtime tests, but they are evidence of
compatibility obligations. A retirement change must classify each test as:

- migrated to Runtime behavior;
- retained as historical read/compatibility coverage; or
- intentionally retired with a reviewed contract change.

No test deletion is approved by this audit.

## 9. Retirement Readiness

| Gate | Current status | Evidence |
| --- | --- | --- |
| No production Python import | FAIL | `main.py`, `assistant_service.py`, and `routes.py` reference `LegacyAgentService`; service imports `agent.py` |
| No API route dependency | FAIL | Legacy run control and event routes remain |
| No CI dependency | FAIL | `.github/workflows/verify.yml` runs the legacy project |
| No script dependency | FAIL | Startup, smoke, report, verify, and P0 scripts reference it |
| No database dependency | FAIL | `assistant_runs`, approvals, events, steps, and migrations remain |
| No approval dependency | FAIL | Legacy approval models/routes/worker behavior remain |
| No SSE/event dependency | FAIL | Legacy history and stream endpoints remain |
| No test dependency | FAIL | Integration, E2E, P0, and standalone tests remain |
| ACTIVE replacement deployed and equivalent | NOT PROVEN | Runtime exists, but equivalence evidence for all legacy contracts is incomplete |

## 10. Required Migration Sequence

1. Instrument legacy route usage, fallback usage, approvals, event streams,
   and unresolved run IDs.
2. Complete `run_id -> execution_id` link coverage for all supported legacy
   operations.
3. Migrate status, cancel, interrupt, approval, event history, and SSE
   routes to the Runtime adapters while preserving old paths.
4. Migrate P0, CI, Docker/runbooks, startup scripts, and health checks to the
   ACTIVE Runtime.
5. Define a read-only and retention policy for `assistant_runs` and legacy
   event/approval records.
6. Run the full compatibility, integration, E2E, evaluation, and deployment
   validation suite with the legacy process disabled in a controlled
   environment.
7. Remove the `AssistantService` fallback and `LegacyAgentService` wiring only
   after the preceding gates pass.
8. Archive the standalone application after the data and rollback review.

## Final Decision

`agent.py`, `LegacyAgentService`, and `community-assistant-agent/` are not
DELETE_CANDIDATE items in Phase 11.3. They remain COMPATIBILITY components
with production, operational, persistence, approval, event, and test
dependencies.

The ACTIVE Runtime remains unchanged and is the migration target. No deletion,
move, or source modification was performed by this audit.
