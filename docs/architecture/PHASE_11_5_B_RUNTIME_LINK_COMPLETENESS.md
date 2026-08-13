> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.5-B Runtime Link Completeness

## 1. Execution ID Transmission Chain

The Runtime execution chain now carries the canonical ID as follows:

```text
Assistant request
  -> run_id generated at API boundary
  -> RuntimeAgentService creates PlanExecution
  -> execution.execution_id
  -> RuntimeContext.execution_id
  -> RuntimeResult.execution_id
  -> API RunExecutionAdapter.bind_run_execution(run_id, execution_id)
  -> RunResponse / RunAcceptedResponse ExecutionReference
```

`PlanExecution` remains the object that creates and owns the execution ID.
Neither `RuntimeResult` nor `RunExecutionLink` copies execution state.

### Previous gap

`RuntimeAgentService` created a `PlanExecution` internally but did not return
its ID through `RuntimeResult`, and the API only checked
`RuntimeContext.execution_id`. As a result, a Runtime request could finish
without a link even though an execution existed.

### Current behavior

- `RuntimeResult` has optional `execution_id`.
- After `ExecutionWorker.init_from_plan()`, Runtime writes the ID to
  `RuntimeContext.execution_id`.
- Completed and waiting-for-approval Runtime results include the canonical ID.
- Approval rejection/failure results associated with an execution also retain
  the ID.
- Pre-execution clarification/failure results without a `PlanExecution` keep
  `execution_id=None`.

## 2. API Link Creation

The API uses the pure boundary helper in
`apps/assistant_api/greenbook_assistant_api/services/runtime_linking.py`.

The helper selects:

```text
result.execution_id or context.execution_id
```

and binds only when `result.execution_path == "runtime"`.

This prevents legacy responses from accidentally creating a Runtime link and
keeps the binding operation at the API boundary:

```text
run_id
  -> RunExecutionAdapter.bind_run_execution()
  -> configured RunExecutionLinkRepository
```

The helper does not inspect or mutate `PlanExecution`, `assistant_runs`,
ExecutionStatus, events, checkpoints, or steps.

The existing adapter's duplicate protection remains authoritative:

- identical `(run_id, execution_id)` binding is idempotent;
- a run bound to another execution raises
  `DuplicateRunExecutionBindingError`;
- an execution bound to another run raises the same protection error.

## 3. Response Contract

### Runtime-backed accepted response

`POST /api/v1/assistant/conversations/{conversation_id}/messages` now returns
optional fields populated for Runtime execution:

```json
{
  "run_id": "run-1",
  "execution_id": "execution-1",
  "execution_reference": {
    "run_id": "run-1",
    "execution_id": "execution-1",
    "task_id": "task-1",
    "source": "RUNTIME"
  },
  "conversation_id": "conversation-1",
  "status": "COMPLETED",
  "events_url": "/api/v1/assistant/runs/run-1/events"
}
```

`GET /api/v1/assistant/runs/{run_id}` and `GET /runs` continue using
`RunResponse` and resolve the same `ExecutionReference` through the adapter.

### Legacy-only response

Legacy-only behavior remains compatible:

```json
{
  "run_id": "legacy-run",
  "execution_id": null,
  "execution_reference": {
    "run_id": "legacy-run",
    "execution_id": null,
    "task_id": null,
    "source": "LEGACY_ONLY"
  }
}
```

The new fields are optional, so existing clients that only read `run_id`,
`conversation_id`, `status`, and `events_url` remain compatible.

## 4. Modified Files

- `apps/assistant_api/greenbook_assistant_api/models/runtime_result.py`
  - added optional `execution_id`.
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`
  - propagated the newly created PlanExecution ID to context and Runtime
    results for execution-backed completion/approval paths.
- `apps/assistant_api/greenbook_assistant_api/services/runtime_linking.py`
  - added the pure API binding helper.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  - uses the result/context ID fallback for link creation;
  - includes optional execution fields in `RunAcceptedResponse`;
  - includes execution ID in Runtime error details when available.
- `tests/unit/test_runtime_execution_link_completeness.py`
  - covers Runtime binding, context fallback, Legacy-only behavior, and
    duplicate protection.
- `docs/architecture/PHASE_11_5_B_RUNTIME_LINK_COMPLETENESS.md`
  - this report.

No Worker, Planner, ToolRuntime, ExecutionStateManager, PlanExecution schema,
StepExecution schema, TaskPlan, IntentSpec, Validator, migration, or
`assistant_runs` schema was modified.

## 5. Runtime / Legacy Boundary

| Case | Link behavior | Response behavior |
| --- | --- | --- |
| Runtime creates PlanExecution | Bind `run_id -> execution_id` | Return both IDs and Runtime reference |
| Runtime pre-execution failure | No PlanExecution to bind | Runtime result may have no execution ID |
| Runtime waiting for approval | Bind created execution | Return execution ID and Runtime reference |
| Legacy route/fallback result | No Runtime link | Keep `execution_id=null` unless an existing explicit link resolves it |
| Repeated Runtime bind | Existing adapter idempotency/protection | No duplicate mapping |

The link repository remains the only mapping storage. It does not become an
execution state source.

## 6. Persistence Note

This phase guarantees that every Runtime-backed result reaching the API with a
canonical execution ID is passed to `RunExecutionAdapter`. The adapter uses
its configured `RunExecutionLinkRepository`; existing repository reload tests
verify that the SQLAlchemy repository can restore a mapping after adapter
reconstruction.

Application deployments must inject the persistent repository implementation
into the application-level adapter. The adapter's default constructor remains
the in-memory compatibility/test implementation. This phase deliberately does
not redesign the async database lifecycle or modify `assistant_runs` schema.

Therefore:

- Runtime link completeness at the API boundary: addressed;
- production repository wiring and restart durability: remains an explicit
  deployment integration gate for the next persistence-focused phase.

## 7. Test Results

Passed:

- `pytest tests/compat/runtime`: 21 passed
- `pytest tests/unit/test_runtime_execution_link_completeness.py`: 4 passed
- `compileall` for modified Runtime/API/result/linking/test modules: passed
- `git diff --check`: passed

## 8. Why `assistant_runs` Remains

`assistant_runs` remains unchanged because it is still required for:

- Legacy-only run reads and API ownership checks;
- legacy response/result history;
- approval records and legacy run association;
- old events and SSE fallback;
- existing clients and compatibility routes.

The new link does not replace that table and does not copy canonical events
into `assistant_runs.events`. Retiring the table requires the separate approval
execution-reference migration and historical data policy.

## Final Decision

Runtime-backed API results now carry the canonical `execution_id`, and the API
creates the `run_id -> execution_id` compatibility link from that result.
Legacy-only responses remain unchanged. This phase stops at link completeness;
it does not continue the `assistant_runs` retirement.
