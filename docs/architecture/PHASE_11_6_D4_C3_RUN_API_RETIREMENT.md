# Phase 11.6-D4-C3 Run API Retirement

## 1. Boundary

`/runs` is now a Legacy history interface. The only retained resource lookup
is:

```http
GET /api/v1/assistant/runs/{run_id}
```

For a mapped Runtime run, this lookup uses `RunExecutionAdapter` only to find
the `execution_id`, then reads the canonical Runtime projection. It does not
make `assistant_runs` the Runtime status source.

The collection endpoint `/runs` remains available for Legacy history and
compatibility discovery. Its rows now use only the historical RunRepository
metadata; it no longer fills status or steps from Runtime. ACTIVE frontend
execution lists use the Runtime execution list endpoint instead.

## 2. Retired run operations

The following routes were removed from the API:

- `POST /runs/{run_id}/cancel`
- `POST /runs/{run_id}/interrupt`
- `POST /runs/{run_id}/resume`
- `POST /runs/{run_id}/approve`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/events/stream`

Runtime control and visibility use `execution_id` exclusively:

- pause, resume, cancel: `/executions/{execution_id}/...`
- retry: `/executions/{execution_id}/steps/{step_id}/retry`
- approval: `/executions/{execution_id}/approve`
- events and SSE: `/executions/{execution_id}/events` and
  `/executions/{execution_id}/stream`

## 3. Adapter boundary

`RunExecutionAdapter` remains an ID-only compatibility lookup for historical
responses. It does not own state or expose a Runtime operation API.

`RunOperationAdapter` had no ACTIVE API consumer and was removed in the
follow-up Phase 11.6-D4-C4 cleanup.

The mapped execution projection now reads `execution_state_manager` directly,
avoiding a Legacy operation bridge and avoiding private adapter state access.

## 4. Frontend behavior

Mapped Runtime rows always call execution APIs for pause, resume, cancel,
retry, approval, events, and streaming. A Legacy-only row can still be
displayed as history, but control/stream operations do not fall back to a
`/runs` endpoint.

Runtime response `events_url` points to the execution stream. A Legacy-only
response points back to its history resource rather than an event stream.

## 5. Preserved infrastructure

This phase does not delete:

- `assistant_runs`
- `RunRepository`
- `LegacyAgent`
- `community-assistant-agent`
- `RunExecutionAdapter`

No Worker, Planner, ToolRuntime, ExecutionStateManager, or PlanExecution code
was modified.

## 6. Verification

- Targeted Python tests: `21 passed`.
- New route retirement contract tests cover history-only Legacy behavior and
  canonical Runtime operations.
- Frontend tests: `4 passed`.
- Frontend production build: passed.
- `RunOperationAdapter` has no production route reference.
