> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-A Consumer Migration

## 1. Outcome

Active Runtime consumers now use `execution_id` for execution lifecycle
operations. `run_id` remains only as a compatibility/history reference when a
response is backed by the legacy API contract.

Canonical active flow:

```text
Assistant response.execution_id
  -> /executions/{execution_id}
  -> status / steps / events / SSE / pause / resume / cancel / retry
```

No Runtime core, `PlanExecution`, `ExecutionStateManager`, Worker, Planner, or
ToolRuntime code was changed.

## 2. Frontend

### Migrated

- `apps/frontend/src/services/executionService.ts` remains the canonical
  Runtime API client.
- Runtime-backed `assistantService.listRuns()` uses the legacy list only to
  discover references, then refreshes status and steps from
  `/executions/{execution_id}`.
- Runtime-backed `getRun()` reads status and steps from Execution API.
- cancel, pause, resume, and retry use Execution API when `execution_id` is
  present.
- Runtime SSE uses `executionService.streamEvents()`.
- Runtime approval uses `/executions/{execution_id}/approve` for approval and
  the shared approval endpoint for rejection; no run-based approval route is
  used for mapped Runtime requests.
- `AssistantPanel`, `TaskCenterPage`, and `CommentSection` pass the available
  `execution_id` through approval and control operations.

### Retained compatibility

Requests without `execution_id` continue to use `/runs/{run_id}` operations.
This is required for Legacy-only history and is not an active Runtime path.
The UI still retains `run_id` on normalized response objects so old links and
history records remain addressable.

## 3. Scripts and E2E

### `scripts/run_p0_e2e.py`

- Requires `accepted.execution_id` for active Assistant Runtime requests.
- Polls `/api/v1/assistant/executions/{execution_id}`.
- Records `assistant_execution_ids` as the primary harness identifiers.
- Retains `assistant_history_refs` only for post-completion content/artifact
  lookup through the compatibility history API.
- Progress logs and timeout evidence use `execution_id`.

### `scripts/e2e-test.ps1`

- `Wait-Execution` polls the Runtime status endpoint.
- The accepted `execution_id` is required for active execution scenarios.
- A completed `/runs/{run_id}` lookup remains only for response content and
  creator artifact history, not status or lifecycle control.

### Development and smoke scripts

- `scripts/setup-dev.ps1` installs `apps/assistant_api` instead of the legacy
  project.
- `scripts/smoke-test.ps1` runs Assistant Runtime API tests from the workspace.
- `scripts/verify-all.ps1` runs the workspace unit suite instead of a separate
  `community-assistant-agent` test project.

`scripts/runtime-report.ps1` still targets the historical Legacy evaluation
report script and is intentionally not treated as an active Runtime consumer;
it requires a separate evaluation-report migration before removal.

## 4. CI

`.github/workflows/verify.yml` now runs the assistant-runtime job from the
workspace root and executes:

```text
uv sync --frozen
uv run pytest -q tests/unit tests/evaluation
```

The former CI job whose working directory was
`community-assistant-agent` is no longer part of the ACTIVE verification path.
The directory itself remains untouched for Legacy compatibility.

## 5. Java Contract Audit

No global replacement was performed.

- `execution_id`: canonical Assistant Runtime execution identifier.
- `run_id`: retained for legacy Assistant history and compatibility responses.
- `agent_run_id`: integration/request correlation value used by Java client
  calls; requires a separate contract decision and was not mechanically
  renamed.
- `creator_run_id` and Creator `run_id`: belong to Creator workflows and are
  not Assistant Runtime identifiers.
- Backend `assistant_run_id` provenance fields remain unchanged.

## 6. Residual Compatibility References

The following are intentionally retained and should not be interpreted as
active Runtime state consumers:

- `/api/v1/assistant/runs/{run_id}` API and `RunRepository`;
- `assistant_runs` history/projection storage;
- `RunExecutionAdapter` and `ExecutionReference`;
- Legacy-only frontend fallback branches;
- `LegacyAgent`, `LegacyAgentService`, and `community-assistant-agent`.

The remaining frontend `/runs` list call is an identifier discovery bridge;
mapped Runtime status and steps are immediately replaced with Execution API
data. A future `/executions` list endpoint can remove that discovery call.

## 7. Verification Plan

Recommended checks after this migration:

- `npm run build` in `apps/frontend`;
- Python compileall for `apps/assistant_api` and `scripts/run_p0_e2e.py`;
- frontend unit tests for execution service and console;
- `pytest tests/unit tests/evaluation tests/compat/runtime`;
- `git diff --check`.

## 8. Next Gate

Before deleting compatibility code, migrate the remaining history lookup and
the Java/Creator contract consumers, then verify that no active UI, CI, E2E,
approval, SSE, or control path requires `run_id`.
