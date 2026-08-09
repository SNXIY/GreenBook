# Phase 11.6-D4 Legacy Removal Audit

## 1. Scope

This audit is static only. No source, test, configuration, database
migration, or directory was deleted or moved.

The canonical execution path remains:

```text
IntentSpec -> Planner -> TaskPlan -> PlanExecution
-> ExecutionStateManager -> Worker -> ToolRuntime -> ExecutionEventStore
```

## 2. Classification Summary

| Area | Classification | Reason |
| --- | --- | --- |
| `PlanExecution`, `ExecutionStateManager`, `Worker`, `ToolRuntime`, `ExecutionEventStore` | KEEP | Active Runtime source and execution path |
| Runtime API under `apps/assistant_api/.../api/runtime_routes.py` | KEEP | Canonical execution visibility and control API |
| `LegacyAgentService`, `greenbook_assistant_core/agent.py` | MIGRATE | Still directly imported and used by explicit Legacy mode/fallback |
| `LegacyFallbackAdapter` | KEEP temporarily | Required emergency-only boundary; delete after fallback retirement |
| `RunRepository`, `assistant_runs` | MIGRATE | Current `/runs` compatibility API and legacy history/projection storage |
| `/api/v1/assistant/runs/*` | MIGRATE | Runtime-mapped compatibility surface still used by clients and tests |
| `RunExecutionAdapter`, `RunOperationAdapter`, `ExecutionReference` | KEEP temporarily | ID-only bridge required while `run_id` consumers remain |
| `run_id` contracts and metadata | MIGRATE | Still present in API response, frontend compatibility, task metadata, Java contracts, and legacy data |
| `community-assistant-agent/` | MIGRATE | CI, development scripts, smoke/P0 E2E flows still reference it |
| Legacy-only tests and compatibility tests | KEEP temporarily | Protect current emergency and historical behavior during migration |

## 3. Detailed Audit

### 3.1 Legacy Agent

**Current production references:**

- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
  imports `greenbook_assistant_core.agent.CommunityOperationsAssistant`.
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
  constructs `LegacyFallbackAdapter` and invokes it only when Legacy mode or
  the explicit fallback boundary permits it.
- `apps/assistant_api/greenbook_assistant_api/main.py` and the lazy setup in
  `api/routes.py` construct `LegacyAgentService` when
  `ASSISTANT_RUNTIME_MODE=off` or fallback is explicitly enabled.

**Classification:** `MIGRATE`.

**Delete gate:** remove the Legacy mode branch, disable and remove the
emergency fallback, migrate all Legacy-only behavior/tests, and verify no
Python import remains. Until then, deleting `agent.py` or
`LegacyAgentService` breaks an active compatibility path.

### 3.2 Fallback Configuration

Relevant locations:

- `apps/assistant_api/greenbook_assistant_api/main.py`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
- `tests/unit/test_legacy_fallback_isolation.py`

`ASSISTANT_RUNTIME_MODE` defaults to `on`; `off` is still an explicit Legacy
mode. `ENABLE_LEGACY_AGENT_FALLBACK` defaults to `false`, but remains an
emergency switch.

**Classification:** `KEEP` temporarily, then `DELETE` after the emergency
period. Removing the variables before removing their consumers would make the
fallback boundary ambiguous and break compatibility tests.

### 3.3 Run Persistence and API

Relevant locations:

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
  defines `assistant_runs` and `RunRepository.create/update/find`.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` uses
  `RunRepository` for row persistence and keeps `/runs/{run_id}` endpoints.
- Runtime-backed rows are now non-authoritative projections; status, steps, and
  events resolve through `RunExecutionAdapter` to `PlanExecution` and
  `ExecutionEventStore`.
- Legacy-only cancel/interrupt and historical reads still use the repository.

**Classification:** `MIGRATE`, not delete.

**Delete gate:** all Runtime consumers must call `/executions/{execution_id}`;
legacy-only data must have an agreed retention/export policy; no API, approval,
SSE, frontend, Java, task metadata, or test path may require `run_id`.

No dedicated `assistant_runs` migration file was found in the scanned
migration directories. The table is declared by the assistant core repository;
database retirement therefore requires a separate schema/data plan and is
outside this audit.

### 3.4 Compatibility Runtime

Relevant files:

- `packages/assistant_core/greenbook_assistant_core/compatibility/history/run_execution_link.py`
- `.../run_execution_repository.py`
- `.../run_operation_adapter.py`
- `.../execution_reference.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_linking.py`

These modules do not own execution state. They map `run_id` to the canonical
`execution_id` and delegate control/event reads to Runtime components.

**Classification:** `KEEP` during migration, followed by `DELETE` only after
the legacy API and all `run_id` consumers are retired. They are not duplicate
execution state models.

### 3.5 Frontend

The active Execution Console uses `apps/frontend/src/services/executionService.ts`
and `/executions/{execution_id}` endpoints.

Legacy compatibility remains in:

- `apps/frontend/src/services/assistantService.ts`
  (`/api/v1/assistant/runs/{run_id}`, cancel, interrupt, and legacy SSE;
  execution-aware branches already exist).
- `apps/frontend/src/components/assistant/AssistantPanel.tsx`
- `apps/frontend/src/pages/TaskCenterPage.tsx`
- `apps/frontend/src/components/comments/CommentSection.tsx`

These components retain `run_id` for accepted responses, polling, controls,
and fallback behavior.

**Classification:** Runtime Console `KEEP`; legacy branches `MIGRATE`.

### 3.6 CI, Docker, and Scripts

Confirmed references:

- `.github/workflows/verify.yml` runs checks with
  `community-assistant-agent` as working directory.
- `scripts/verify-all.ps1`, `scripts/setup-dev.ps1`,
  `scripts/smoke-test.ps1`, and `scripts/runtime-report.ps1` reference or
  enter `community-assistant-agent`.
- `scripts/run_p0_e2e.py` starts and exercises the legacy assistant root and
  stores assistant `run_id` values in its manifest.
- `scripts/e2e-test.ps1` consumes the accepted `run_id` and legacy run URLs.
- `scripts/start-assistant.ps1` and assistant configuration retain the legacy
  audience name `community-assistant-agent`.

No direct Docker service reference to the legacy directory was found in the
scanned `docker-compose.yml` and `infra` files. The scripts and CI references
are nevertheless sufficient to block deletion.

**Classification:** `MIGRATE` for CI/scripts/E2E; re-audit Docker after those
flows are converted to the Runtime API.

### 3.7 Database and External Contracts

The scan found:

- assistant core `assistant_runs` and approval/task metadata using `run_id`;
- backend assistant capability/comment tables and Java code using
  `run_id`/`assistant_run_id`;
- creator-agent migrations using their own `creator_runs` identifiers.

These are not interchangeable models. A global replacement would risk
breaking Java capability provenance, comments, creator workflows, or historical
data.

**Classification:** `MIGRATE` by contract owner, with historical data
`KEEP` until retention and backfill decisions are approved. No schema deletion
is safe in this phase.

## 4. Deletion Plan

### Phase D4-A: Consumer Inventory and Runtime API Migration

- Convert frontend controls, polling, SSE, and task views to use
  `execution_id`/`ExecutionReference`.
- Convert P0 E2E and development scripts to Runtime API.
- Remove normal `/runs` use from active UI while preserving the endpoint.
- Keep `run_id` only for explicit Legacy/history references.

### Phase D4-B: Legacy Execution Isolation

- Remove `ASSISTANT_RUNTIME_MODE=off` from normal deployment configuration.
- Retain a separately tested emergency fallback process, if required.
- Move Legacy-only tests to an explicit compatibility suite.
- Confirm zero ACTIVE Python imports of `agent.py` and
  `LegacyAgentService`.

### Phase D4-C: Legacy Code Retirement

Only after D4-A and D4-B gates pass:

- archive or delete `LegacyAgentService`, `LegacyFallbackAdapter`, and
  `greenbook_assistant_core/agent.py`;
- remove the legacy assistant project and its CI/script entries;
- archive/delete compatibility runtime adapters after all `/runs` and
  `run_id` consumers are gone.

### Phase D4-D: Persistence Retirement

- classify and export historical `assistant_runs` data;
- migrate remaining task/approval/provenance references;
- remove `RunRepository` and the `assistant_runs` table only through an
  approved database migration and rollback plan.

## 5. Final Delete Checklist

A candidate may be marked `DELETE` only when all of the following are true:

- no Python import or API route reference;
- no frontend, Java, CI, Docker, script, or deployment reference;
- no active database write/read dependency;
- no approval, SSE, event, or control dependency;
- no non-archived test requires it;
- historical data has a retention/export decision;
- Runtime API and ExecutionEventStore have verified replacement coverage;
- full unit, compatibility, evaluation, and relevant integration suites pass.

## 6. Audit Result

No item in the listed Legacy set is currently safe for immediate deletion.
The nearest candidates are the Legacy implementation and its fallback adapter,
but both still have direct imports or explicit compatibility callers. The
correct next action is consumer migration, followed by a second audit; this
phase intentionally performs no deletion or movement.
